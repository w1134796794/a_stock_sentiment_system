"""Desired invariants currently violated; strict xfails track audit findings."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest.minute_entry import ENTRY_WEAK, MinuteEntryEvaluator, normalize_minute_bars


def _frame():
    return pd.DataFrame([
        {"time": f"09:{30+i:02d}:00", "open": price, "high": price,
         "low": price, "close": price, "volume": 1000, "amount": price * 1000}
        for i, price in enumerate([9.8, 9.85, 9.9, 9.95, 9.98, 10.03, 10.04])
    ])


@pytest.mark.xfail(strict=True, reason="AUD-01: legacy modes ignore per-strategy deadline")
def test_strategy_deadline_must_limit_legacy_confirmation():
    result = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=_frame(), open_gap=-0.02, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: True,
        confirmation_deadline="09:34:00",
    )
    assert not result.filled


@pytest.mark.xfail(strict=True, reason="AUD-03: weak mode assumes locked next-minute fill")
def test_weak_mode_must_not_assume_locked_limit_fill():
    frame = _frame()
    frame.loc[6, ["open", "high", "low", "close"]] = 11.0
    result = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=frame, open_gap=-0.02, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: True, limit_price=11,
    )
    assert not result.filled


@pytest.mark.xfail(strict=True, reason="AUD-04: monetary amount ignored by VWAP fallback")
def test_vwap_uses_actual_amount_when_volume_is_in_shares():
    frame = pd.DataFrame([{"time": "09:30:00", "open": 10, "high": 12,
                           "low": 10, "close": 12, "volume": 100,
                           "amount": 1050}])
    assert normalize_minute_bars(frame).iloc[0]["vwap"] == pytest.approx(10.5)


@pytest.mark.xfail(strict=True, reason="AUD-06: disabling catch-up also disables scheduled daily runs")
def test_daily_schedule_runs_with_catch_up_disabled(monkeypatch):
    from core.automation.internal_scheduler import InternalScheduler

    scheduler = InternalScheduler()
    scheduler.catch_up_enabled = False
    scheduler.daily_time = "20:00"
    scheduler.calendar = SimpleNamespace(is_trade_date=lambda _: True)
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(
        2026, 9, 11, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    calls = []
    monkeypatch.setattr(scheduler, "_dispatch_job", lambda *args: calls.append(args) or True)
    scheduler._recover_due_daily_job()
    assert calls


@pytest.mark.xfail(strict=True, reason="AUD-07: one successful recipient suppresses failed recipients")
def test_failed_notification_recipient_can_retry(monkeypatch):
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.notifications.notifier import NotificationService

    for key in ("WECOM_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "SERVERCHAN_SENDKEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SERVERCHAN_SENDKEYS", "test-a,test-b")
    service = NotificationService(backend=MemoryStateBackend("audit-recipients"))
    calls = []
    monkeypatch.setattr(service, "_post_form", lambda url, payload:
                        calls.append(url) or {"ok": "test-a" in url})
    service.send("test", "test", event_key="audit-event")
    service.send("test", "test", event_key="audit-event")
    assert sum("test-b" in url for url in calls) == 2


def _monitor(tmp_path, monkeypatch):
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.position_monitor import PositionMonitor

    repository = HoldingRepository(tmp_path / "audit.sqlite")
    position = repository.open_position({"code": "000001", "entry_date": "20260910",
                                         "entry_price": 10, "shares": 1000})
    decision = {"action": "reduce", "action_label": "reduce", "current_price": 10,
                "pnl_pct": 0, "can_sell": True, "reason": "same signal", "evidence": {}}
    monitor = PositionMonitor(repository, backend=MemoryStateBackend(str(tmp_path)))
    monitor.decisions = SimpleNamespace(evaluate=lambda *a, **k:
                                         SimpleNamespace(to_dict=lambda: dict(decision)))
    monkeypatch.setattr(monitor, "_quotes", lambda codes: {"quotes": []})
    monkeypatch.setattr(monitor, "_market_context", lambda: {})
    monkeypatch.setattr(monitor, "_sector_context", lambda *a: {})
    monkeypatch.setattr(monitor, "_notify", lambda *a, **k: 0)
    return monitor, repository, position


@pytest.mark.xfail(strict=True, reason="AUD-08: unchanged reduce signal executes repeatedly")
def test_same_reduction_signal_executes_once(tmp_path, monkeypatch):
    monitor, repository, position = _monitor(tmp_path, monkeypatch)
    monitor.run_once(signal_date="20260911", auto_execute=True)
    monitor.run_once(signal_date="20260911", auto_execute=True)
    assert repository.get_position(position["id"])["shares"] == 500


@pytest.mark.xfail(strict=True, reason="AUD-09: unchanged exit action never retries failed notification")
def test_failed_exit_notification_retries(tmp_path, monkeypatch):
    monitor, _, _ = _monitor(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(monitor, "_notify", lambda *a, **k: calls.append(1) or 0)
    assert monitor.run_once(signal_date="20260911")["ok"]
    assert monitor.run_once(signal_date="20260911")["ok"]
    assert len(calls) == 2
