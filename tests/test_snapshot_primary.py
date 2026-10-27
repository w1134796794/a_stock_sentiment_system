from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.confirmation_quality import ConfirmationPolicy
from backtest.minute_entry import ENTRY_WEAK, ENTRY_CONTINUATION, ENTRY_ACCELERATION, MinuteEntryEvaluator
from core.realtime.entry_signal_service import RealtimeEntrySignalService
from core.realtime.snapshot_signal_service import SnapshotSignalService


NOW = datetime(2026, 10, 22, 9, 36, 3)


def minutes():
    return pd.DataFrame([dict(time=f"09:{m}:00", open=9.9, high=10.0, low=9.8,
        close=9.95, volume=10000, amount=99500) for m in range(31, 36)])


def ticks():
    return pd.DataFrame([dict(code="000001", date="20261022", time=f"09:36:0{i * 3}",
        received_at=(NOW - timedelta(seconds=3 - i * 3)).isoformat(), source="eltdx_batch",
        last_price=10.05 + i * .01, ask1=10.06 + i * .01, delta_volume=10,
        vol_hand=20000 + i * 10, amount_yuan=20000000 + i * 10200,
        open_price=9.9, pre_close=10, quality_ok=True) for i in range(2)])


def setup_service():
    evaluator = MinuteEntryEvaluator(confirmation_policy=ConfirmationPolicy(mode="volume_price"))
    return SnapshotSignalService(clock=lambda: NOW), evaluator


def primary(**overrides):
    service, evaluator = setup_service()
    args = dict(code="000001", trade_date="20261022", mode=ENTRY_WEAK,
        minute_bars=minutes(), snapshots=ticks(), prev_close=10, open_gap=-.01,
        sector_sync=lambda at: True, is_leader=False, limit_price=11,
        evaluator=evaluator, previous_amount=100000000,
        expected_amount_fraction=lambda at: .2, profile_samples=200)
    args.update(overrides)
    return service.evaluate_primary(**args)


def test_snapshots_confirm_without_any_post_opening_minute_or_minute_confirmation():
    decision, fallback = primary()
    assert not fallback
    assert decision.status == "confirmed"
    assert decision.confirm_time == "09:36:03"
    assert decision.entry_time == "" and decision.entry_price == 0
    checks = decision.condition_checks["confirmation"]["checks"]
    assert checks["basis"] == "eltdx_snapshot_primary_v2_temporal_protection"
    assert checks["vwap_value"] == pytest.approx(20010200 / 2001000)
    assert checks["local_volume_ratio"] == pytest.approx(2)
    assert decision.amount_pace == pytest.approx(1.00051)


def test_acceleration_can_confirm_before_0935_but_weak_still_needs_five_minutes():
    now = datetime(2026, 10, 22, 9, 32, 3)
    service = SnapshotSignalService(clock=lambda: now)
    evaluator = MinuteEntryEvaluator(acceleration_opening_minutes=1,
        confirmation_policy=ConfirmationPolicy(mode="volume_price"))
    bars = pd.DataFrame([dict(time="09:31:00", open=10.6, high=10.65, low=10.5,
                              close=10.6, volume=10000, amount=106000)])
    snapshots = ticks()
    for i in range(2):
        snapshots.loc[i, "time"] = f"09:32:0{i * 3}"
        snapshots.loc[i, "received_at"] = (now - timedelta(seconds=3-i*3)).isoformat()
        snapshots.loc[i, "last_price"] = 10.7 + i * .01
        snapshots.loc[i, "ask1"] = 10.71 + i * .01
        snapshots.loc[i, "open_price"] = 10.6
        snapshots.loc[i, "amount_yuan"] = 21300000 + i * 10700
    args = dict(code="000001", trade_date="20261022", minute_bars=bars, snapshots=snapshots,
                prev_close=10, sector_sync=lambda at: True, is_leader=True, limit_price=11,
                evaluator=evaluator)
    decision, fallback = service.evaluate_primary(mode=ENTRY_ACCELERATION, open_gap=.06, **args)
    assert not fallback
    assert decision.status == "confirmed"
    assert decision.confirm_time == "09:32:03"
    assert decision.condition_checks["latest"]["checks"]["opening_minutes_required"] == 1
    # Same short minute history is insufficient for weak-to-strong.
    snapshots["open_price"] = 9.9
    decision, _ = service.evaluate_primary(mode=ENTRY_WEAK, open_gap=-.01, **args)
    assert decision.data_status == "snapshot_structure_pending"
    assert decision.condition_checks["latest"]["checks"]["opening_minutes_required"] == 5


