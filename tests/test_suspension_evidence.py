from types import SimpleNamespace
import json

import pandas as pd
import pytest

from core.data.data_manager_stock import StockDataManager
from core.data.data_manager_suspension import FIELDS
from backtest.backtest_engine import BacktestEngine, BacktestConfig
from backtest.evidence_policy import ReplayEvidenceBlocked

DATE = '20260901'


def row(code='002870.SZ', kind='S', timing='', date=DATE):
    return {'ts_code': code, 'trade_date': date, 'suspend_type': kind, 'suspend_timing': timing}


def manager(tmp_path, rows):
    dm = StockDataManager('', tmp_path)
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(rows, columns=FIELDS)
    dm.ts_pro = SimpleNamespace(suspend_d=query)
    return dm, calls


@pytest.mark.parametrize('kind,timing,expected', [
    ('S', '', 'full_day'), ('S', None, 'full_day'), ('R', '', 'trading'),
    ('S', '09:30-10:00', 'intraday'), ('S', '09:30-15:00', 'intraday')])
def test_daily_types_and_timing_are_not_conflated(tmp_path, kind, timing, expected):
    dm, calls = manager(tmp_path, [row(kind=kind, timing=timing)])
    assert dm.get_suspension_status('002870', DATE)['state'] == expected
    assert dm.get_suspension_status('002870.SZ', DATE)['state'] == expected
    assert len(calls) == 1
    assert calls[0]['trade_date'] == DATE


def test_empty_success_is_cached_but_not_a_suspension(tmp_path):
    dm, calls = manager(tmp_path, [])
    assert dm.get_suspension_status('002870', DATE)['state'] == 'trading'
    offline = StockDataManager('', tmp_path)
    offline.allow_remote_history = False
    assert offline.get_suspension_status('002870', DATE)['state'] == 'trading'
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['permission', 'bad_columns', 'wrong_date', 'limit', 'conflict'])
def test_failed_or_ambiguous_evidence_never_asserts_full_day(tmp_path, failure):
    dm, _ = manager(tmp_path, [row()])
    if failure == 'permission':
        dm.ts_pro.suspend_d = lambda **kw: (_ for _ in ()).throw(ValueError('no permission'))
    elif failure == 'bad_columns':
        dm.ts_pro.suspend_d = lambda **kw: pd.DataFrame()
    elif failure == 'wrong_date':
        dm.ts_pro.suspend_d = lambda **kw: pd.DataFrame([row(date='20260831')])
    elif failure == 'limit':
        dm.ts_pro.suspend_d = lambda **kw: pd.DataFrame([row()] * 5000)
    else:
        dm.ts_pro.suspend_d = lambda **kw: pd.DataFrame([row(), row(kind='R')])
    assert dm.get_suspension_status('002870', DATE)['state'] == 'unknown'
    if failure != 'conflict':
        assert not (dm.stock_dir / 'suspend_d' / f'{DATE}.json').exists()


@pytest.mark.parametrize('payload', [[], {'trade_date': DATE}, 'not-json'])
def test_corrupt_cache_is_repaired_not_trusted(tmp_path, payload):
    dm, calls = manager(tmp_path, [row()])
    path = dm.stock_dir / 'suspend_d' / f'{DATE}.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding='utf-8')
    assert dm.get_suspension_status('002870', DATE)['state'] == 'full_day'
    assert len(calls) == 1


def test_backtest_reads_status_cache_only_without_remote_calls(tmp_path):
    dm, calls = manager(tmp_path, [row()])
    engine = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy='strict'))
    assert not engine._is_full_day_suspended('002870', DATE, scope='candidate')
    assert calls == []
    dm.get_daily_suspensions(DATE)
    other = BacktestEngine(StockDataManager('', tmp_path), engine.config)
    assert other._is_full_day_suspended('002870', DATE, scope='candidate')
    assert len(calls) == 1


def test_full_day_candidate_is_a_valid_non_tradable_result_even_in_strict_mode(tmp_path):
    dm, _ = manager(tmp_path, [row()])
    dm.get_daily_suspensions(DATE)
    dm.get_stock_tick = lambda *args: pytest.fail('no minute request for verified suspension')
    dm.get_auction_data = lambda *args: pytest.fail('no auction request for verified suspension')
    dm.get_stock_daily_data = lambda *args: pytest.fail('do not guess entry from daily bars')
    instance = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy='strict'))
    instance._load_sector_peer_codes = lambda *args: {}
    plan = pd.Series({'代码': '002870', '名称': '测试', '目标价': 10})
    instance._prefetch_entry_data(pd.DataFrame([plan]), DATE)
    assert instance._check_buy_conditions(plan, DATE, '002870', '测试') == (False, 0)
    assert instance.entry_attempts[-1]['reason_code'] == 'suspended'
    quality = instance.replay_data_quality()
    assert quality['status'] == 'complete'
    assert quality['skipped_candidate_count'] == 0
    assert quality['candidate_samples'] == 0
    assert quality['suspended_candidate_count'] == 1


