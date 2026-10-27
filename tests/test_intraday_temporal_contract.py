from dataclasses import replace
from datetime import datetime

import pandas as pd
import pytest

from backtest.minute_entry import ENTRY_ACCELERATION, ENTRY_CONTINUATION, ENTRY_WEAK, EntryDecision, MinuteEntryEvaluator, normalize_minute_bars
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.snapshot_signal_service import SnapshotSignalService
from core.realtime.trading_session import realtime_session_status
from core.signals.production_rules import acceleration_eligibility, production_candidates
from test_snapshot_primary import minutes, primary, ticks


def test_session_cumulative_low_is_not_a_post_structure_break():
    frame = ticks()
    frame['low_price'] = 9.0
    decision, fallback = primary(snapshots=frame)
    assert not fallback and decision.status == 'confirmed'
    checks = decision.condition_checks['latest']['checks']
    assert checks['session_low_used'] is False
    assert checks['post_structure_low'] > checks['protection_price']


def test_real_break_survives_hot_window_eviction_and_worker_restart():
    backend = MemoryStateBackend()
    service = SnapshotSignalService(state_backend=backend)
    key = ('20261022', '000001', ENTRY_WEAK)
    bars = normalize_minute_bars(minutes())
    frame = ticks()
    frame.loc[0, 'last_price'] = 9.0
    assert service._protection(key, bars, frame, '09:35:00', 9.7)[0] is False
    assert service._protection(key, bars, ticks(), '09:35:00', 9.7)[1]['first_break_at'] == '09:36:00'
    assert service._protection(key, bars, ticks(), '09:35:00', 9.7)[0] is False
    restarted = SnapshotSignalService(state_backend=backend)
    assert restarted._protection(key, bars, ticks(), '09:35:00', 9.7)[0] is False
    # Another date and another opening window do not inherit the veto.
    assert restarted._protection(('20261023', '000001', ENTRY_WEAK), bars, ticks(), '09:35:00', 9.7)[0]
    assert service._protection(key, bars, ticks(), '09:36:00', 9.7)[0]


def test_pre_structure_low_does_not_cancel_later_signal():
    frame = ticks()
    early = frame.iloc[0].to_dict()
    early.update(time='09:34:00', received_at='2026-10-22T09:34:00', last_price=9.0)
    decision, _ = primary(snapshots=pd.concat([pd.DataFrame([early]), frame], ignore_index=True))
    assert decision.status == 'confirmed'


@pytest.mark.parametrize('mode,count,gap', [(ENTRY_ACCELERATION, 1, .06), (ENTRY_CONTINUATION, 3, .02), (ENTRY_WEAK, 5, -.01)])
@pytest.mark.parametrize('after', [False, True])
def test_protection_boundary_follows_each_configured_opening_window(mode, count, gap, after):
    frame = ticks()
    frame['open_price'] = 10 * (1 + gap)
    low = frame.iloc[0].to_dict()
    low_time = f'09:{30 + count}:{30 if after else 0:02d}'
    low.update(time=low_time, received_at='2026-10-22T' + low_time, last_price=9.0)
    decision, fallback = primary(mode=mode, open_gap=gap, is_leader=True,
        snapshots=pd.concat([pd.DataFrame([low]), frame], ignore_index=True))
    assert not fallback
    checks = decision.condition_checks['latest']['checks']
    assert checks['protection_window_start'] == f'09:{30 + count}:00'
    assert checks['protection'] is not after


@pytest.mark.parametrize('kind', ['minute_price_point', 'unknown'])
def test_price_points_cannot_supply_opening_structure_even_when_drawn_as_candles(kind):
    bars = minutes()
    bars['bar_kind'] = kind
    decision, fallback = primary(minute_bars=bars)
    assert not fallback and decision.data_status == 'snapshot_structure_pending'
    assert 'OHLC' in decision.reason
    following = bars.iloc[-1].to_dict()
    following.update(time='09:36:00', close=10.05)
    result = MinuteEntryEvaluator().evaluate(mode=ENTRY_WEAK,
        bars=pd.concat([bars, pd.DataFrame([following])], ignore_index=True), open_gap=-.01, prev_close=10)
    assert result.data_status == 'missing_opening_ohlc'
    assert not result.confirm_time


def test_legacy_eltdx_flat_price_points_are_not_silently_upgraded():
    bars = minutes().assign(source='eltdx', amount_is_estimated=True)
    normalized = normalize_minute_bars(bars)
    assert normalized['bar_kind'].eq('minute_price_point').all()
    assert not normalize_minute_bars(normalized)['ohlc_evidence'].any()


