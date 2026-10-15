from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from core.portfolio.account_replacement import AccountReplacement
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.incremental_replay import IncrementalPaperReplay
from core.portfolio.paper_trading_service import PaperTradingService
from core.realtime.day_replay import _ReplayEntryService
from core.signals.production_rules import RULESET_VERSION, candidate_strength, production_candidates
from core.signals.sector_sync import make_constituent_sync_checker


def sector_inputs():
    codes = ['000002', '600000', '600001']
    frames = {c: pd.DataFrame({'time': ['09:33:00', '09:36:00'], 'close': [9.1, 9.5]}) for c in codes}
    return dict(code='000001', names=['test'], members={'test': codes}, frames=frames,
                previous={c: {'close': 10} for c in codes}, opening_prices={c: 9 for c in codes})


def test_backtest_and_day_diagnostic_use_latest_sector_policy():
    inputs = sector_inputs()
    shared, detail = make_constituent_sync_checker(**inputs)
    assert shared('09:36:00') is True
    assert detail['confirmation_path'] == 'open_rising_3m'
    engine = BacktestEngine(None, BacktestConfig(production_rules=True))
    engine._production_inputs = inputs
    engine._minute_frames = {('20261014', c): f for c, f in inputs['frames'].items()}
    historical = engine._sector_sync_checker(pd.Series({'共振板块': 'test'}), '20261014', '000001')
    service = _ReplayEntryService(inputs['previous'], {}, inputs['members'], inputs['frames'],
                                  SimpleNamespace(), inputs['opening_prices'])
    diagnostic, observed = service._sector_checker({'sector_names': ['test']}, '000001', [], {}, {}, '20261014')
    assert historical('09:36:00') is diagnostic('09:36:00') is True
    assert observed['sector_sync_policy_version'] == detail['sector_sync_policy_version']
    assert observed['trend_coverage'] == 1


def test_coverage_and_self_confirmation_cannot_be_bypassed():
    inputs = sector_inputs()
    inputs['members']['test'] += ['000001'] + [f'{n:06d}' for n in range(10, 30)]
    checker, detail = make_constituent_sync_checker(**inputs)
    assert checker('09:36:00') is None
    assert detail['data_completeness'] == 0


def test_candidates_are_deduplicated_and_risky_stocks_excluded():
    inputs = {'leader_pools': {'10': {'rows': [
        {'code': '000001', 'name': 'leader', 'leader_score': 70},
        {'code': '000002', 'name': '*ST测试'}]}},
        'candidates': [{'code': '000001', 'name': 'leader', 'strategy_id': 'weak_to_strong',
                        'strategy_execution': {'max_positions': 1}}]}
    rows = production_candidates(inputs)
    assert len(rows) == 1
    assert rows[0]['strategy_id'] == 'weak_to_strong'
    assert rows[0]['leader_score'] == 70


def test_rotation_strength_is_shared():
    row = {'screening_score': 60, 'entry_mode': 'weak_to_strong',
           'action_group': '重点确认', 'sector_detail': {'breadth': .7}}
    assert candidate_strength(row) == PaperTradingService._candidate_strength(row)
    engine = BacktestEngine(None, BacktestConfig(production_rules=True))
    engine._last_entry_meta['000001'] = {'entry_strength_score': candidate_strength(row)}
    assert engine._last_entry_meta['000001']['entry_strength_score'] == 69


def certified_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(IncrementalPaperReplay, 'config_version', staticmethod(lambda: 'current-v3'))
    repo = HoldingRepository(tmp_path / 'accounts.sqlite')
    repo.ensure_account('default', name='paper', initial_capital=100000)
    repo.ensure_account('history_new', name='replay', initial_capital=100000)
    repo.open_position({'code': '000001', 'name': 'manual', 'entry_date': '20261013',
                        'entry_price': 10, 'shares': 100, 'source': 'manual'}, 'default')
    repo.open_position({'code': '600001', 'name': 'replay', 'entry_date': '20261014',
                        'entry_price': 12, 'shares': 200, 'source': 'historical_replay',
                        'metadata': {'strategy_execution': {'exit': {'hard_stop_loss': .05}}}}, 'history_new')
    service = AccountReplacement(repo)
    service.register('history_new', 'new', '20261014', 'current-v3', {
        'ruleset_version': RULESET_VERSION, 'status': 'complete', 'costs_verified': True,
        'full_sample_metrics_valid': True})
    return repo, service


