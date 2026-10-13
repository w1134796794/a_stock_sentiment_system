from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.notifications.notifier import NotificationService
from core.realtime.signal_event_repository import RealtimeSignalEventRepository


def payload():
    return {
        "candidate_date": "20261008", "market_date": "20261009", "profile": "leader_pool",
        "counts": {"confirmed": 1},
        "rows": [{
            "code": "002614", "name": "test", "confirm_status": "confirmed",
            "entry_mode": "weak_only", "confirm_time": "09:46:44",
            "received_at": "2026-10-09T09:46:44", "reason": "price trigger",
            "sector_detail": {"observed_at": "2026-10-09T09:46:44",
                              "data_completeness": 1.0, "member_count": 10, "observed_members": 10},
        }],
    }


@pytest.mark.parametrize("postclose_ok,coverage_ok", [(False, True), (True, False), (True, True)])
def test_confirmation_archive_is_after_both_health_gates(tmp_path, monkeypatch, postclose_ok, coverage_ok):
    import web.app as app
    import core.operations.health as health
    import core.operations.evidence as evidence
    import core.realtime.quote_cache as quotes
    import core.realtime.signal_event_repository as events

    repository = RealtimeSignalEventRepository(tmp_path / "events.sqlite")
    monkeypatch.setattr(events, "RealtimeSignalEventRepository", lambda: repository)
    monkeypatch.setattr(app, "_postclose_gate_for_date", lambda *_: {"ok": postclose_ok, "reasons": ["missing partition"]})
    collector = {"storage": "redis", "trade_date": "20261009",
                 "collector": {"ok": True, "lease_owned": True, "updated_at": "2026-10-09T09:46:44",
                               "session_started_at": "2026-10-09T09:15:00"}}
    monkeypatch.setattr(quotes, "RealtimeQuoteCache", lambda: SimpleNamespace(health=lambda: collector))
    gate = health.gate_realtime_payload
    monkeypatch.setattr(health, "gate_realtime_payload", lambda value, **kwargs: gate(
        value, **kwargs, now=datetime(2026, 10, 9, 9, 46, 45, tzinfo=ZoneInfo("Asia/Shanghai")), require_shared=True,
    ))
    monkeypatch.setattr(evidence, "attach_evidence", lambda value: value)
    value = payload()
    if not coverage_ok:
        value["rows"][0]["sector_detail"]["observed_members"] = 1
    result = app._prepare_signal_payload(value)
    eligible = postclose_ok and coverage_ok
    assert len(repository.list_events("20261009", "leader_pool")) == int(eligible)
    assert result["rows"][0]["confirm_status"] == ("confirmed" if eligible else "observe")
    assert result["rows"][0]["raw_trigger_reason"] == "price trigger"
    if eligible:
        later = payload()
        later["rows"][0]["confirm_status"] = "observe"
        later["rows"][0]["reason"] = "quote expired after close"
        later = app._prepare_signal_payload(later)
        assert later["rows"][0]["confirm_status"] == "confirmed_history"
        service = NotificationService(backend=MemoryStateBackend())
        monkeypatch.setattr(service, "send", lambda *_args, **_kwargs: pytest.fail("historical signal must not be sent"))
        assert service.notify_realtime_payload(later) == 0


@pytest.mark.parametrize("delivery,status", [
    ({"ok": True, "sent": 1}, "已推送"),
    ({"ok": False, "sent": 1}, "部分渠道已推送"),
    ({"ok": True, "sent": 0, "deduplicated": True}, "已去重"),
    ({"ok": False, "sent": 0, "inflight": True}, "推送处理中"),
    ({"ok": False, "sent": 0, "results": [{"retry_pending": True}]}, "等待渠道重试"),
    ({"ok": False, "sent": 0, "message": "channel not configured"}, "推送失败"),
])
def test_notification_outcomes_are_not_conflated(monkeypatch, delivery, status):
    service = NotificationService(backend=MemoryStateBackend())
    monkeypatch.setattr(service, "send", lambda *_args, **_kwargs: deepcopy(delivery))
    value = payload()
    assert service.notify_realtime_payload(value) == delivery["sent"]
    assert value["rows"][0]["notification_status"] == status
    if status == "推送失败":
        assert value["rows"][0]["notification_skip_reason"] == delivery["message"]


def test_price_diagnostic_is_not_an_executable_confirmation():
    import pandas as pd
    from scripts.diagnose_intraday_signal import price_diagnostic

    bars = pd.DataFrame([{"time": f"09:{31 + i:02d}:00", "price": price, "volume": 100}
                         for i, price in enumerate([8.24, 8.35, 8.25, 8.30, 8.21, 8.24, 8.37])])
    result = price_diagnostic(bars, {"open": 8.30, "pre_close": 8.34}, "10:00:00")
    assert result["status"] == "price_evidence_only"
    assert result["first_price_trigger"] == "09:37:00"
    assert result["weak_price_trigger_before_break"]
    assert result["amount_estimated"]
    assert price_diagnostic(bars, {"open": float("nan")}, "10:00:00")["status"] == "missing_opening_evidence"


def test_health_veto_preserves_original_trigger_in_evidence(monkeypatch):
    import core.operations.evidence as evidence

    monkeypatch.setattr(evidence, "_fetch_time", lambda _: "2026-10-08T20:00:00")
    value = payload()
    value["rows"][0].update({"confirm_status": "observe", "raw_trigger_reason": "price trigger",
                             "health_gate_reason": "sector coverage insufficient",
                             "reason": "sector coverage insufficient"})
    recorded = evidence.attach_evidence(value)["rows"][0]["signal_evidence"]
    assert recorded["trigger_reason"] == "price trigger"
    assert recorded["veto_reason"] == "sector coverage insufficient"
