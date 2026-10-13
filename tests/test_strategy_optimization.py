from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.confirmation_quality import ConfirmationPolicy, confirmation_index
from backtest.evidence_policy import ReplayEvidenceBlocked
from backtest.minute_entry import MinuteEntryEvaluator
from core.operations.ledger import TradingLedger
from core.operations.replay_research import (
    FrozenCalendar,
    FrozenEvidence,
    ReplayResearch,
    ResearchEngine,
    ResearchSpec,
    atomic_json,
    research_engine,
)
from core.operations.strategy_optimization import (
    OPTIMIZATION_ARMS,
    REFINEMENT_ARMS,
    TUNING_ARMS,
    OptimizationEngine,
    OptimizationParameters,
    failure_exit,
    paired_comparisons,
    pullback_structure_check,
    quality_check,
    reversal_regime_check,
    weak_regime_check,
)


def bars(closes, *, start="09:36:00"):
    times = pd.date_range("2026-10-09 " + start, periods=len(closes), freq="min")
    return pd.DataFrame({"time": times.strftime("%H:%M:%S"), "open": closes,
                         "high": [c + .02 for c in closes], "low": [c - .01 for c in closes],
                         "close": closes, "vwap": 10., "volume": 10000.})


def confirm(frame, mode="sustain", **kwargs):
    return confirmation_index(frame, 0, level=10., protection=9.8, deadline="10:30:00",
                              policy=ConfirmationPolicy(mode), sector_sync=lambda time: True, **kwargs)


def test_wick_only_breakout_rejected_but_legacy_unchanged():
    frame = bars([9.99, 10.1])
    frame.loc[0, "high"] = 10.5
    assert confirm(frame, "legacy")[1] == "ready"
    assert confirm(frame, "close_breakout")[1] == "quality_rejected"
    frame.loc[0, "close"] = 10.01
    assert confirm(frame, "close_breakout")[0:2] == (0, "ready")


def test_sustain_requires_two_later_completed_bars_and_rechecks_sector():
    frame = bars([10.02, 10.03, 10.04, 10.05])
    assert confirm(frame.iloc[:2])[1] == "quality_pending"
    assert confirm(frame)[0:2] == (2, "ready")
    for state, status in [(None, "data_insufficient"), (False, "quality_rejected")]:
        result = confirmation_index(frame, 0, level=10, protection=9.8, deadline="10:30:00",
                                    policy=ConfirmationPolicy("sustain"), sector_sync=lambda t, state=state: state)
        assert result[1] == status
    frame.loc[3, ["close", "high", "low"]] = 1000
    assert confirm(frame)[0:2] == (2, "ready")  # Future candle cannot change a completed confirmation.


def test_pullback_touch_is_not_same_bar_as_rebreak():
    frame = bars([10.1, 10.02, 10.08, 10.09])
    frame.loc[1, "low"] = 10.001
    assert confirm(frame.iloc[:2], "pullback")[1] == "quality_pending"
    assert confirm(frame, "pullback")[0:2] == (2, "ready")
    frame.loc[1, "low"] = 9.97  # Outside the predeclared 0.2% retest band.
    assert confirm(frame.iloc[:3], "pullback")[1] == "quality_pending"


def test_broken_protection_and_missing_minute_are_not_buy_signals():
    frame = bars([10.1, 10.02, 10.08, 10.09])
    frame.loc[1, "low"] = 9.7
    assert confirm(frame)[1] == "cancelled"
    frame.loc[1, "low"] = 10.
    frame.loc[1, "time"] = "09:38:00"
    assert confirm(frame)[1] == "data_insufficient"


def test_empty_volume_does_not_count_as_sustained_confirmation():
    frame = bars([10.1, 10.2, 10.3, 10.4])
    frame.loc[1, "volume"] = 0
    assert confirm(frame)[1] == "quality_rejected"


def test_final_denial_preserves_key_and_specific_reason():
    evaluator = MinuteEntryEvaluator(confirmation_policy=ConfirmationPolicy("close_breakout"))
    frame = bars([10., 10., 10., 10., 10., 10.01], start="09:31:00")
    frame.loc[5, "high"] = 10.1
    result = evaluator._weak_to_strong(frame, frame.iloc[:5], frame.iloc[5:], 0, 10, 0, 1.5, 11,
                                      lambda t: True, None, 0, False)
    assert result.status == "quality_rejected"
    assert "分钟收盘" in result.reason and result.breakout_level == 10.02
    assert result.initial_confirm_time == "09:36:00"