@pytest.mark.parametrize("change,reason", [
    ("missing", "snapshot_sequence_missing"), ("stale", "snapshot_stale"),
    ("gap", "snapshot_gap"), ("identity", "snapshot_identity_mismatch"),
    ("fields", "snapshot_cumulative_fields_missing"), ("regressed", "snapshot_cumulative_regressed"),
    ("invalid", "snapshot_invalid"), ("reference", "snapshot_session_reference_mismatch"),
    ("future", "snapshot_stale"),
])
def test_unavailable_snapshots_explicitly_request_minute_fallback(change, reason):
    frame = ticks()
    if change == "missing":
        frame = frame.iloc[:1]
    elif change == "stale":
        frame["received_at"] = "2026-10-22T09:35:00"
    elif change == "gap":
        frame.loc[0, "time"] = "09:35:30"
    elif change == "identity":
        frame["code"] = "000002"
    elif change == "fields":
        frame = frame.drop(columns="amount_yuan")
    elif change == "regressed":
        frame.loc[1, "vol_hand"] = 19999
    elif change == "invalid":
        frame.loc[1, "quality_ok"] = False
    elif change == "reference":
        frame["open_price"] = 9.8
    elif change == "future":
        frame["time"] = ["09:37:00", "09:37:03"]
    decision, fallback = primary(snapshots=frame)
    assert decision is None and fallback == reason


@pytest.mark.parametrize("field,value,reason", [
    ("sector_sync", lambda at: None, "板块"),
    ("sector_sync", lambda at: False, "板块"),
    ("expected_amount_fraction", None, "进度模型"),
    ("previous_amount", 1000000000, "量能进度"),
    ("open_gap", -.04, "参考"),
])
def test_negative_snapshot_conditions_are_not_replaced_by_minute_fallback(field, value, reason):
    decision, fallback = primary(**{field: value})
    if field == "open_gap":
        assert decision is None and "reference" in fallback
    else:
        assert decision.status not in {"confirmed", "filled"}
        assert not fallback and reason in decision.reason


@pytest.mark.parametrize("change", ["weak_break", "ask_missing", "vwap", "extension", "no_turnover", "no_local_volume"])
def test_snapshot_protection_price_and_volume_checks_remain_required(change):
    frame, bars = ticks(), minutes()
    if change == "weak_break":
        bars = pd.concat([bars, pd.DataFrame([dict(time="09:35:30", open=9.9, high=10, low=9.5,
            close=9.95, volume=10000, amount=99500)])], ignore_index=True)
    elif change == "ask_missing":
        frame["ask1"] = 0
    elif change == "vwap":
        frame["amount_yuan"] *= 1.1
    elif change == "extension":
        frame["ask1"] = 10.5
    elif change == "no_turnover":
        frame.loc[1, ["vol_hand", "amount_yuan"]] = frame.loc[0, ["vol_hand", "amount_yuan"]]
    elif change == "no_local_volume":
        frame.loc[1, "vol_hand"] = 20001
    decision, fallback = primary(snapshots=frame, minute_bars=bars)
    assert not fallback
    assert decision.status not in {"confirmed", "filled"}


def test_primary_never_uses_incomplete_or_future_opening_structure():
    decision, fallback = primary(minute_bars=minutes().iloc[:4])
    assert decision.data_status == "snapshot_structure_pending" and not fallback
    frame = ticks()
    frame["time"] = ["09:35:00", "09:35:03"]
    frame["received_at"] = ["2026-10-22T09:35:00", "2026-10-22T09:35:03"]
    service = SnapshotSignalService(clock=lambda: datetime(2026, 10, 22, 9, 35, 3))
    _, evaluator = setup_service()
    decision, fallback = service.evaluate_primary(code="000001", trade_date="20261022", mode=ENTRY_WEAK,
        minute_bars=minutes(), snapshots=frame, prev_close=10, open_gap=-.01,
        sector_sync=lambda at: True, is_leader=False, limit_price=11, evaluator=evaluator)
    assert decision.data_status == "snapshot_structure_pending" and not fallback


def entry_service(monkeypatch, snapshot_frame=None):
    snapshots, evaluator = setup_service()
    service = RealtimeEntrySignalService(evaluator=evaluator, snapshot_signal_service=snapshots,
        amount_profile_repository=SimpleNamespace(available=True, expected_fraction=lambda *a: (.2, 200)),
        signal_stats_repository=SimpleNamespace(get=lambda *a, **kw: {}))
    service._snapshot_frame = lambda *args: ticks() if snapshot_frame is None else snapshot_frame
    service._sector_checker = lambda *args: (lambda at: True, {"data_completeness": 1})
    return service


def evaluate_entry(service, *, allowed=None, stale=False):
    row = {"code": "000001", "allowed_entry_modes": allowed or ["weak_to_strong"]}
    quote = dict(code="000001", date="20261022", time="09:36:03", received_at=NOW.isoformat(),
                 open_price=9.9, pre_close=10, last_price=10.06, is_stale=stale)
    return service._evaluate_entry(row, quote, minutes(), {}, {},
        {"close": 10, "amount_yuan": 100000000}, "20261022", [row], {})


def test_real_entry_adapter_uses_primary_before_minute_evaluator(monkeypatch):
    service = entry_service(monkeypatch)
    service.evaluator.evaluate = lambda **kw: pytest.fail("Must not wait for minute confirmation")
    signal = evaluate_entry(service)
    assert signal["signal_status"] == "confirmed"
    assert signal["trigger_source"] == "eltdx_snapshot"
    assert signal["evaluation_timing"]["minute_decision_ms"] == 0
    assert not signal["condition_checks"]["fallback_reason"]