def test_adoption_backs_up_manual_trades_and_checkpoints_next_day(tmp_path, monkeypatch):
    repo, service = certified_accounts(tmp_path, monkeypatch)
    old = repo.account('default')
    preview = service.preview('history_new')
    result = service.replace('history_new', preview['revision'])
    backup = result['backup_account']
    assert [p['code'] for p in repo.list_positions(backup)] == ['000001']
    assert repo.list_trades(backup)[0]['source'] == 'manual'
    assert repo.account(backup)['cash'] == old['cash']
    assert [p['code'] for p in repo.list_positions('default')] == ['600001']
    assert repo.list_positions('default')[0]['metadata']['strategy_execution']['exit']['hard_stop_loss'] == .05
    assert repo.account('default')['cash'] == repo.account('history_new')['cash']
    with repo._connect() as conn:
        checkpoint = conn.execute("SELECT * FROM portfolio_replay_checkpoints WHERE account_key='default'").fetchone()
        assert checkpoint['last_completed_date'] == '20261014'
        assert checkpoint['account_trade_id'] == repo.list_trades('default')[0]['id']


def test_adoption_refuses_account_changed_after_preview(tmp_path, monkeypatch):
    repo, service = certified_accounts(tmp_path, monkeypatch)
    preview = service.preview('history_new')
    repo.open_position({'code': '600002', 'name': 'later manual', 'entry_date': '20261014',
                        'entry_price': 10, 'shares': 100}, 'default')
    with pytest.raises(ValueError, match='账户已发生变化'):
        service.replace('history_new', preview['revision'])
    assert len(repo.list_positions('default')) == 2
    assert not any(a['account_key'].startswith('backup_') for a in repo.list_accounts())


def test_adoption_rolls_back_backup_and_target_on_failure(tmp_path, monkeypatch):
    repo, service = certified_accounts(tmp_path, monkeypatch)
    preview = service.preview('history_new')
    original = service._copy
    def fail_copy(conn, source, target, **kwargs):
        if target == 'default':
            raise RuntimeError('injected failure')
        original(conn, source, target, **kwargs)
    monkeypatch.setattr(service, '_copy', fail_copy)
    with pytest.raises(RuntimeError, match='injected'):
        service.replace('history_new', preview['revision'])
    assert [p['code'] for p in repo.list_positions('default')] == ['000001']
    assert not any(a['account_key'].startswith('backup_') for a in repo.list_accounts())


def test_adoption_refuses_incomplete_and_old_rules(tmp_path, monkeypatch):
    repo, service = certified_accounts(tmp_path, monkeypatch)
    service.register('history_new', 'new', '20261014', 'current-v3', {'status': 'incomplete'})
    with pytest.raises(ValueError, match='旧口径或缺证据'):
        service.preview('history_new')


def test_latest_exit_reduces_once_and_does_not_skip_later_minutes(monkeypatch):
    inputs = sector_inputs()
    engine = BacktestEngine(None, BacktestConfig(production_rules=True, slippage=0, commission_rate=0, stamp_duty_rate=0))
    engine._production_inputs = inputs
    engine._minute_frames = {('20261014', c): f for c, f in inputs['frames'].items()}
    engine._exit_scan_times = {}
    bars = pd.DataFrame([
        dict(time='09:36:00', open=10.1, high=10.15, low=10.1, close=10.1),
        dict(time='09:37:00', open=10.1, high=10.15, low=9.3, close=9.3)])
    monkeypatch.setattr(engine, '_load_exit_minute_bars', lambda *args: bars)
    position = dict(stock_name='test', shares=1000, entry_price=10, entry_date='20261013',
                    stop_loss_price=9.4, highest_price=10.15, cost_basis=10000, pattern_type='test',
                    hot_resonance=False, resonance_sectors='test', strategy_id='first_board_launch')
    engine.current_positions['000001'] = position
    first = engine._minute_exit_decision('000001', position, '20261014', {'open':10.4,'pre_close':10.5})
    assert first['reason'] == 'context_reduce'
    assert first['shares'] == 500
    assert engine._exit_scan_times[('20261014', '000001')] == '09:36:00'
    engine._execute_sell('000001', first['price'], '20261014', first['reason'],
                         exit_time=first['time'], shares_to_sell=first['shares'])
    assert position['shares'] == 500 and position['cost_basis'] == 5000
    second = engine._minute_exit_decision('000001', position, '20261014', {'open':10.4,'pre_close':10.5})
    assert second['reason'] == 'stop_loss_minute' and second['time'] == '09:37:00'