def test_lunch_break_cancels_pending_confirmation_not_missing_data():
    frame = bars([10.1, 10.2], start="11:30:00")
    frame.loc[1, "time"] = "13:01:00"
    result = confirmation_index(frame, 0, level=10, protection=0, deadline="14:30:00",
                                policy=ConfirmationPolicy("sustain"), sector_sync=lambda t: True)
    assert result[1] == "quality_rejected"


def test_fill_follows_final_confirmation_and_preserves_first_trigger():
    evaluator = MinuteEntryEvaluator(confirmation_policy=ConfirmationPolicy("sustain"))
    frame = bars([10.1, 10.2, 10.3, 10.4])
    def evaluate():
        return evaluator._next_minute_fill(frame, 0, "weak_to_strong", "test", 0, 1, True, 11,
                                           breakout_level=10, sector_sync=lambda t: True)
    result = evaluate()
    assert result.status == "filled"
    assert (result.initial_confirm_time, result.confirm_time, result.entry_time) == ("09:36:00", "09:38:00", "09:39:00")
    assert result.entry_price == 10.4
    assert result.breakout_level == 10
    frame.loc[3, "volume"] = 0
    assert evaluate().status == "signal_unfilled"
    frame.loc[3, "volume"] = 10000
    frame.loc[3, "open"] = 11
    assert evaluate().status == "signal_unfilled"


@pytest.mark.parametrize("values", [{"sustain_minutes": 0}, {"gap_min_samples": 21},
                                    {"gap_floor": float("nan")}, {"relative_strength_min": 101},
                                    {"sector_risk_budget": .02}, {"failure_deadline": "14:00:00"}])
def test_invalid_optimization_parameters_refused(values):
    with pytest.raises(ValueError):
        OptimizationParameters(**values)


def test_strategy_quality_missing_is_distinct_from_rejected():
    params = OptimizationParameters()
    plan = {"策略ID": "limit_pullback", "因子指标": {"stk_pullback_contraction": 49,
                                                      "stk_relative_strength_sector": 60}}
    okay, missing, denied = quality_check(plan, params)
    assert not okay and not missing and len(denied) == 1
    plan["因子指标"].pop("stk_relative_strength_sector")
    assert quality_check(plan, params)[1] == ["stk_relative_strength_sector"]
    plan["因子指标"] = "null"
    assert quality_check(plan, params)[1] == ["factor_metrics_payload"]
    assert quality_check({"策略ID": "first_board_launch"}, params) == (True, [], [])
    assert quality_check({"策略ID": "new_unknown"}, params)[1] == ["unknown_strategy"]


def test_gap_stress_is_prior_only_and_uses_official_preclose():
    engine = object.__new__(OptimizationEngine)
    engine.parameters = OptimizationParameters(gap_lookback=2, gap_min_samples=2, gap_floor=.01)
    engine.calendar = FrozenCalendar(["20261008", "20261009", "20261012"])
    reads = []
    def daily(code, date):
        reads.append(date)
        return {"open": 9 if date == "20261009" else 10, "pre_close": 10}
    engine.dm = SimpleNamespace(get_stock_daily_data=daily)
    engine._gap_cache = {}
    assert engine._gap_risk("000001", "20261012") == pytest.approx((.1, 2))
    assert reads == ["20261008", "20261009"]
    engine._gap_cache = {}
    engine.dm = SimpleNamespace(get_stock_daily_data=lambda *args: {})
    with pytest.raises(ReplayEvidenceBlocked) as error:
        engine._gap_risk("000001", "20261012")
    assert error.value.details[0]["evidence"] == "prior_gap_history"


