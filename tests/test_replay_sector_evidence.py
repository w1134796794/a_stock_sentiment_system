from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.evidence_policy import ReplayEvidenceBlocked
from backtest.minute_entry import EntryDecision
from backtest.plan_source import _to_backtest_row
from core.utils.sector_names import normalize_sector_names
from scripts.prefetch_strategy_minutes import _first_sector


@pytest.mark.parametrize('value', [
    ['机器人概念', '汽车零部件'],
    '["机器人概念", "汽车零部件"]',
    "['机器人概念', '汽车零部件']",
    '机器人概念,汽车零部件',
    '机器人概念；汽车零部件，机器人概念',
])
def test_sector_names_survive_artifact_and_csv_formats(value):
    assert normalize_sector_names(value) == ['机器人概念', '汽车零部件']
    assert _first_sector(value) == '机器人概念'
    row = _to_backtest_row({'code': '002536', 'name': '飞龙股份', '共振板块': value})
    assert row['共振板块'] == '机器人概念,汽车零部件'
    assert row['所属板块'] == '机器人概念,汽车零部件'


@pytest.mark.parametrize('value', [None, float('nan'), '', 'nan', '[broken', {'sector': '机器人'}, 2])
def test_invalid_sector_payloads_are_not_guessed(value):
    assert normalize_sector_names(value) == []


def test_peer_mapping_is_exact_unique_and_uses_only_prior_session(tmp_path):
    path = tmp_path / 'factors.duckdb'
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE factor_stock_wide(code VARCHAR, trade_date VARCHAR, resonance_sectors VARCHAR, total_score DOUBLE)')
    con.executemany('INSERT INTO factor_stock_wide VALUES (?, ?, ?, ?)', [
        ('002536', '20260703', '机器人概念', 80),
        ('002472', '20260703', '机器人概念,汽车零部件', 79),
        ('002472', '20260703', '机器人概念,汽车零部件', 78),
        ('002850', '20260703', "['机器人概念']", 77),
        ('000003', '20260703', '机器人概念增强', 99),
        ('000004', '20260706', '机器人概念', 100),
    ])
    con.close()
    assert BacktestEngine._load_sector_peer_codes('20260706', {'002536': {'机器人概念'}}, duckdb_path=path) == {
        '002536': ['002472', '002850']}


def test_prefetch_covers_all_candidate_sectors_not_only_the_first(tmp_path, monkeypatch):
    import scripts.prefetch_strategy_minutes as prefetch
    path = tmp_path / 'factors.duckdb'
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE stock_daily_silver(trade_date VARCHAR)')
    con.execute("INSERT INTO stock_daily_silver VALUES ('20260703'), ('20260706')")
    con.execute('CREATE TABLE factor_stock_wide(code VARCHAR, ts_code VARCHAR, trade_date VARCHAR, resonance_sectors VARCHAR, total_score DOUBLE, computed_at VARCHAR)')
    con.executemany('INSERT INTO factor_stock_wide VALUES (?, ?, ?, ?, ?, ?)', [
        ('002536', '002536.SZ', '20260703', '机器人概念,汽车零部件', 80, ''),
        ('002472', '002472.SZ', '20260703', '机器人概念', 79, ''),
        ('002850', '002850.SZ', '20260703', '汽车零部件', 77, ''),
    ])
    con.close()
    monkeypatch.setattr(prefetch, 'FACTOR_DB_PATH', path)
    monkeypatch.setattr(prefetch, '_strategy_candidates', lambda *args: {
        ('20260703', '002536'): {'resonance_sectors': "['机器人概念', '汽车零部件']"}})
    minutes, auctions, _ = prefetch.build_requirements([], '20260703', '20260703', peer_count=8)
    assert minutes == {('20260706', code) for code in ('002536.SZ', '002472.SZ', '002850.SZ')}
    assert auctions == {('20260706', '002536.SZ')}


def minute_frame():
    times = [f'{m // 60:02d}:{m % 60:02d}:00'
             for start in (570, 780) for m in range(start + 1, start + 121)]
    return pd.DataFrame({'trade_date': '20260706', 'time': times, 'close': 9.0,
                         'open': 9.0, 'high': 9.0, 'low': 9.0, 'volume': 100.0, 'pre_close': 10.0})


def candidate():
    return pd.Series({'代码': '002536', '名称': '飞龙股份', '目标价': 10,
        '共振板块': "['机器人概念', '汽车零部件', '新能源汽车']", '策略ID': 'weak_to_strong'})


