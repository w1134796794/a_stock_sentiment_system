"""Regression invariants for the system audit findings."""
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


def test_strategy_deadline_must_limit_legacy_confirmation():
    result = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=_frame(), open_gap=-0.02, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: True,
        confirmation_deadline="09:34:00",
    )
    assert not result.filled


def test_weak_mode_must_not_assume_locked_limit_fill():
    frame = _frame()
    frame.loc[6, ["open", "high", "low", "close"]] = 11.0
    result = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=frame, open_gap=-0.02, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: True, limit_price=11,
    )
    assert not result.filled


def test_vwap_uses_actual_amount_when_volume_is_in_shares():
    frame = pd.DataFrame([{"time": "09:30:00", "open": 10, "high": 12,
                           "low": 10, "close": 12, "volume": 100,
                           "amount": 1050}])
    assert normalize_minute_bars(frame).iloc[0]["vwap"] == pytest.approx(10.5)


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


def test_failed_notification_recipient_can_retry(monkeypatch):
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.notifications.notifier import NotificationService

    for key in ("WECOM_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "SERVERCHAN_SENDKEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SERVERCHAN_SENDKEYS", "test-a,test-b")
    service = NotificationService(backend=MemoryStateBackend("audit-recipients"))
    service.retry_seconds = 0
    calls = []
    monkeypatch.setattr(service, "_post_form", lambda url, payload:
                        calls.append(url) or {"ok": "test-a" in url})
    service.send("test", "test", event_key="audit-event")
    service.send("test", "test", event_key="audit-event")
    assert sum("test-b" in url for url in calls) == 2
    assert sum("test-a" in url for url in calls) == 1


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
    monkeypatch.setattr(monitor, "_quotes", lambda codes: {"quotes": [{
        "code": "000001", "date": "20260911", "time": "09:40:00",
        "pre_close": 10, "last_price": 10, "is_stale": False,
    }]})
    monkeypatch.setattr(monitor, "_market_context", lambda: {})
    monkeypatch.setattr(monitor, "_sector_context", lambda *a: {})
    monkeypatch.setattr(monitor, "_notify", lambda *a, **k: 0)
    return monitor, repository, position


def test_same_reduction_signal_executes_once(tmp_path, monkeypatch):
    monitor, repository, position = _monitor(tmp_path, monkeypatch)
    monitor.run_once(signal_date="20260911", auto_execute=True)
    monitor.run_once(signal_date="20260911", auto_execute=True)
    assert repository.get_position(position["id"])["shares"] == 500


def test_failed_exit_notification_retries(tmp_path, monkeypatch):
    monitor, _, _ = _monitor(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(monitor, "_notify", lambda *a, **k: calls.append(1) or 0)
    assert monitor.run_once(signal_date="20260911")["ok"]
    assert monitor.run_once(signal_date="20260911")["ok"]
    assert len(calls) == 2


def test_added_today_shares_cannot_be_sold(tmp_path):
    from core.portfolio.holding_repository import HoldingRepository

    repository = HoldingRepository(tmp_path / "batches.sqlite")
    position = repository.open_position({"code": "000001", "entry_date": "20260910",
                                        "entry_price": 10, "shares": 1000})
    repository.open_position({"code": "000001", "entry_date": "20260911",
                              "entry_price": 10, "shares": 500})
    assert repository.sellable_shares(position["id"], "20260911") == 1000
    with pytest.raises(ValueError, match=r"T\+1"):
        repository.sell_position(position["id"], {"trade_date": "20260911", "price": 10, "shares": 1500})
    repository.sell_position(position["id"], {"trade_date": "20260911", "price": 10, "shares": 1000})
    assert repository.sellable_shares(position["id"], "20260911") == 0
    assert repository.sellable_shares(position["id"], "20260914") == 500


def test_rotation_rolls_back_both_legs_when_buy_fails(tmp_path):
    from core.portfolio.holding_repository import HoldingRepository

    repository = HoldingRepository(tmp_path / "atomic.sqlite")
    repository.ensure_account("default", name="audit", initial_capital=10000)
    position = repository.open_position({"code": "000001", "entry_date": "20260910",
                                        "entry_price": 10, "shares": 1000})
    before = repository.account("default")
    with pytest.raises(ValueError):
        repository.rotate_position(position["id"],
                                   {"trade_date": "20260911", "price": 10, "shares": 1000},
                                   {"code": "000002", "entry_date": "20260911", "entry_price": 20, "shares": 1000},
                                   "default")
    assert repository.get_position(position["id"])["shares"] == 1000
    assert repository.account("default")["cash"] == before["cash"]
    assert len(repository.list_trades()) == 1


def test_sector_snapshot_is_not_used_before_observation():
    from core.realtime.entry_signal_service import RealtimeEntrySignalService

    service = RealtimeEntrySignalService.__new__(RealtimeEntrySignalService)
    service.sector_breadth = SimpleNamespace(evaluate=lambda *args: (
        True, {"observed_at": "2026-09-11T09:55:00"}))
    checker, _ = service._sector_checker({}, "000001", [], {}, {}, "20260911")
    assert checker("09:35:00") is None
    assert checker("09:55:00") is True
    assert checker("09:58:00") is None


def test_missing_next_minute_is_not_a_fill():
    frame = _frame()
    frame.loc[6, "time"] = "09:39:00"
    result = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=frame, open_gap=-0.02, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: True,
    )
    assert not result.filled


def test_explicit_deadline_does_not_mutate_shared_evaluator():
    evaluator = MinuteEntryEvaluator()
    evaluator.evaluate(mode=ENTRY_WEAK, bars=_frame(), open_gap=-0.02, prev_close=10,
                       confirmation_deadline="09:34")
    assert evaluator.deadline == "10:00:00"


def test_pending_exit_retries_after_position_closes(tmp_path, monkeypatch):
    monitor, repository, position = _monitor(tmp_path, monkeypatch)
    repository.save_exit_signal(position["id"], {"action": "sell", "signal_date": "20260911"})
    repository.sell_position(position["id"], {"trade_date": "20260911", "price": 10, "shares": 1000})
    calls = []
    monkeypatch.setattr(monitor, "_notify", lambda *a, **k: calls.append((a, k)) or 1)
    result = monitor.run_once(signal_date="20260911")
    assert result["notified"] == 1
    assert len(calls) == 1
    assert "非新的卖出指令" in calls[0][0][1]["reason"]


def test_exit_signal_is_new_event_on_next_day(tmp_path, monkeypatch):
    _, repository, position = _monitor(tmp_path, monkeypatch)
    first = repository.save_exit_signal(position["id"], {"action": "sell", "signal_date": "20260911"})
    second = repository.save_exit_signal(position["id"], {"action": "sell", "signal_date": "20260914"})
    assert first["id"] != second["id"]
    assert second["changed"]


def test_invalid_previous_close_cannot_pass_quote_gate():
    from core.portfolio.execution_quotes import quote_error

    quote = {"date": "20260911", "time": "09:40:00", "last_price": 10, "is_stale": False}
    for value in (float("nan"), float("inf"), "invalid", 0):
        assert quote_error({**quote, "pre_close": value}, "20260911")


def test_profile_environment_gate_overrides_legacy_allowlist():
    from core.portfolio.decision_pool_service import DecisionPoolService

    profile = {"id": "mainline_leader", "enabled": True,
               "market_regimes": ["weak"], "emotion_phases": ["decline"]}
    result = DecisionPoolService().build(
        {"mainline_leader": {"final": []}}, {"mainline_leader": profile},
        market_score=30, market_state={"phase": "decline"},
    )
    assert result["active_strategy_ids"] == ["mainline_leader"]
    profile["emotion_phases"] = ["active"]
    result = DecisionPoolService().build(
        {"mainline_leader": {"final": []}}, {"mainline_leader": profile},
        market_score=30, market_state={"phase": "decline"},
    )
    assert not result["active_strategy_ids"]


def test_frozen_exit_config_is_used_for_live_position(tmp_path):
    from core.portfolio.exit_decision_service import ExitDecisionService
    from core.portfolio.holding_repository import HoldingRepository

    repository = HoldingRepository(tmp_path / "frozen.sqlite")
    position = repository.open_position({
        "code": "000001", "entry_date": "20260910", "entry_price": 10, "shares": 1000,
        "strategy_id": "limit_pullback", "metadata": {
            "strategy_version": "audit-frozen", "strategy_execution": {
                "exit": {"hard_stop_loss": 0.1, "trailing_stop": 0},
            },
        },
    })
    decision = ExitDecisionService().evaluate(position, {
        "last_price": 8.9, "pre_close": 10, "open_price": 9, "high_price": 10,
        "is_stale": False, "date": "20260911", "time": "09:40:00",
    }, signal_date="20260911")
    assert decision.action == "sell"
    assert decision.protect_price == pytest.approx(9)
    assert decision.evidence["strategy_version"] == "audit-frozen"


def test_estimated_amount_and_lot_unit_are_not_confused():
    frame = pd.DataFrame([{"time": "09:30:00", "close": 12, "volume": 1,
                           "amount": 1050, "volume_unit": "lots"}])
    assert normalize_minute_bars(frame).iloc[0].vwap == pytest.approx(10.5)
    frame["amount_is_estimated"] = True
    result = normalize_minute_bars(frame).iloc[0]
    assert result.vwap == pytest.approx(12)
    assert result.vwap_source == "estimated_close_weighted"


def test_direct_weak_screening_requires_prior_leader_pool(tmp_path, monkeypatch):
    from core.realtime.leader_pool_service import LeaderPoolService
    from core.screening.screening_engine import ScreeningEngine

    calls = []
    monkeypatch.setattr(LeaderPoolService, "historical_leader_codes",
                        lambda self, date, **kw: calls.append((date, kw)) or set())
    engine = ScreeningEngine(duckdb_path=tmp_path / "none.duckdb", output_dir=tmp_path)
    frame = pd.DataFrame([{"code": "000001", "tech_score": 100}])
    result = engine.run("20260911", profile="weak_to_strong",
                        profile_config={"strategy_id": "weak_to_strong"},
                        candidate_frame=frame, persist=False)
    assert result.ok and result.final == []
    assert calls[0][1]["include_trade_date"] is False
    assert "历史龙头前置池" in result.message
    assert len(frame) == 1


def test_zero_sell_quantity_does_not_liquidate_position(tmp_path, monkeypatch):
    _, repository, position = _monitor(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        repository.sell_position(position["id"], {"trade_date": "20260911", "price": 10, "shares": 0})
    assert repository.get_position(position["id"])["shares"] == 1000


@pytest.mark.parametrize("configured,internal", [
    ("weak_to_strong", "weak_only"), ("continuation", "continuation_only"),
    ("acceleration", "acceleration_only"), ("limit_pullback", "limit_pullback"),
])
def test_per_mode_deadline_accepts_strategy_and_runtime_names(configured, internal):
    from backtest.minute_entry import resolve_entry_deadline

    execution = {"confirmation_deadline": "10:00:00", "mode_deadlines": {configured: "09:40:00"}}
    assert resolve_entry_deadline(execution, internal) == "09:40:00"