def test_signal_times_distinguish_attempt_confirmation_invalidation_and_fill():
    pending = EntryDecision('quality_pending', confirm_time='09:36:00', initial_confirm_time='09:35:00')
    assert pending.trigger_at == '09:35:00'
    assert not pending.confirm_time and not pending.formal_confirmed_at
    cancelled = EntryDecision('cancelled', confirm_time='09:38:00')
    assert cancelled.invalidated_at == '09:38:00'
    assert not cancelled.trigger_at and not cancelled.confirm_time
    confirmed = EntryDecision('confirmed', confirm_time='09:39:00', initial_confirm_time='09:36:00')
    assert confirmed.trigger_at == '09:36:00' and confirmed.formal_confirmed_at == '09:39:00'
    assert not confirmed.simulated_entry_at
    fill = replace(confirmed, status='filled', entry_time='09:39:03', entry_price=10)
    assert fill.formal_confirmed_at == '09:39:00' and fill.simulated_entry_at == '09:39:03'
    denied = replace(confirmed, status='observing', reason='price quality')
    assert not denied.confirm_time and not denied.formal_confirmed_at


def test_acceleration_qualification_remains_stricter_than_pool_membership():
    assert not acceleration_eligibility({'pool_type': '连板龙头'})['eligible']
    assert acceleration_eligibility({'pool_type': '核心龙头'})['eligible']
    assert acceleration_eligibility({'metrics': {'stk_kpl_leader_quality': 65}})['eligible']
    assert acceleration_eligibility({'metrics': {'stk_sector_mainline_score': 75, 'stk_board_position': 60}})['eligible']
    assert not acceleration_eligibility({'metrics': {'stk_sector_mainline_score': 75, 'stk_board_position': 59}})['eligible']


def test_overlapping_candidate_keeps_decision_execution_not_inherited_leader_defaults():
    inputs = {'leader_pools': {'10': {'rows': [{'code': '000001', 'pool_type': '核心龙头',
        'strategy_execution': {'allowed_entry_modes': ['acceleration_only']}}]}},
        'candidates': [{'code': '000001', 'strategy_execution': {'allowed_entry_modes': ['weak_only']}}]}
    row, = production_candidates(inputs)
    assert row['strategy_id'] == row['execution_pool'] == 'decision_pool'
    assert row['execution_sources'] == ['leader_pool', 'decision_pool']
    assert row['strategy_execution']['allowed_entry_modes'] == ['weak_only']
    assert row['acceleration_eligibility']['eligible']


@pytest.mark.parametrize('at,opened,is_break', [
    ('11:30:00', True, False), ('11:30:01', False, True),
    ('12:00:00', False, True), ('12:59:59', False, True), ('13:00:00', True, False),
])
def test_lunch_break_and_same_day_next_open(at, opened, is_break):
    calendar = type('Calendar', (), {'is_trade_date': lambda self, day: True,
                                   'next': lambda self, day: '20261028'})()
    status = realtime_session_status(datetime.fromisoformat('2026-10-27T' + at), calendar=calendar)
    assert status['is_open'] is opened and status['session_break'] is is_break
    if is_break:
        assert status['next_open_at'].startswith('2026-10-27T13:00:00')


def test_runtime_does_not_read_or_evaluate_market_during_lunch(tmp_path, monkeypatch):
    import core.realtime.strategy_runtime as runtime
    from core.operations.ledger import TradingLedger
    worker = runtime.StrategyRuntime(backend=MemoryStateBackend(),
        ledger=TradingLedger(tmp_path / 'ledger.sqlite3'), clock=lambda: datetime(2026, 10, 27, 12, 0))
    worker._load = lambda *a: pytest.fail('No market read during lunch')
    assert worker.run_once() is None
    assert worker.backend.get_json(runtime.HEALTH_KEY)['session_break'] is True


def test_notification_requires_formal_state_and_health_but_not_simulated_fill(tmp_path):
    from core.notifications.outbox import RealtimeNotificationOutbox
    from core.operations.ledger import TradingLedger
    queue = RealtimeNotificationOutbox(TradingLedger(tmp_path / 'ledger.sqlite3'))
    row = {'code': '000001', 'confirm_status': 'confirmed', 'entry_mode': ENTRY_WEAK,
           'confirm_time': '09:36:03', 'formal_confirmed_at': '09:36:03', 'entry_price': None}
    payload = {'market_date': '20261022', 'rows': [row], 'execution_health': {'ready': True}}
    assert queue.enqueue({**payload, 'execution_health': {'ready': False}}) == 0
    assert queue.enqueue({**payload, 'rows': [{**row, 'formal_confirmed_at': ''}]}) == 0
    assert queue.enqueue(payload) == 1
    assert queue.enqueue(payload) == 0