def test_total_and_same_sector_risk_budget_reduce_position(monkeypatch):
    engine = object.__new__(OptimizationEngine)
    engine.arm = "overnight_risk"
    engine.parameters = OptimizationParameters()
    engine.spec = ResearchSpec(suite="confirmation_quality")
    engine.config = BacktestConfig()
    engine.cash = 800000
    engine.total_capital = 1000000
    engine.current_positions = {"000002": {"market_value": 200000, "stress_risk_distance": .03,
                                            "resonance_sectors": "电池"}}
    engine._active_research_date = "20261012"
    monkeypatch.setattr(ResearchEngine, "_calculate_position_size", lambda *args: 340000)
    monkeypatch.setattr(engine, "_gap_risk", lambda *args: (.08, 20))
    monkeypatch.setattr(engine, "_plan_execution", lambda plan: {})
    monkeypatch.setattr(engine, "_execution_exit_config", lambda execution: {"hard_stop_loss": .05})
    distance = .08 + 2 * engine.config.commission_rate + engine.config.stamp_duty_rate + engine.config.slippage
    plan = {"代码": "000001", "共振板块": "电池"}
    same_sector = engine._calculate_position_size(plan)
    assert same_sector == pytest.approx(1500 / distance)
    plan["共振板块"] = "传媒"
    assert engine._calculate_position_size(plan) == pytest.approx(5000 / distance)
    engine.current_positions["000002"]["stress_risk_distance"] = .1
    assert engine._calculate_position_size(plan) == 0


def test_failure_exit_needs_two_closes_and_next_tradable_minute():
    frame = bars([9.9, 9.8, 9.7])
    frame.index = [10, 20, 30]
    evaluate = lambda **kw: failure_exit(frame, level=10, minutes=2, deadline="10:00:00", **kw)
    result = evaluate()
    assert result["time"] == "09:38:00" and result["confirm_time"] == "09:37:00"
    assert result["price"] == 9.7
    assert evaluate(until_time="09:38:00") is None
    assert evaluate(lower_limit=9.7) is None
    frame.loc[30, "volume"] = 0
    assert evaluate() is None


@pytest.mark.parametrize("arm,start,expected", [("exit_failure", "09:36:00", "09:38:00"),
                                               ("exit_patient", "09:40:00", "09:43:00"),
                                               ("combined_patient", "09:40:00", "09:43:00")])
def test_failure_exit_is_t_plus_one_only_and_hard_stop_keeps_priority(monkeypatch, arm, start, expected):
    engine = object.__new__(OptimizationEngine)
    engine.arm = arm
    engine.parameters = OptimizationParameters()
    engine.calendar = FrozenCalendar(["20261008", "20261009"])
    frame = bars([9.9, 9.8, 9.7, 9.6], start=start)
    monkeypatch.setattr(engine, "_load_exit_minute_bars", lambda *args: frame)
    # Input already has vwap; normalize preserves the causal below-VWAP sequence.
    monkeypatch.setattr("core.operations.strategy_optimization.normalize_minute_bars", lambda x: x)
    calls = []
    def parent(*args, **kwargs):
        calls.append(kwargs.get("until_time"))
        return {"sell": True, "time": "09:36:00", "reason": "hard_stop"}
    monkeypatch.setattr(ResearchEngine, "_minute_exit_decision", parent)
    position = {"entry_date": "20261008", "stock_name": "测试", "breakout_level": 10}
    result = engine._minute_exit_decision("000001", position, "20261009", {"pre_close": 10})
    assert result["reason"] == "hard_stop" and calls == [expected]
    calls.clear()
    engine._minute_exit_decision("000001", position, "20261008", {"pre_close": 10})
    assert calls == [None]


class Evidence:
    def __init__(self):
        self.minute_reads = []

    def get_stock_tick(self, code, date):
        self.minute_reads.append(date)
        times = pd.date_range(f"{date} 09:31", periods=120, freq="min").append(
            pd.date_range(f"{date} 13:01", periods=120, freq="min"))
        return pd.DataFrame({"time": times.strftime("%H:%M:%S"), "open": 10, "close": 10,
                             "high": 10.1, "low": 10, "volume": 10000, "amount": 100000,
                             "volume_unit": "shares", "trade_date": date})

    def get_stock_daily_data(self, code, date):
        return {"open": 10, "pre_close": 10, "close": 10}

    def get_auction_data(self, code, date):
        return {}

    def get_suspension_status(self, *args, **kwargs):
        return {"state": "trading"}