def test_full_day_holding_is_frozen_then_requires_real_evidence_on_resume(tmp_path):
    dm, _ = manager(tmp_path, [row()])
    dm.get_daily_suspensions(DATE)
    dm.get_stock_tick = lambda *args: pd.DataFrame()
    instance = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy='strict', initial_capital=10000))
    instance.cash = 9000
    position = {'shares': 100, 'entry_price': 10, 'market_value': 1000, 'last_close': 10}
    instance.current_positions = {'002870': position.copy()}
    instance._get_prev_trade_date = lambda date: '20260831'
    instance._load_trade_plans = lambda *args: pd.DataFrame()
    instance._get_stock_daily_bar = lambda *args: pytest.fail('do not invent daily exit on suspension')
    instance._process_date(DATE, 'unused')
    instance._execute_sell('002870', 1, DATE, 'stop_loss')
    assert instance.current_positions['002870'] == position
    assert instance.cash == 9000
    assert instance.daily_nav[-1]['total_value'] == 10000
    assert instance.replay_data_quality()['suspended_sessions'][0]['valuation_basis'] == 'last_available_mark'
    with pytest.raises(ReplayEvidenceBlocked):
        instance._process_date('20260902', 'unused')
    assert len(instance.daily_nav) == 1


def test_intraday_suspension_missing_minutes_still_blocks(tmp_path):
    dm, _ = manager(tmp_path, [row(timing='09:30-10:00')])
    dm.get_daily_suspensions(DATE)
    dm.get_stock_tick = lambda *args: pd.DataFrame()
    instance = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy='strict'))
    instance.current_positions = {'002870': {'market_value': 1000}}
    with pytest.raises(ReplayEvidenceBlocked):
        instance._process_date(DATE, 'unused')


def test_incremental_evidence_exempts_only_confirmed_full_day(tmp_path):
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.incremental_replay import IncrementalPaperReplay
    dm, _ = manager(tmp_path, [row(), row(code='002860.SZ', timing='10:00-11:00')])
    calls = []
    dm.get_stock_tick = lambda code, date, **kw: (calls.append(code) or pd.DataFrame())
    replay = IncrementalPaperReplay(HoldingRepository(tmp_path / 'holdings.sqlite'), data_manager=dm)
    assert replay._required_minutes(DATE, ['002870', '002860']) == ['002860']
    assert calls == ['002860.SZ']
    assert replay._suspended_codes == {'002870'}


def test_rebuild_prefetch_removes_confirmed_suspension_before_requesting_minutes(tmp_path, monkeypatch):
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.portfolio.holding_repository import HoldingRepository
    import core.data as data_module
    import scripts.prefetch_strategy_minutes as prefetch
    import backtest.trade_calendar as calendar
    dm, _ = manager(tmp_path, [row()])
    dm.get_stock_tick = lambda *a, **kw: pytest.fail('verified suspension must not request minutes')
    dm.get_auction_data = lambda *a: pytest.fail('verified suspension must not request auction')
    monkeypatch.setattr(data_module, 'DataManager', lambda *a, **kw: dm)
    monkeypatch.setattr(calendar, 'TradeCalendar', lambda: SimpleNamespace(is_real=True, prev=lambda date: '20260831'))
    monkeypatch.setattr(prefetch, 'build_requirements', lambda *a, **kw: (
        {(DATE, '002870.SZ')}, {(DATE, '002870.SZ')}, {}))
    service = PaperTradingService(HoldingRepository(tmp_path / 'portfolio.sqlite'))
    result = service.prefetch_replay_minutes(DATE, DATE)
    assert result['ok']
    assert result['minute_failed'] == 0
    assert result['auction_failed'] == 0
    assert result['suspended_count'] == 1
    assert result['tradable_minute_requirements'] == 0


def test_suspension_audit_survives_engine_checkpoint(tmp_path):
    dm, _ = manager(tmp_path, [row()])
    dm.get_daily_suspensions(DATE)
    instance = BacktestEngine(dm)
    instance._is_full_day_suspended('002870', DATE, scope='holding')
    restored = BacktestEngine(dm)
    restored.import_state(instance.export_state())
    assert restored.replay_data_quality()['suspended_sessions'] == instance.replay_data_quality()['suspended_sessions']


def test_rebuild_loads_daily_status_even_without_new_candidates(tmp_path, monkeypatch):
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.portfolio.holding_repository import HoldingRepository
    import core.data as data_module
    import scripts.prefetch_strategy_minutes as prefetch
    import backtest.trade_calendar as calendar
    dm, calls = manager(tmp_path, [])
    dates = [DATE, '20260902']
    monkeypatch.setattr(data_module, 'DataManager', lambda *a, **kw: dm)
    monkeypatch.setattr(calendar, 'TradeCalendar', lambda: SimpleNamespace(
        is_real=True, prev=lambda date: '20260831', get_trade_dates=lambda start, end: dates))
    monkeypatch.setattr(prefetch, 'build_requirements', lambda *a, **kw: (set(), set(), {}))
    service = PaperTradingService(HoldingRepository(tmp_path / 'portfolio.sqlite'))
    result = service.prefetch_replay_minutes(DATE, dates[-1])
    assert result['ok']
    assert result['suspension_query_failures'] == []
    assert [call['trade_date'] for call in calls] == dates
    assert (dm.stock_dir / 'suspend_d' / f'{dates[-1]}.json').exists()