def test_ohlcv_collector_adapter_filters_requested_day_and_keeps_true_high_low():
    from core.data.providers.eltdx_provider import EltdxProvider
    provider = EltdxProvider()
    calls = []
    def kline(code, period, count):
        calls.append((code, period, count))
        return pd.DataFrame([
            {'datetime': '2026-10-21T09:31:00', 'open': 9.7, 'high': 10, 'low': 9.6, 'close': 9.9, 'vol': 100},
            {'datetime': '2026-10-22T09:31:00', 'open': 9.9, 'high': 10.2, 'low': 9.8, 'close': 10, 'vol': 120},
        ]).assign(source='eltdx', bar_kind='minute_ohlcv', amount_is_estimated=False)
    provider.get_kline = kline
    frame = provider.get_minute_ohlcv('000001', '20261022')
    assert calls == [('000001', '1min', 800)]
    assert len(frame) == 1 and frame.iloc[0]['time'] == '09:31:00'
    assert frame.iloc[0]['high'] == 10.2 and frame.iloc[0]['low'] == 9.8
    assert normalize_minute_bars(frame)['ohlc_evidence'].all()
    assert provider.get_minute_ohlcv('000001', '20261020').empty


def test_collector_archives_real_candles_without_calling_price_point_interface():
    from types import SimpleNamespace
    from core.realtime.eltdx_collector import EltdxQuoteCollector
    from core.realtime.minute_cache import RealtimeMinuteCache
    collector = EltdxQuoteCollector.__new__(EltdxQuoteCollector)
    backend = MemoryStateBackend()
    calls, archived = [], []
    bars = minutes().assign(bar_kind='minute_ohlcv')
    collector.clock = lambda: datetime(2026, 10, 22, 9, 36)
    collector.provider = SimpleNamespace(
        get_minute_ohlcv=lambda code, date: (calls.append((code, date)) or bars),
        get_minute_bars=lambda *a: pytest.fail('Price points cannot supply opening candles'))
    collector.quote_cache = SimpleNamespace(backend=backend)
    collector.minute_cache = RealtimeMinuteCache(backend)
    collector.journal = SimpleNamespace(append=lambda *args: archived.append(args))
    collector._sync_minutes(['000001'])
    assert calls == [('000001', '20261022')]
    assert archived[0][0] == 'minutes'
    assert all(row['bar_kind'] == 'minute_ohlcv' for row in archived[0][2][0]['bars'])
    assert collector.minute_cache.read('20261022', '000001')['bar_kind'].eq('minute_ohlcv').all()


def test_incremental_evaluation_can_avoid_unrelated_peers_without_dropping_sector_dependencies():
    from core.realtime.strategy_runtime import affected_codes
    rows = [{'code': '000001', 'sector_names': ['A']}, {'code': '000002', 'sector_names': ['B']}]
    plan = {'sectors': [{'name': 'A', 'code': '885001.TI', 'members': ['600001']},
                        {'name': 'B', 'code': '885002.TI', 'members': ['600002']}]}
    assert affected_codes(rows, plan, {'000001'}, peer_dependencies=False) == {'000001'}
    assert affected_codes(rows, plan, {'885002'}, peer_dependencies=False) == {'000002'}
    assert affected_codes(rows, plan, {'600001'}, peer_dependencies=False) == {'000001'}
    assert affected_codes(rows, plan, {'000001'}, peer_dependencies=True) == {'000001', '000002'}


def test_optional_board_scoring_cannot_reappear_as_mandatory_health_gate():
    from core.operations.health import gate_realtime_payload
    from core.realtime.trading_session import shanghai_now
    now = shanghai_now(datetime(2026, 10, 27, 9, 43))
    collector = {'storage': 'redis', 'trade_date': '20261027', 'collector': {
        'ok': True, 'lease_owned': True, 'updated_at': '2026-10-27T09:42:59',
        'session_started_at': '2026-10-27T09:30:00'}}
    row = {'code': '000001', 'confirm_status': 'confirmed', 'confirm_time': '09:42:58',
           'formal_confirmed_at': '09:42:58', 'received_at': '2026-10-27T09:42:59',
           'entry_parameters': {'confirmation_policy': {'require_sector': False}}}
    gate_realtime_payload({'market_date': '20261027', 'rows': [row]}, collector=collector, now=now, require_shared=True)
    assert row['confirm_status'] == 'confirmed'
    row['entry_parameters']['confirmation_policy']['require_sector'] = True
    row['condition_checks'] = {'sector_required': False}
    gate_realtime_payload({'market_date': '20261027', 'rows': [row]}, collector=collector, now=now, require_shared=True)
    assert row['confirm_status'] == 'observe'
    assert not row['formal_confirmed_at'] and not row['confirm_time']
    assert row['rule_confirmed_at'] == '09:42:58'
    assert '板块' in row['health_gate_reason']