@pytest.mark.parametrize("suite,registry", [("confirmation_quality", OPTIMIZATION_ARMS),
                                          ("return_refinement", REFINEMENT_ARMS), ("in_sample_tuning", TUNING_ARMS)])
def test_eight_arm_freeze_history_no_minute_fetch_and_idempotent_resume(tmp_path, monkeypatch, suite, registry):
    service = ReplayResearch(root=tmp_path / "research", ledger=TradingLedger(tmp_path / "ledger.sqlite"))
    plans = tmp_path / "plans"
    plans.mkdir()
    frame = pd.DataFrame([{"代码": "000001", "名称": "测试", "策略ID": "weak_to_strong", "综合评分": 60,
                          "入场门控来源": "production_decision_v1", "生产决策允许入场": True,
                          "策略执行": json.dumps({"allowed_entry_modes": ["weak_to_strong"]})}])
    frame.to_csv(plans / "交易计划_20261008.csv", index=False)
    monkeypatch.setattr(BacktestEngine, "_load_sector_peer_codes", lambda *args, **kwargs: {"000001": []})
    data = Evidence()
    manifest = service.freeze(plans_dir=plans, start="20261009", end="20261009", data_manager=data,
        calendar=FrozenCalendar(["20260930", "20261008", "20261009"]),
        spec=ResearchSpec(suite=suite, optimization={"gap_lookback": 2, "gap_min_samples": 2}))
    assert set(manifest["arms"]) == set(registry)
    assert manifest["spec"]["optimization"]["failure_deadline"] == "10:00:00"
    assert "20260930" not in data.minute_reads
    directory = service._directory(manifest["experiment_id"])
    arm = {"confirmation_quality": "true_breakout", "return_refinement": "reversal_regime",
           "in_sample_tuning": "combined_sustain3"}[suite]
    engine = research_engine(FrozenEvidence(directory), BacktestConfig(**manifest["base_config"]), manifest, arm=arm)
    assert engine.minute_evaluator.confirmation_policy.mode == ("close_breakout" if suite == "confirmation_quality" else "sustain")
    assert engine.dm.get_stock_daily_data("000001", "20260930")["close"] == 10
    if suite in {"return_refinement", "in_sample_tuning"}:
        assert manifest["unseen_validation_required"]
        assert "history_already_examined" in manifest["hypothesis_origin"]
    if suite == "in_sample_tuning":
        assert engine.parameters.sustain_minutes == 3
        assert manifest["resolved_arm_parameters"][arm]["sustain_minutes"] == 3
        assert manifest["search_policy"]["registered_variants"] == 4
        assert manifest["classification"] == "in_sample_fitted_parameter_search"
    def day(engine, date, plan_dir):
        engine.daily_nav.append({"date": date, "cash": engine.cash, "position_value": 0,
                                 "total_value": engine.cash, "position_count": 0})
        return {}
    monkeypatch.setattr(ResearchEngine, "run_one_day", day)
    result = service.run(manifest["experiment_id"])
    assert result["status"] == "complete" and len(result["paired_comparisons"]) == len(registry) - 1
    checkpoints = {p: p.read_bytes() for p in directory.glob("states/*.json")}
    assert checkpoints
    service.run(manifest["experiment_id"])
    assert all(p.read_bytes() == content for p, content in checkpoints.items())
    manifest["spec"]["optimization"]["sustain_minutes"] = 4
    atomic_json(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="合同"):
        service.verify(manifest["experiment_id"])


def test_paired_results_do_not_compare_incomplete_accounts():
    report = {"results": {arm: {"complete": True, "summary": {"return_pct": -1, "max_drawdown_pct": 2,
                                                               "closed_trades": 3}} for arm in OPTIMIZATION_ARMS}}
    report["results"]["confirmed_pullback"]["complete"] = False
    pairs = paired_comparisons(report)
    assert len(pairs) == 7 and all(not p["release_allowed"] for p in pairs)
    assert next(p for p in pairs if p["arm"] == "confirmed_pullback")["return_delta_pp"] is None
    assert next(p for p in pairs if p["arm"] == "overnight_risk")["control"] == "confirmed_sustain"


