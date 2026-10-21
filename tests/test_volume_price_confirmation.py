from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.confirmation_quality import ConfirmationPolicy, confirmation_index, policy_from_config
from backtest.minute_entry import ENTRY_WEAK, ENTRY_CONTINUATION, EntryDecision, MinuteEntryEvaluator
from backtest.backtest_engine import BacktestConfig, BacktestEngine
from core.signals.production_rules import minute_evaluator
from core.realtime.snapshot_signal_service import SnapshotSignalService
from risk.risk_config import RiskConfig


POLICY = ConfirmationPolicy(mode="volume_price", pullback_window=5)


def bars(tail=(10.03, 10.05, 10.06), volumes=(150, 120, 100)):
    closes = [9.95] * 5 + list(tail)
    return pd.DataFrame({
        "time": pd.date_range("2026-10-21 09:31", periods=len(closes), freq="min").strftime("%H:%M:%S"),
        "open": closes, "high": [10.0] * 5 + [price + 0.01 for price in tail],
        "low": [9.9] * 5 + [price - 0.01 for price in tail], "close": closes,
        "volume": [100] * 5 + list(volumes), "vwap": 9.95, "avg_price": 9.95,
    })


def confirm(frame, **kwargs):
    return confirmation_index(frame, 5, level=10.0, protection=9.89, deadline="10:00:00",
                              policy=POLICY, sector_sync=kwargs.pop("sector_sync", lambda t: True), **kwargs)


def evaluate(frame, *, live=False, policy=POLICY):
    return MinuteEntryEvaluator(confirmation_policy=policy).evaluate(
        mode=ENTRY_WEAK, bars=frame, open_gap=0, prev_close=10,
        plan_amount_ratio=1.5, sector_sync=lambda t: True, live=live)


def test_two_completed_closes_and_local_volume_not_one_tick_are_required():
    frame = bars()
    assert confirm(frame.iloc[:6])[1] == "quality_pending"
    assert confirm(frame.iloc[:7])[:2] == (6, "ready")
    assert "1.50" in confirm(frame)[2]
    frame.loc[5, "volume"] = 100
    assert confirm(frame.iloc[:7])[1] == "quality_pending"


def test_wick_breakout_cannot_confirm():
    frame = bars()
    frame.loc[5, ["high", "close"]] = [10.2, 9.99]
    assert confirm(frame)[1] == "quality_rejected"


def test_shrink_retest_and_volume_rebreak_are_distinct_completed_minutes():
    frame = bars((10.03, 10.02, 10.06, 10.07), (100, 70, 160, 100))
    frame.loc[6, "low"] = 10.001
    assert confirm(frame.iloc[:7])[1] == "quality_pending"
    assert confirm(frame)[:2] == (7, "ready")
    assert "缩量回踩" in confirm(frame)[2]
    frame.loc[6, "volume"] = 95
    assert confirm(frame)[1] != "ready"


def test_no_same_bar_retest_rebreak_order_is_invented():
    frame = bars((10.03, 10.08), (100, 70))
    frame.loc[6, "low"] = 10.001
    assert confirm(frame)[1] == "quality_pending"


@pytest.mark.parametrize("state,status", [(None, "data_insufficient"), (False, "quality_rejected")])
def test_sector_is_rechecked_at_final_confirmation(state, status):
    result = confirm(bars(), sector_sync=lambda time: True if time == "09:36:00" else state)
    assert result[1] == status


def test_future_prices_and_volumes_cannot_change_completed_confirmation():
    frame = bars()
    before = confirm(frame)
    frame.loc[7, ["close", "low", "high", "volume"]] = [1000, 1, 1000, 999999]
    assert confirm(frame) == before


def test_confirmation_gap_and_missing_volume_are_explicit():
    frame = bars()
    frame.loc[6, "time"] = "09:38:00"
    assert confirm(frame)[1] == "data_insufficient"
    frame = bars()
    frame.loc[3, "volume"] = 0
    assert confirm(frame)[1] == "data_insufficient"
    assert evaluate(frame).data_status == "missing_confirmation_evidence"