def test_old_csv_plan_preloads_peers_and_checker_does_not_use_future_minutes():
    frame = minute_frame()
    frame.loc[frame['time'] >= '11:00:00', ['open', 'high', 'low', 'close']] = 20
    dm = SimpleNamespace(get_stock_tick=lambda *a: frame.copy(), get_auction_data=lambda *a: {})
    engine = BacktestEngine(dm, BacktestConfig(replay_missing_evidence_policy='strict'))
    def load(date, sectors):
        assert sectors == {'002536': {'机器人概念', '汽车零部件', '新能源汽车'}}
        return {'002536': ['002472', '002850']}
    engine._load_sector_peer_codes = load
    engine._day_plans = pd.DataFrame([candidate()])
    engine._prefetch_entry_data(engine._day_plans, '20260706')
    checker = engine._sector_sync_checker(candidate(), '20260706', '002536')
    assert checker('09:36:00') is False
    assert checker('11:00:00') is True
    assert engine._sector_evidence[('20260706', '002536')]['observed_count'] == 2


def test_selected_mode_records_its_own_sector_time_and_does_not_alias_later_observations():
    frame = minute_frame()
    frame.loc[:, ['open', 'high', 'low', 'close']] = 11
    engine = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy='strict'))
    engine._day_plans = pd.DataFrame([candidate()])
    engine._sector_peers = {'002536': ['002472', '002850']}
    for code in ('002472', '002850'):
        engine._minute_frames[('20260706', code)] = frame.copy()
    checker = engine._sector_sync_checker(candidate(), '20260706', '002536')
    assert checker('09:56:00') is True
    assert checker('10:59:00') is True  # A later independent mode is evaluated before selection.
    decision = EntryDecision(status='filled', confirm_time='09:56:00', entry_time='09:57:00', sector_confirmed=True)
    engine._record_entry_attempt(candidate(), '20260706', '002536', '飞龙股份', decision, entry_mode='limit_pullback')
    saved = engine.entry_attempts[-1]['sector_evidence']
    assert saved['at_time'] == '09:56:00' and saved['positive_count'] == 2
    assert checker('11:00:00') is True
    engine._sector_evidence_snapshots[('20260706', '002536')]['09:56:00']['peer_codes'].append('999999')
    assert saved['at_time'] == '09:56:00' and '999999' not in saved['peer_codes']


@pytest.mark.parametrize('policy', ['strict', 'skip_candidates'])
def test_missing_sector_details_distinguish_mapping_from_minute_missing(policy):
    engine = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy=policy))
    engine._day_plans = pd.DataFrame([candidate()])
    assert engine._sector_sync_checker(candidate(), '20260706', '002536')('09:36:00') is None
    decision = EntryDecision(status='data_insufficient', data_status='missing_sector',
        reason='缺少真实板块指数或成分股宽度，保持观察')
    if policy == 'strict':
        with pytest.raises(ReplayEvidenceBlocked) as exc:
            engine._record_entry_attempt(candidate(), '20260706', '002536', '飞龙股份', decision, entry_mode='weak_to_strong')
        failure = exc.value.details[0]
    else:
        engine._record_entry_attempt(candidate(), '20260706', '002536', '飞龙股份', decision, entry_mode='weak_to_strong')
        failure = engine.replay_data_quality()['skipped_candidates'][0]
    assert '同板块样本0只' in failure['reason']
    assert failure['sector_evidence']['peer_codes'] == []
    assert engine.entry_attempts[-1]['reason_code'] == 'missing_sector_confirmation'


def test_missing_peer_minutes_include_the_stock_and_specific_reason():
    engine = BacktestEngine(SimpleNamespace(), BacktestConfig(replay_missing_evidence_policy='skip_candidates'))
    engine._day_plans = pd.DataFrame([candidate()])
    engine._sector_peers = {'002536': ['002472', '002850']}
    engine._sector_peer_failures[('20260706', '002472')] = '缺少 20 个交易分钟'
    assert engine._sector_sync_checker(candidate(), '20260706', '002536')('09:36:00') is None
    engine._record_entry_attempt(candidate(), '20260706', '002536', '飞龙股份',
        EntryDecision(status='data_insufficient', data_status='missing_sector',
                      reason='缺少真实板块指数或成分股宽度，保持观察'), entry_mode='weak_to_strong')
    reason = engine.replay_data_quality()['skipped_candidates'][0]['reason']
    assert '002472: 缺少 20 个交易分钟' in reason
    assert '002850:' in reason