@pytest.mark.parametrize("suite", ["confirmation_quality", "return_refinement", "in_sample_tuning"])
def test_negative_training_not_selected_even_when_validation_is_positive(tmp_path, suite):
    service = ReplayResearch(root=tmp_path / "research", ledger=TradingLedger(tmp_path / "ledger.sqlite"))
    dates = ["20260928", "20260929", "20260930", "20261008"]
    state = {"daily_nav": [{"date": date, "total_value": value} for date, value in zip(dates, [100, 99, 98, 150], strict=True)],
             "trade_history": [{"date": dates[1], "entry_date": dates[0], "action": "SELL", "pnl": -1,
                                "position_size": 10, "exit_price": 9, "shares": 1, "pnl_pct": -.1}]}
    atomic_json(tmp_path / "state.json", {"state": state})
    report = {"dates": dates, "spec": asdict(ResearchSpec(suite=suite, train_days=2,
               validation_days=1, embargo_days=1, min_train_closed=1)), "base_config": {"initial_capital": 100},
              "results": {"fixed_risk": {"state_path": "state.json", "data_quality": {}}}, "regimes": {}}
    fold = service._rolling(report, tmp_path)[0]
    assert fold["selected_arm"] is None
    assert fold["selection_status"] == "no_positive_training_edge_or_insufficient_samples"
    assert fold["validation"]["fixed_risk"]["account"]["return_pct"] > 0


@pytest.mark.parametrize("suite", ["confirmation_quality", "return_refinement", "in_sample_tuning"])
def test_api_preserves_explicit_research_suite_and_parameters(monkeypatch, suite):
    import web.app as module
    calls = []
    class Thread:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def start(self):
            pass
    monkeypatch.setattr(module, "Thread", Thread)
    monkeypatch.setattr(module, "_RESEARCH_REPLAY_JOB", {})
    response = module.api_replay_research_start({"start": "20260908", "end": "20260910",
        "suite": suite, "optimization": {"sustain_minutes": 3}})
    assert response.status_code == 200
    assert calls[0]["args"][3].suite == suite
    assert calls[0]["args"][3].optimization["sustain_minutes"] == 3


def test_api_rejects_invalid_policy_before_launch(monkeypatch):
    import web.app as module
    monkeypatch.setattr(module, "Thread", lambda **kwargs: pytest.fail("invalid policy must not launch"))
    response = module.api_replay_research_start({"start": "20260908", "end": "20260910",
        "suite": "confirmation_quality", "optimization": {"sustain_minutes": 0}})
    assert response.status_code == 422


@pytest.mark.parametrize("regime,okay,missing", [("强市 / 情绪活跃", True, False),
    ("震荡市 / 情绪活跃", False, False), ("strong / active", True, False),
    ("弱市 / 情绪退潮", False, False), ("强市 / 情绪高潮", False, False), (None, False, True)])
def test_reversal_regime_gate_is_strategy_specific(regime, okay, missing):
    result = reversal_regime_check({"策略ID": "limit_reversal"}, regime)
    assert result[0] == okay and bool(result[1]) == missing
    assert reversal_regime_check({"策略ID": "weak_to_strong"}, regime) == (True, [], [])


def test_pullback_structure_uses_prior_dates_and_actual_fill_space():
    execution = {"structures": {"limit_pullback": {"as_of_date": "20261008", "event_date": "20260930",
        "support": 10, "protection": 9, "resistance": 12, "atr": 1, "reference_close": 10.2}}}
    params = OptimizationParameters()
    check = lambda **kw: pullback_structure_check(execution, "20261008", params, **kw)
    assert check() == (True, [], [])
    assert check(entry_price=10.1)[0]
    assert not check(entry_price=10.5)[0]  # 1.5 / 1.5 is below the frozen 1.5 ratio.
    structure = execution["structures"]["limit_pullback"]
    structure["reference_close"] = 11.1
    assert check()[2]
    structure["as_of_date"] = "20261009"
    assert check()[1] == ["prior_pullback_structure"]
    structure["as_of_date"] = "20261008"
    structure["support"] = float("nan")
    assert check()[1]