def test_protection_break_cancels_pending_setup():
    frame = bars()
    frame.loc[6, "low"] = 9.8
    assert confirm(frame)[1] == "cancelled"
    frame.loc[6, "low"] = 10.01
    frame.loc[5, "low"] = 9.8
    assert confirm(frame)[1] == "cancelled"


def test_overextension_is_not_bought_at_confirmation_or_next_fill():
    frame = bars((10.03, 10.3, 10.3))
    assert confirm(frame)[1] == "quality_rejected"
    frame = bars()
    frame.loc[7, "open"] = 10.4
    assert evaluate(frame).status == "signal_unfilled"
    frame.loc[7, "open"] = 10.06
    assert evaluate(frame).status == "filled"


def test_live_prefix_and_backtest_share_final_confirmation_and_fill_times():
    frame = bars()
    pending = evaluate(frame.iloc[:6], live=True)
    confirmed = evaluate(frame.iloc[:7], live=True)
    live_fill = evaluate(frame, live=True)
    historic = evaluate(frame)
    assert pending.status == "quality_pending"
    assert confirmed.status == "confirmed"
    assert confirmed.confirm_time == historic.confirm_time == "09:37:00"
    assert live_fill.entry_time == historic.entry_time == "09:38:00"
    assert live_fill.initial_confirm_time == "09:36:00"


def test_live_can_finish_three_bar_confirmation_started_before_last_two_rows():
    policy = replace(POLICY, sustain_minutes=3)
    frame = bars((10.03, 10.04, 10.05, 10.06), (150, 120, 100, 100))
    assert evaluate(frame.iloc[:8], live=True, policy=policy).confirm_time == "09:38:00"
    assert evaluate(frame, live=True, policy=policy).entry_time == "09:39:00"


def test_weak_reclaim_must_still_hold_previous_close_at_confirmation_and_execution():
    evaluator = MinuteEntryEvaluator(confirmation_policy=POLICY)
    args = dict(mode=ENTRY_WEAK, open_gap=-0.002, prev_close=10.02,
                plan_amount_ratio=1.5, sector_sync=lambda time: True)
    frame = bars((10.03, 10.01, 10.01))
    assert evaluator.evaluate(bars=frame, **args).status != "filled"
    frame = bars()
    frame.loc[7, "open"] = 10.01
    assert evaluator.evaluate(bars=frame, **args).status == "signal_unfilled"


def test_bare_research_legacy_and_old_frozen_config_remain_reproducible():
    assert MinuteEntryEvaluator().confirmation_policy.mode == "legacy"
    assert RiskConfig.from_dict({"entry_min_amount_pace": 0.8}).entry_confirmation_mode == "legacy"
    assert RiskConfig.from_dict({}).entry_confirmation_mode == "legacy"
    risk = RiskConfig()
    assert minute_evaluator(risk).confirmation_policy.mode == "volume_price"
    projected = BacktestConfig.from_risk_config(risk)
    assert projected.entry_confirmation_policy == asdict(policy_from_config(risk))
    assert BacktestEngine(None, projected).minute_evaluator.confirmation_policy == minute_evaluator(risk).confirmation_policy


@pytest.mark.parametrize("values", [
    {"mode": "made_up"}, {"sustain_minutes": 1}, {"sustain_minutes": True},
    {"volume_lookback": 0}, {"volume_lookback": 2.5}, {"min_volume_ratio": float("nan")},
    {"min_volume_ratio": True}, {"max_vwap_distance": 0}, {"max_breakout_extension": float("inf")},
    {"pullback_max_volume_ratio": 1.1}, {"pullback_window": 1},
])
def test_invalid_new_confirmation_parameters_are_rejected(values):
    with pytest.raises(ValueError):
        replace(POLICY, **values)


def ticks(count, *, price=10.05):
    start = datetime(2026, 10, 21, 9, 38, 0)
    return pd.DataFrame([{
        "date": "20261021", "time": (start + timedelta(seconds=3 * i)).strftime("%H:%M:%S"),
        "received_at": (start + timedelta(seconds=3 * i)).isoformat(),
        "last_price": price, "ask1": price + 0.01, "delta_volume": 10, "quality_ok": True,
    } for i in range(count)])


