from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.plan_source import _to_backtest_row
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.paper_trading_service import PaperTradingService
from core.utils.stock_eligibility import new_entry_exclusion


@pytest.mark.parametrize('row', [
    {'name': '*ST国华'}, {'名称': 'ST恒久'}, {'股票名称': ' *st 测试 '},
    {'name': 'S*ST测试'}, {'name': 'ＳＴ测试'}, {'is_st': 1},
    {'context': {'is_st': 'true'}}, {'name': '某某退'}, {'name': '退市测试'},
    {'name': '正常名称', 'stock_name': '*ST旧记录'},
])
def test_risk_names_and_explicit_asof_flags_are_excluded(row):
    assert new_entry_exclusion(row)


@pytest.mark.parametrize('row', [
    {}, {'name': None}, {'name': float('nan')}, {'is_st': False}, {'is_st': 'false'},
    {'name': 'STANLEY'}, {'name': '正常股份', 'list_status': 'D'},
    {'name': '正常股份', 'delist_date': '20261001'},
])
def test_unknown_or_today_delisting_does_not_delete_a_historical_sample(row):
    assert new_entry_exclusion(row) == ''


def test_old_plan_conversion_and_buy_gate_reject_st_without_fetching():
    assert _to_backtest_row({'code': '000004', 'name': '*ST国华'}) is None
    engine = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy='strict'))
    plan = pd.Series({'代码': '000004', '名称': '*ST国华', '目标价': 10})
    engine._prefetch_entry_data(pd.DataFrame([plan]), '20260714')
    assert engine._minute_frames == {}
    assert engine._candidate_evidence_samples == set()
    assert engine._check_buy_conditions(plan, '20260714', '000004', '*ST国华') == (False, 0)
    assert engine.entry_attempts[-1]['reason_code'] == 'restricted_stock'


def test_peer_selection_filters_asof_st_but_preserves_pre_st_history(tmp_path):
    path = tmp_path / 'factor.duckdb'
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE factor_stock_wide(code VARCHAR, name VARCHAR, trade_date VARCHAR, resonance_sectors VARCHAR, total_score DOUBLE)')
    con.executemany('INSERT INTO factor_stock_wide VALUES (?, ?, ?, ?, ?)', [
        ('000004', '国华股份', '20260710', '机器人', 100),
        ('000004', '*ST国华', '20260713', '机器人', 100),
        ('002808', '*ST恒久', '20260713', '机器人', 99),
        ('000002', '正常股份', '20260713', '机器人', 80),
    ])
    con.close()
    assert BacktestEngine._load_sector_peer_codes('20260713', {'000001': {'机器人'}}, duckdb_path=path) == {'000001': ['000004']}
    assert BacktestEngine._load_sector_peer_codes('20260714', {'000001': {'机器人'}}, duckdb_path=path) == {'000001': ['000002']}


def test_peer_warning_survives_account_checkpoint():
    engine = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy='strict'))
    engine._sector_peer_failures[('20260714', '000004')] = '分钟数据为空'
    restored = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy='strict'))
    restored.import_state(engine.export_state())
    warning = restored.replay_data_quality()['optional_evidence_warnings'][0]
    assert warning['scope'] == 'sector_peer'
    assert warning['code'] == '000004'


def test_prefetch_excludes_st_candidates_and_asof_peers(tmp_path, monkeypatch):
    import scripts.prefetch_strategy_minutes as prefetch
    path = tmp_path / 'factors.duckdb'
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE stock_daily_silver(trade_date VARCHAR)')
    con.execute("INSERT INTO stock_daily_silver VALUES ('20260713'), ('20260714')")
    con.execute('CREATE TABLE factor_stock_wide(code VARCHAR, ts_code VARCHAR, name VARCHAR, trade_date VARCHAR, resonance_sectors VARCHAR, total_score DOUBLE, computed_at VARCHAR)')
    con.executemany('INSERT INTO factor_stock_wide VALUES (?, ?, ?, ?, ?, ?, ?)', [
        ('000001', '000001.SZ', '正常', '20260713', '机器人', 80, ''),
        ('000002', '000002.SZ', '参考', '20260713', '机器人', 70, ''),
        ('000004', '000004.SZ', '*ST国华', '20260713', '机器人', 99, ''),
        ('002808', '002808.SZ', '*ST恒久', '20260713', '机器人', 100, ''),
    ])
    con.close()
    monkeypatch.setattr(prefetch, 'FACTOR_DB_PATH', path)
    monkeypatch.setattr(prefetch, '_strategy_candidates', lambda *args: {
        ('20260713', '000001'): {'name': '正常', 'resonance_sectors': '机器人'},
        ('20260713', '000004'): {'name': '*ST国华', 'resonance_sectors': '机器人'}})
    minutes, auctions, audit = prefetch.build_requirements([], '20260713', '20260713')
    assert minutes == {('20260714', '000001.SZ'), ('20260714', '000002.SZ')}
    assert auctions == {('20260714', '000001.SZ')}
    assert audit['excluded_risk_candidates'] == 1


@pytest.mark.parametrize('candidate_missing', [False, True])
def test_missing_peer_is_warning_but_missing_candidate_still_blocks(tmp_path, monkeypatch, candidate_missing):
    import core.data as data_module
    import scripts.prefetch_strategy_minutes as prefetch
    import backtest.trade_calendar as calendar

    date = '20260714'
    candidate = (date, '000001.SZ')
    peer = (date, '000004.SZ')
    def minutes(code, day, **kwargs):
        if candidate_missing or code == peer[1]:
            return pd.DataFrame()
        times = [f'{m // 60:02d}:{m % 60:02d}:00'
                 for start in (570, 780) for m in range(start + 1, start + 121)]
        return pd.DataFrame({'trade_date': day, 'time': times, 'close': 10, 'volume': 100})
    dm = SimpleNamespace(get_stock_tick=minutes, get_auction_data=lambda *args: {'open': 10})
    monkeypatch.setattr(data_module, 'DataManager', lambda *args, **kwargs: dm)
    monkeypatch.setattr(calendar, 'TradeCalendar', lambda: SimpleNamespace(is_real=True, prev=lambda day: '20260713'))
    monkeypatch.setattr(prefetch, 'build_requirements', lambda *a, **kw: ({candidate, peer}, {candidate}, {}))
    monkeypatch.setattr(prefetch, '_minute_cached', lambda *args: False)
    service = PaperTradingService(HoldingRepository(tmp_path / 'portfolio.sqlite'))
    progress = []
    result = service.prefetch_replay_minutes(date, date, progress=progress.append)
    assert result['ok'] is not candidate_missing
    assert result['minute_failed'] == int(candidate_missing)
    assert result['sector_peer_failed'] == 1
    assert result['optional_evidence_warnings'][0]['scope'] == 'sector_peer'
    assert result['optional_evidence_warnings'][0]['trade_date'] == date
    assert all(row['scope'] == 'candidate' for row in result['evidence_failures'])
    assert progress[-1]['sector_peer_failed'] == 1