def test_range_replay_releases_previous_constituent_frames_without_losing_account():
    engine = BacktestEngine(None, BacktestConfig(production_rules=True))
    old, current = ('20261013', '000001'), ('20261014', '000001')
    frame = pd.DataFrame([dict(time='09:31:00', close=10)])
    for cache in (engine._minute_frames, engine._exit_minute_frames, engine._auction_rows,
                  engine._exit_scan_times, engine._sector_evidence, engine._sector_evidence_snapshots):
        cache.update({old: frame, current: frame})
    engine._production_inputs = {'date': '20261013', 'frames': {'000001': frame}}
    engine.current_positions['000001'] = {'shares': 100, 'entry_date': '20261013'}
    engine.entry_attempts.append({'date': '20261013', 'sector_evidence': {'coverage': 1}})
    engine._production_input_manifest.append({'date': '20261013', 'input_hash': 'sealed'})
    engine._release_completed_minute_data('20261014')
    assert set(engine._minute_frames) == {current}
    assert set(engine._exit_minute_frames) == {current}
    assert set(engine._sector_evidence_snapshots) == {current}
    assert engine._production_inputs == {}
    assert engine.current_positions['000001']['shares'] == 100
    assert engine.entry_attempts[0]['sector_evidence']['coverage'] == 1
    assert engine._production_input_manifest[0]['input_hash'] == 'sealed'


def test_sector_price_index_keeps_last_duplicate_and_never_fills_missing_times():
    from core.signals.sector_sync import constituent_minute_prices
    frames = {'000001': pd.DataFrame([
        {'time': '09:31:00', 'close': 10}, {'time': '09:31:00', 'close': 11},
        {'time': '09:33:00', 'close': 12}]), '000002': pd.DataFrame()}
    indexed = constituent_minute_prices(frames)
    assert indexed['000001']['09:31:00'] == 11
    assert '09:32:00' not in indexed['000001']
    assert '000002' not in indexed


@pytest.mark.parametrize('allow_remote', [False, True])
def test_production_loader_respects_minute_fetch_policy_and_publishes_progress(monkeypatch, allow_remote):
    from types import SimpleNamespace
    import core.realtime.day_replay as replay
    requests, progress = [], []

    class Loader:
        def __init__(self, **kwargs):
            pass

        def run(self, request, **kwargs):
            requests.append(request)
            kwargs['progress']({'completed': 1, 'total': 1, 'code': '000001'})
            return dict(rows=[], frames={}, previous={}, auctions={}, members={}, sector_codes={},
                        opening_prices={}, opening_price_source='test', bundle_id='', input_basis='test')

    monkeypatch.setattr(replay, 'DayReplayLoader', Loader)
    engine = BacktestEngine(SimpleNamespace(allow_remote_minutes=allow_remote), BacktestConfig(production_rules=True))
    engine._replay_progress = progress.append
    engine._prepare_production_day('20261014')
    assert requests[0]['fetch_missing'] is allow_remote
    assert progress[0]['trade_date'] == '20261014'
    assert progress[0]['unit'] == '证据项'


def test_live_parity_requires_prefixes_and_must_not_revive_old_confirmations():
    from backtest.minute_entry import MinuteEntryEvaluator
    bars = pd.DataFrame([
        dict(time=f'09:{minute:02d}:00', open=10, high=10, low=10, close=10,
             volume=100, amount=100000) for minute in range(31, 36)] + [
        dict(time='09:36:00', open=10.1, high=10.1, low=10.1, close=10.1, volume=100, amount=101000),
        dict(time='09:37:00', open=10.1, high=10.1, low=10.1, close=10.1, volume=100, amount=101000),
        dict(time='14:59:00', open=10.2, high=10.2, low=10.2, close=10.2, volume=100, amount=102000),
        dict(time='15:00:00', open=10.2, high=10.2, low=10.2, close=10.2, volume=100, amount=102000)])
    bars['volume_unit'] = 'lots'
    evaluator = MinuteEntryEvaluator()
    arguments = dict(mode='weak_only', open_gap=0, prev_close=10,
                     plan_amount_ratio=1, sector_sync=lambda time: True)
    historical = evaluator.evaluate(bars=bars, **arguments)
    prefix = evaluator.evaluate(bars=bars.iloc[:7], live=True, **arguments)
    after_close = evaluator.evaluate(bars=bars, live=True, **arguments)
    assert historical.filled and prefix.filled
    assert historical.confirm_time == prefix.confirm_time == '09:36:00'
    assert historical.entry_time == prefix.entry_time == '09:37:00'
    assert after_close.status == 'cancelled'