def test_snapshot_preserves_shared_confirmation_and_refuses_late_chase():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 10, 21, 9, 38, 6))
    decision = evaluate(bars().iloc[:7], live=True)
    args = dict(code="000001", trade_date="20261021", mode=ENTRY_WEAK, minute_bars=bars().iloc[:7],
                prev_close=10, open_gap=0, sector_confirmed=True, is_leader=False, limit_price=11,
                minute_decision=decision, confirmation_policy=POLICY)
    assert service.evaluate(snapshots=ticks(2), **args).data_status == "snapshot_triggered"
    result = service.evaluate(snapshots=ticks(3), **args)
    assert result.status == "filled" and result.confirmation_policy == "volume_price"
    assert result.initial_confirm_time == "09:36:00" and result.breakout_level == 10
    assert "局部放量" in result.confirmation_reason
    denied = service.evaluate(snapshots=ticks(3, price=10.3), **args)
    assert denied.data_status == "snapshot_price_quality" and denied.status == "observing"


def test_snapshot_can_execute_confirmed_vwap_reclaim_below_opening_high():
    frame = bars().iloc[:7].copy()
    frame.loc[:4, "high"] = 10.2
    decision = EntryDecision("confirmed", confirm_time="09:37:00", breakout_level=10,
                             initial_confirm_time="09:36:00", confirmation_policy="volume_price")
    service = SnapshotSignalService(clock=lambda: datetime(2026, 10, 21, 9, 38, 6))
    args = dict(code="000001", trade_date="20261021", mode=ENTRY_CONTINUATION,
                minute_bars=frame, prev_close=9.8, open_gap=0.02, sector_confirmed=True,
                is_leader=False, limit_price=10.8, minute_decision=decision, confirmation_policy=POLICY)
    assert service.evaluate(snapshots=ticks(2), **args).data_status == "snapshot_triggered"
    assert service.evaluate(snapshots=ticks(3), **args).status == "filled"


def test_volume_comparison_normalizes_lots_and_shares():
    frame = bars()
    frame["volume"] = frame["volume"].astype(float)
    frame["volume_unit"] = "shares"
    frame.loc[5, "volume_unit"] = "lots"
    frame.loc[5, "volume"] = 1.5
    assert evaluate(frame).status == "filled"


def test_final_confirmation_rechecks_amount_pace_and_records_final_metrics():
    frame = bars()
    evaluator = MinuteEntryEvaluator(confirmation_policy=POLICY)
    args = dict(breakout_level=10, sector_sync=lambda t: True)
    bad = evaluator._next_minute_fill(frame, 5, "weak", "test", 0, 1.5, True, 11,
                                     amount_pace_at=lambda row: 4.0, **args)
    assert bad.status == "quality_rejected" and "成交进度" in bad.reason
    ready = evaluator._next_minute_fill(frame.iloc[:7], 5, "weak", "test", 0, 1.5, True, 11,
                                       live=True, amount_pace_at=lambda row: 1.4, **args)
    assert ready.status == "confirmed" and ready.hold_minutes == 2 and ready.amount_pace == 1.4


def test_pending_snapshot_cannot_survive_a_minute_setup_failure():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 10, 21, 9, 38, 6))
    decision = evaluate(bars().iloc[:7], live=True)
    args = dict(code="000001", trade_date="20261021", mode=ENTRY_WEAK, minute_bars=bars().iloc[:7],
                prev_close=10, open_gap=0, sector_confirmed=True, is_leader=False, limit_price=11,
                minute_decision=decision, confirmation_policy=POLICY)
    assert service.evaluate(snapshots=ticks(2), **args).data_status == "snapshot_triggered"
    rejected = service.evaluate(snapshots=ticks(3), **{**args, "minute_decision": replace(decision, status="cancelled")})
    assert rejected.status == "cancelled"
    assert service.evaluate(snapshots=ticks(3), **args).data_status == "snapshot_triggered"