def test_absent_snapshots_run_shared_minute_evaluator(monkeypatch):
    service = entry_service(monkeypatch, pd.DataFrame())
    calls = []
    original = service.evaluator.evaluate
    service.evaluator.evaluate = lambda **kw: (calls.append(kw) or original(**kw))
    signal = evaluate_entry(service)
    assert len(calls) == 1
    assert signal["trigger_source"] == "minute_fallback"
    assert signal["condition_checks"]["fallback_reason"] == "snapshot_sequence_missing"


def test_valid_but_negative_snapshots_do_not_fall_through_to_minutes(monkeypatch):
    service = entry_service(monkeypatch)
    service._sector_checker = lambda *args: (lambda at: False, {})
    service.evaluator.evaluate = lambda **kw: pytest.fail("Negative snapshot must not be bypassed")
    assert evaluate_entry(service)["signal_status"] == "observe"


def test_strategy_mode_and_outer_quote_freshness_remain_gates(monkeypatch):
    assert evaluate_entry(entry_service(monkeypatch), allowed=["continuation"])["signal_status"] != "confirmed"
    signal = evaluate_entry(entry_service(monkeypatch), stale=True)
    assert signal["data_status"] == "quote_stale" and signal["signal_status"] != "confirmed"


def test_source_survives_leader_and_decision_projection():
    from core.realtime.leader_pool_service import IntradayStrengthService
    from core.realtime.overlay_service import RealtimeOverlayService
    signal = {"signal_status": "confirmed", "trigger_source": "eltdx_snapshot"}
    quote = {"open_price": 9.9, "pre_close": 10, "last_price": 10.06}
    leader = IntradayStrengthService.__new__(IntradayStrengthService)._build_row({"code": "000001"}, quote, signal)
    decision = RealtimeOverlayService.__new__(RealtimeOverlayService)._build_row("20261021", "20261022", {"code": "000001"}, quote, signal)
    assert leader["trigger_source"] == decision["trigger_source"] == "eltdx_snapshot"


def test_snapshot_confirm_enqueues_before_fill_with_its_own_timestamp(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    from core.notifications.outbox import RealtimeNotificationOutbox
    from core.operations.ledger import TradingLedger
    from core.realtime.overlay_service import RealtimeOverlayService
    service = entry_service(monkeypatch)
    signal = evaluate_entry(service)
    quote = {"open_price": 9.9, "pre_close": 10, "last_price": 10.06,
             "received_at": NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()}
    row = RealtimeOverlayService.__new__(RealtimeOverlayService)._build_row(
        "20261021", "20261022", {"code": "000001"}, quote, signal)
    sent = []
    class Notifier:
        def notify_realtime_payload(self, value):
            sent.append(value)
            value["rows"][0].update(notification_status="sent", notification_result={"sent": 1, "all_delivered": True})
    queue = RealtimeNotificationOutbox(TradingLedger(tmp_path / "ledger.sqlite3"),
        clock=lambda: NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp(), notifier_factory=Notifier)
    assert queue.enqueue({"market_date": "20261022", "profile": "production", "rows": [row]}) == 1
    assert queue.process_once()
    assert len(sent) == 1
    assert sent[0]["rows"][0]["entry_price"] is None
    assert sent[0]["rows"][0]["confirm_time"] == "09:36:03"


def test_continuation_auction_and_acceleration_leader_are_not_overridden():
    frame = ticks()
    frame["open_price"] = 10.2
    decision, fallback = primary(mode=ENTRY_CONTINUATION, open_gap=.02, snapshots=frame,
        auction_amount=1000, auction_volume=1, previous_volume=1000)
    assert not fallback and "竞价" in decision.reason
    frame["open_price"] = 10.6
    decision, fallback = primary(mode=ENTRY_ACCELERATION, open_gap=.06, snapshots=frame, is_leader=False)
    assert not fallback and decision.status == "rejected"


def test_primary_execution_waits_for_later_snapshot():
    clock = [NOW]
    service = SnapshotSignalService(clock=lambda: clock[0])
    _, evaluator = setup_service()
    args = dict(code="000001", trade_date="20261022", mode=ENTRY_WEAK,
        minute_bars=minutes(), prev_close=10, open_gap=-.01,
        sector_sync=lambda at: True, is_leader=False, limit_price=11, evaluator=evaluator,
        previous_amount=100000000, expected_amount_fraction=lambda at: .2)
    frame = ticks()
    first, _ = service.evaluate_primary(snapshots=frame, **args)
    again, _ = service.evaluate_primary(snapshots=frame, **args)
    assert first.status == again.status == "confirmed"
    clock[0] += timedelta(seconds=3)
    extra = frame.iloc[-1].to_dict()
    extra.update(time="09:36:06", received_at=clock[0].isoformat(), vol_hand=20020, amount_yuan=20020400)
    frame = pd.concat([frame, pd.DataFrame([extra])], ignore_index=True)
    filled, _ = service.evaluate_primary(snapshots=frame, **args)
    assert filled.status == "filled" and filled.confirm_time == "09:36:03"
    assert filled.entry_time == "09:36:06"
