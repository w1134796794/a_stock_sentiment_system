from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.operations.ledger import TradingLedger
from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
from core.notifications.outbox import RealtimeNotificationOutbox


NOW = datetime(2026, 10, 22, 9, 36, tzinfo=ZoneInfo("Asia/Shanghai"))


def point(code, time, price):
    return dict(code=code, date="20261022", time=time, received_at=f"2026-10-22T{time}+08:00",
                last_price=price, open_price=10, pre_close=11, source="eltdx_batch")


def test_live_checker_reads_shared_closed_pairs_after_restart(tmp_path, monkeypatch):
    import core.realtime.sector_breadth as module
    import core.realtime.sector_quote_minutes as repository
    from core.operations.preopen_evidence import PreopenEvidenceStore
    repo = SectorQuoteMinuteRepository(tmp_path, backend=MemoryStateBackend())
    codes = ["000001", "000002"]
    repo.record_batch([point(c, "09:32:58", 9.8) for c in codes], now=NOW - timedelta(minutes=3))
    repo.record_batch([point(c, "09:35:58", 10.2) for c in codes], now=NOW)
    monkeypatch.setattr(module, "shanghai_now", lambda value=None: (
        value.replace(tzinfo=NOW.tzinfo) if value is not None else NOW))
    monkeypatch.setattr(repository, "SectorQuoteMinuteRepository", lambda: repo)
    monkeypatch.setattr(PreopenEvidenceStore, "load", lambda *a: None)
    # Every new Web provider uses the collector archive, not its own polling history.
    for _ in range(2):
        provider = module.RealtimeSectorBreadthProvider()
        provider._members = lambda *a: set(codes)
        checker, detail = provider.minute_checker(["board"], "20261022")
        assert checker("09:36:00") is True
        assert detail["confirmation_path"] == "open_rising_3m"
        assert detail["coverage_denominator"] == detail["trend_coverage_denominator"] == 2
        assert detail["current_coverage"] == detail["trend_coverage"] == 1
        assert checker("09:35:00") is None  # no carry-forward
        assert checker("09:37:00") is None  # no future minute


def test_full_universe_not_first_eighty_defines_width_coverage():
    from core.signals.sector_sync import make_constituent_sync_checker
    codes = [f"{i:06d}" for i in range(100)]
    checker, detail = make_constituent_sync_checker(code="999999", names=["board"],
        members={"board": codes}, frames={}, previous={c: {"close": 10} for c in codes},
        opening_prices={c: 10 for c in codes},
        minute_prices={c: {"09:33:00": 10, "09:36:00": 10.2} for c in codes[:60]})
    assert checker("09:36:00") is True
    assert detail["coverage"] == detail["trend_coverage"] == .6
    assert detail["coverage_denominator"] == 100


def test_bidirectional_reconciliation_reports_neighbors_without_claiming_exact_tick():
    rows = [{"code": "000001", "evidence_kind": "runtime_observation", "profile": "production",
             "observed_at": f"2026-10-22T{time}+08:00", "evidence": {
                 "status": "observe", "trigger_reason": "missing sector", "data_status": "missing_sector"}}
            for time in ["09:36:50", "09:37:10"]]
    value = attach_runtime_diagnostics({"market_date": "20261022", "until": "15:01:00",
        "stocks": [{"code": "000001", "status": "cancelled", "first_confirm_time": "09:36:00",
                    "first_evaluated_at": "09:37:00"}], "recorded_runtime": {"ledger": {"rows": rows}}})
    stock = value["stocks"][0]
    assert stock["ever_confirmed"] and stock["closing_status"] == "cancelled"
    diagnostic = stock["runtime_diagnostic"]
    assert diagnostic["replay_discrepancy"] == "replay_confirmed_without_runtime_confirmation"
    comparison = diagnostic["confirmation_comparison"]
    assert comparison["before"]["offset_seconds"] == -10
    assert comparison["after"]["offset_seconds"] == 10
    assert comparison["before"]["reason"] == "missing sector"


def payload():
    return {"market_date": "20261022", "candidate_date": "20261021", "profile": "production",
            "rows": [{"code": "000001", "signal_id": "signal", "entry_mode": "weak_only",
                      "confirm_time": "09:36:00",
                      "confirm_status": "confirmed", "entry_price": 0, "entry_time": "",
                      "received_at": NOW.isoformat(), "entry_parameters": {"deadline": "10:00:00"}}]}