def test_replay_adapter_keeps_strategy_mode_restriction_and_new_confirmation_evidence():
    from core.realtime.day_replay import _ReplayEntryService
    profiles = SimpleNamespace(available=False, expected_fraction=lambda *a: (None, 0))
    service = _ReplayEntryService({}, {}, {}, {}, profiles, evaluator=minute_evaluator(RiskConfig()))
    service._sector_checker = lambda *a: (lambda time: True, {"data_completeness": 1.0})
    row = {"code": "000001", "amount_ratio": 1.5}
    quote = {"date": "20261021", "time": "09:38:00", "open_price": 10, "pre_close": 10,
             "last_price": 10.05, "is_stale": False}
    def signal(modes):
        return service._evaluate_one({**row, "allowed_entry_modes": modes}, quote, bars().iloc[:7],
                                     {}, {}, {"close": 10}, "20261021", [row], {})
    allowed = signal(["weak_to_strong"])
    assert allowed["signal_status"] == "confirmed"
    assert allowed["confirmation_policy"] == "volume_price"
    assert allowed["initial_confirm_time"] == "09:36:00"
    assert allowed["breakout_level"] == 10
    denied = signal(["continuation"])
    assert denied["strategy_mode_allowed"] is False
    assert denied["signal_status"] != "confirmed"


def test_editable_policy_rejects_invalid_inputs_and_allows_legacy_switch(monkeypatch):
    from config import config_registry as registry
    written = []
    monkeypatch.setattr(registry, "_index_fields", lambda: {
        ("risk", "entry_confirmation_mode"): {"type": "str", "default": "volume_price"},
        ("risk", "entry_sustain_minutes"): {"type": "int", "default": 2},
        ("risk", "entry_min_local_volume_ratio"): {"type": "float", "default": 1.2},
    })
    monkeypatch.setattr(registry, "_reapply_live", lambda: None)
    monkeypatch.setattr(registry.ov, "set_override", lambda *args: written.append(args))
    for path, value in [("entry_confirmation_mode", "bogus"), ("entry_sustain_minutes", 1.5),
                        ("entry_sustain_minutes", True), ("entry_min_local_volume_ratio", "nan")]:
        result = registry.apply_updates([{"scope": "risk", "path": path, "value": value}])
        assert result["errors"]
    assert not written
    result = registry.apply_updates([{"scope": "risk", "path": "entry_confirmation_mode", "value": "legacy"}])
    assert result["applied"] and not result["errors"]
    assert written == [("risk", "entry_confirmation_mode", "legacy")]


def test_configuration_selector_is_a_real_menu_and_current_yaml_enables_policy():
    from pathlib import Path
    from config.config_registry import build_simple_registry
    from jinja2 import Environment, nodes
    registry = build_simple_registry()
    field = next(f for section in registry["sections"] for group in section["groups"]
                 for f in group["fields"] if f["path"] == "entry_confirmation_mode")
    assert {item["value"] for item in field["choices"]} == {"legacy", "volume_price"}
    env = Environment(autoescape=True)
    ast = env.parse((Path(__file__).resolve().parents[1] / "web/templates/config.html").read_text(encoding="utf-8"))
    macro = next(node for node in ast.find_all(nodes.Macro) if node.name == "render_input")
    template = env.from_string(nodes.Template([macro]))
    html = template.module.render_input(field)
    assert "<select" in html and 'value="volume_price"' in html and 'data-type="str"' in html
    assert RiskConfig.load().entry_confirmation_mode == "volume_price"


def test_leader_and_strategy_rows_keep_shared_confirmation_evidence():
    from core.realtime.leader_pool_service import IntradayStrengthService
    from core.realtime.overlay_service import RealtimeOverlayService
    evidence = {"confirmation_policy": "volume_price", "confirmation_reason": "volume and hold",
                "initial_confirm_time": "09:36:00", "breakout_level": 10.0}
    signal = {"signal_status": "observe", **evidence}
    candidate = {"code": "000001"}
    quote = {"open_price": 10, "pre_close": 10, "last_price": 10.05}
    leader = IntradayStrengthService.__new__(IntradayStrengthService)._build_row(candidate, quote, signal)
    strategy = RealtimeOverlayService.__new__(RealtimeOverlayService)._build_row(
        "20261020", "20261021", candidate, quote, signal)
    for row in (leader, strategy):
        assert {key: row[key] for key in evidence} == evidence