def test_patient_exit_grace_recovery_volume_and_next_minute():
    frame = bars([9.8, 9.8, 9.8, 9.8, 9.8, 9.8], start="09:37:00")
    check = lambda: failure_exit(frame, level=10, minutes=3, deadline="10:00:00",
                                 start_time="09:40:00", buffer=.003, require_volume=True)
    assert check() is None  # Only three post-grace bars; still no next-minute fill.
    frame = bars([9.8, 9.8, 9.8, 9.8], start="09:40:00")
    assert check()["time"] == "09:43:00"
    frame.loc[1, "close"] = 9.98
    assert check() is None  # A recovery within the buffer cancels the streak.
    frame.loc[1, "close"] = 9.8
    frame.loc[1, "volume"] = 0
    assert check() is None
    frame.loc[1, "volume"] = 10000
    frame.loc[1, "time"] = "09:42:00"
    assert check() is None


@pytest.mark.parametrize("values", [{"patient_failure_start": "10:01:00"},
    {"patient_failure_minutes": 11}, {"patient_failure_buffer": .03},
    {"pullback_reward_risk_min": 0}, {"pullback_max_support_atr": float("nan")}])
def test_refinement_parameters_are_bounded(values):
    with pytest.raises(ValueError):
        OptimizationParameters(**values)


@pytest.mark.parametrize("arm", ["reversal_regime", "combined_quality", "combined_patient", "combined_sustain3", "combined_weak_regime"])
def test_reversal_gate_reads_frozen_prior_regime_before_minute_evaluation(monkeypatch, arm):
    engine = object.__new__(OptimizationEngine)
    engine.arm = arm
    engine.parameters = OptimizationParameters()
    engine.prior_regimes = {"20261009": "震荡市 / 情绪活跃"}
    recorded = []
    monkeypatch.setattr(engine, "_record_gate_attempt", lambda *args: recorded.append(args))
    monkeypatch.setattr(ResearchEngine, "_check_buy_conditions", lambda *args: pytest.fail("must deny before minutes"))
    plan = {"策略ID": "limit_reversal", "因子指标": {"stk_reversal_recovery": 70, "stk_relative_strength_sector": 70}}
    assert engine._check_buy_conditions(plan, "20261009", "000001", "测试") == (False, 0)
    assert recorded[0][3] == "refinement_rejected"


@pytest.mark.parametrize("arm", ["pullback_quality", "combined_quality", "combined_patient", "combined_sustain3", "combined_weak_regime"])
def test_pullback_rejects_actual_expensive_fill_and_clears_entry_state(monkeypatch, arm):
    engine = object.__new__(OptimizationEngine)
    engine.arm = arm
    engine.prior_regimes = {}
    engine.parameters = OptimizationParameters()
    engine.calendar = FrozenCalendar(["20261008", "20261009"])
    engine.entry_attempts = [{"status": "filled"}]
    engine._last_entry_gap = {"000001": 0}
    engine._last_entry_signal = {"000001": "limit_pullback"}
    engine._last_entry_meta = {"000001": {"entry_price": 10.5}}
    records = []
    monkeypatch.setattr(engine, "_record_gate_attempt", lambda *args: records.append(args))
    monkeypatch.setattr(ResearchEngine, "_check_buy_conditions", lambda *args: (True, 10.5))
    plan = {"策略ID": "limit_pullback", "因子指标": {"stk_pullback_contraction": 80,
        "stk_relative_strength_sector": 60}, "策略执行": {"structures": {"limit_pullback": {
        "as_of_date": "20261008", "event_date": "20260930", "reference_close": 10.2,
        "support": 10, "protection": 9, "resistance": 12, "atr": 1}}}}
    assert engine._check_buy_conditions(plan, "20261009", "000001", "测试") == (False, 0)
    assert records[-1][3] == "pullback_space_rejected"
    assert not engine._last_entry_meta and not engine._last_entry_gap and not engine._last_entry_signal
    assert engine.entry_attempts[0]["status"] == "research_filtered"


@pytest.mark.parametrize("regime,okay,missing", [("弱市 / 情绪退潮", True, False), ("weak / retreat", True, False),
    ("震荡市 / 活跃", False, False), ("强市 / 情绪活跃", False, False), (None, False, True)])
def test_weak_regime_is_explicit_historical_fit_and_strategy_specific(regime, okay, missing):
    result = weak_regime_check({"策略ID": "weak_to_strong"}, regime)
    assert result[0] == okay and bool(result[1]) == missing
    assert weak_regime_check({"策略ID": "mainline_leader"}, regime) == (True, [], [])