def test_confirmed_signal_enqueues_before_simulated_fill_and_survives_worker_restart(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.sqlite3")
    calls = []
    class Notifier:
        def notify_realtime_payload(self, value):
            calls.append(value)
            value["rows"][0].update(notification_status="已推送", notification_result={"sent": 1, "all_delivered": True})
    clock = lambda: NOW.timestamp()
    queue = RealtimeNotificationOutbox(ledger, clock=clock, notifier_factory=Notifier)
    assert queue.enqueue(payload()) == 1
    assert not calls
    assert queue.enqueue(payload()) == 0
    restarted = RealtimeNotificationOutbox(ledger, clock=clock, notifier_factory=Notifier)
    assert restarted.process_once()
    assert len(calls) == 1
    assert not restarted.process_once()
    assert queue.enqueue(payload()) == 0
    assert calls[0]["rows"][0]["entry_price"] == 0
    from core.portfolio.paper_trading_service import PaperTradingService
    assert PaperTradingService._skip_reason(calls[0]["rows"][0], "20261022") == "下一分钟成交价缺失"
    assert ledger.timeline("signal")["events"]


@pytest.mark.parametrize("change", ["expired", "stale", "missing_receipt", "next_day", "deadline", "old_confirmation"])
def test_notification_worker_drops_invalid_signals_without_network(tmp_path, change):
    ledger = TradingLedger(tmp_path / "ledger.sqlite3")
    now = [NOW.timestamp()]
    value = payload()
    if change == "stale":
        value["rows"][0]["received_at"] = (NOW - timedelta(seconds=90)).isoformat()
    if change == "missing_receipt":
        value["rows"][0]["received_at"] = ""
    if change == "next_day":
        value["market_date"] = "20261021"
    if change == "deadline":
        value["rows"][0]["strategy_execution"] = {"mode_deadlines": {"weak_to_strong": "09:35:00"}}
    if change == "old_confirmation":
        value["rows"][0]["confirm_time"] = "09:31:00"
    def no_network():
        pytest.fail("Invalid notification must not reach a channel")
    queue = RealtimeNotificationOutbox(ledger, clock=lambda: now[0], notifier_factory=no_network)
    assert queue.enqueue(value) == 1
    if change == "expired":
        now[0] += 31
    assert queue.process_once()
    with ledger._connect() as con:
        assert con.execute("SELECT state FROM realtime_notification_jobs").fetchone()[0] == "expired"


def test_cycle_metrics_are_independent_of_minute_evidence_dedup(tmp_path):
    from core.operations.evidence import record_runtime_observations
    ledger = TradingLedger(tmp_path / "ledger.sqlite3")
    value = payload()
    value["rows"][0]["signal_evidence"] = {"code": "000001", "status": "observe", "market_date": "20261022"}
    value["runtime_cycle"] = {"id": "cycle1"}
    assert record_runtime_observations(ledger, value, now=NOW) == 1
    assert record_runtime_observations(ledger, value, now=NOW) == 0
    ledger.event("cycle1", "runtime_cycle", {"market_date": "20261022"})
    value["runtime_cycle"] = {"id": "cycle2"}
    assert record_runtime_observations(ledger, value, now=NOW) == 0
    ledger.event("cycle2", "runtime_cycle", {"market_date": "20261022"})
    with ledger._connect() as con:
        assert con.execute("SELECT count(*) FROM trading_events WHERE kind='runtime_cycle'").fetchone()[0] == 2


def test_streaming_confirmation_does_not_wait_for_next_stock(tmp_path):
    from core.realtime.overlay_service import RealtimeOverlayService
    order = []
    candidates = [{"code": "000001"}, {"code": "000002"}]
    class Signals:
        def evaluate(self, rows, quotes, *, on_evaluated, **kwargs):
            result = {}
            for row in rows:
                order.append("evaluate:" + row["code"])
                signal = {"signal_status": "confirmed" if row["code"] == "000001" else "observe"}
                result[row["code"]] = signal
                on_evaluated(row, {}, signal)
            return result
    overlay = RealtimeOverlayService(screening_dir=tmp_path, entry_signal_service=Signals())
    overlay._quote_map = lambda codes: {}
    overlay._strategy_metadata = lambda *args: {"id": "production", "name": "production", "version": "test",
                                               "execution": {}, "position_cap_pct": 8}
    overlay._strategy_label_map = lambda: {}
    overlay.build_overlay("20261022", market_date="20261022", candidates=candidates,
        on_confirmed=lambda value: order.append("enqueue:" + value["rows"][0]["code"]))
    assert order == ["evaluate:000001", "enqueue:000001", "evaluate:000002"]


@pytest.mark.parametrize("fields", [{"confirm_status": "observe"}, {"confirm_status": "unfilled"},
                                     {"health_gate_reason": "collector stale"}, {"entry_veto_reason": "blocked"}])
def test_outbox_never_overrides_status_or_health_gate(tmp_path, fields):
    queue = RealtimeNotificationOutbox(TradingLedger(tmp_path / "ledger.sqlite3"))
    value = payload()
    value["rows"][0].update(fields)
    assert queue.enqueue(value) == 0


def test_live_lane_does_not_preload_other_stocks_before_first_confirmation(monkeypatch):
    import pandas as pd
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
    from core.operations.preopen_evidence import PreopenEvidenceStore
    monkeypatch.setattr(PreopenEvidenceStore, "load", lambda *args: None)
    service = RealtimeEntrySignalService.__new__(RealtimeEntrySignalService)
    service.evaluator = SimpleNamespace(board_feedback_policy={})
    service._automatic_evaluator = None
    service.sector_breadth = RealtimeSectorBreadthProvider()
    service.calendar = SimpleNamespace(prev=lambda date: "20261021")
    service.quote_service = None
    service._previous_daily_map = lambda date: {}
    order = []
    service._minute_frame = lambda code, date: (order.append("minute:" + code) or pd.DataFrame())
    service._evaluate_one = lambda row, *args: (order.append("evaluate:" + row["code"]) or {})
    service._evaluate_batch([{"code": "000001"}, {"code": "000002"}], {}, market_date="20261022",
        on_evaluated=lambda row, *args: order.append("publish:" + row["code"]))
    assert order[:4] == ["minute:000001", "evaluate:000001", "publish:000001", "minute:000002"]
