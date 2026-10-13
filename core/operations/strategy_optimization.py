"""Registered research comparisons and explicit in-sample tuning, never production overrides."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from backtest.confirmation_quality import ConfirmationPolicy
from backtest.evidence_policy import ReplayEvidenceBlocked
from backtest.minute_entry import normalize_minute_bars
from core.operations.replay_research import ResearchEngine
from core.utils.sector_names import normalize_sector_names

OPTIMIZATION_ARMS = {
    "baseline": {"name": "原34%三仓", "change": "原信号基准", "parent": None},
    "fixed_risk": {"name": "固定风险", "change": "仅仓位算法", "parent": "baseline"},
    "true_breakout": {"name": "真突破", "change": "仅分钟收盘突破", "parent": "fixed_risk"},
    "confirmed_sustain": {"name": "真突破＋持续确认", "change": "仅连续站稳", "parent": "true_breakout"},
    "confirmed_pullback": {"name": "真突破＋回踩确认", "change": "仅回踩再突破", "parent": "true_breakout"},
    "overnight_risk": {"name": "持续确认＋隔夜风险", "change": "仅隔夜及组合风险仓位", "parent": "confirmed_sustain"},
    "strategy_quality": {"name": "隔夜风险＋策略质量", "change": "仅分策略质量条件", "parent": "overnight_risk"},
    "exit_failure": {"name": "策略质量＋失效退出", "change": "仅次日信号失效退出", "parent": "strategy_quality"},
}

REFINEMENT_ARMS = {
    "strategy_quality": {"name": "策略质量原对照", "change": "上一轮质量基准", "parent": None},
    "reversal_regime": {"name": "反包市场适配", "change": "仅反包市场阶段门禁", "parent": "strategy_quality"},
    "pullback_quality": {"name": "回踩空间及缩量", "change": "仅回踩结构与成交空间", "parent": "strategy_quality"},
    "exit_failure": OPTIMIZATION_ARMS["exit_failure"],
    "exit_patient": {"name": "失效退出容错", "change": "仅失效退出缓冲及持续跌破", "parent": "exit_failure"},
}
TUNING_ARMS = {
    "strategy_quality": REFINEMENT_ARMS["strategy_quality"],
    "combined_quality": {"name": "组合门禁", "change": "组合反包阶段与回踩质量门禁", "parent": "strategy_quality"},
    "combined_patient": {"name": "组合门禁＋退出容错", "change": "仅增加次日容错退出", "parent": "combined_quality"},
    "combined_sustain3": {"name": "组合门禁＋三分钟站稳", "change": "仅持续确认改为三分钟", "parent": "combined_quality",
                          "parameter_overrides": {"sustain_minutes": 3}},
    "combined_weak_regime": {"name": "组合门禁＋弱转强阶段", "change": "仅弱转强限定弱市退潮", "parent": "combined_quality"},
}
COMBINED_ARMS = set(TUNING_ARMS) - {"strategy_quality"}
REVERSAL_GATE_ARMS = {"reversal_regime"} | COMBINED_ARMS
PULLBACK_GATE_ARMS = {"pullback_quality"} | COMBINED_ARMS
PATIENT_EXIT_ARMS = {"exit_patient", "combined_patient"}
QUALITY_ARMS = {"strategy_quality", "exit_failure", "reversal_regime", "pullback_quality", "exit_patient"} | COMBINED_ARMS
RISK_ARMS = QUALITY_ARMS | {"overnight_risk"}


@dataclass(frozen=True)
class OptimizationParameters:
    sustain_minutes: int = 2
    pullback_window: int = 10
    pullback_tolerance: float = 0.002
    gap_lookback: int = 20
    gap_min_samples: int = 10
    gap_quantile: float = 0.9
    gap_floor: float = 0.08
    portfolio_risk_budget: float = 0.015
    sector_risk_budget: float = 0.0075
    relative_strength_min: float = 50.0
    contraction_min: float = 50.0
    recovery_min: float = 50.0
    repair_min: float = 60.0
    leader_identity_min: float = 70.0
    sector_persistence_min: float = 50.0
    failure_minutes: int = 2
    failure_deadline: str = "10:00:00"
    pullback_contraction_min: float = 70.0
    pullback_max_support_atr: float = 1.0
    pullback_reward_risk_min: float = 1.5
    patient_failure_minutes: int = 3
    patient_failure_start: str = "09:40:00"
    patient_failure_buffer: float = 0.003

    def __post_init__(self):
        ConfirmationPolicy("sustain", self.sustain_minutes, self.pullback_window, self.pullback_tolerance)
        if any(type(x) is not int or x < 1 for x in (self.gap_lookback, self.gap_min_samples, self.failure_minutes,
                                                  self.patient_failure_minutes)):
            raise ValueError("风险历史及失效窗口必须为正整数")
        if self.gap_min_samples > self.gap_lookback or self.gap_lookback > 120:
            raise ValueError("风险历史窗口或最少样本无效")
        for value in (self.gap_quantile, self.gap_floor, self.portfolio_risk_budget, self.sector_risk_budget):
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError("隔夜风险参数无效")
        if not 0.5 <= self.gap_quantile < 1 or self.sector_risk_budget > self.portfolio_risk_budget:
            raise ValueError("风险分位或板块预算无效")
        for key, value in asdict(self).items():
            if key.endswith("_min") and (not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError("策略质量阈值须在0至100之间")
        parsed = datetime.strptime(self.failure_deadline, "%H:%M:%S")
        if not "09:31:00" <= parsed.strftime("%H:%M:%S") <= "11:30:00":
            raise ValueError("失效退出截止时间须在上午连续竞价期间")
        start = datetime.strptime(self.patient_failure_start, "%H:%M:%S").strftime("%H:%M:%S")
        if not "09:31:00" <= start < self.failure_deadline or self.patient_failure_minutes > 10:
            raise ValueError("容错退出起始时间或持续窗口无效")
        if not math.isfinite(self.patient_failure_buffer) or not 0 <= self.patient_failure_buffer <= .02:
            raise ValueError("容错退出缓冲须在0至2%之间")
        for value in (self.pullback_max_support_atr, self.pullback_reward_risk_min):
            if not math.isfinite(value) or not 0 < value <= 5:
                raise ValueError("回踩距离及收益风险空间参数无效")


def quality_check(plan, params):
    metrics = plan.get("因子指标", "{}")
    try:
        metrics = json.loads(metrics) if isinstance(metrics, str) else dict(metrics)
        if not isinstance(metrics, dict):
            raise ValueError
    except (ValueError, TypeError):
        return False, ["factor_metrics_payload"], []
    rules = {
        "first_board_launch": {},  # Too few observations to add another selection threshold.
        "limit_pullback": {"stk_pullback_contraction": params.contraction_min,
                           "stk_relative_strength_sector": params.relative_strength_min},
        "limit_reversal": {"stk_reversal_recovery": params.recovery_min,
                           "stk_relative_strength_sector": params.relative_strength_min},
        "weak_to_strong": {"stk_behavior_repair": params.repair_min,
                           "stk_relative_strength_sector": params.relative_strength_min},
        "mainline_leader": {"stk_mainline_leader_identity": params.leader_identity_min,
                           "stk_sector_persistence_score": params.sector_persistence_min,
                           "stk_relative_strength_sector": params.relative_strength_min},
    }
    strategy = str(plan.get("策略ID") or "")
    if strategy not in rules:
        return False, ["unknown_strategy"], []
    missing, denied = [], []
    for key, threshold in rules[strategy].items():
        try:
            value = float(metrics[key])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            missing.append(key)
            continue
        if value < threshold:
            denied.append(f"{key}={value:.2f} < {threshold:.2f}")
    return not missing and not denied, missing, denied


def reversal_regime_check(plan, regime):
    if plan.get("策略ID") != "limit_reversal":
        return True, [], []
    labels = str(regime or "").replace(" ", "").split("/")
    market = {"强市": "strong", "震荡市": "sideways", "弱市": "weak",
              "strong": "strong", "sideways": "sideways", "weak": "weak"}
    emotion = {"情绪活跃": "active", "活跃": "active", "情绪退潮": "retreat", "退潮": "retreat",
               "情绪高潮": "climax", "高潮": "climax", "情绪回暖": "warming", "回暖": "warming",
               "情绪冰点": "ice", "冰点": "ice", "active": "active", "retreat": "retreat",
               "climax": "climax", "warming": "warming", "ice": "ice"}
    if len(labels) != 2 or labels[0] not in market or labels[1] not in emotion:
        return False, ["prior_candidate_regime"], []
    okay = market[labels[0]] == "strong" and emotion[labels[1]] == "active"
    return okay, [], [] if okay else [f"反包仅研究强市/活跃，候选日阶段为{regime}"]


def weak_regime_check(plan, regime):
    if plan.get("策略ID") != "weak_to_strong":
        return True, [], []
    # Reuse label validation, but the allowed pair belongs to a different strategy.
    _, missing, _ = reversal_regime_check({"策略ID": "limit_reversal"}, regime)
    if missing:
        return False, missing, []
    labels = str(regime).replace(" ", "").split("/")
    okay = labels[0] in {"weak", "弱市"} and labels[1] in {"retreat", "情绪退潮", "退潮"}
    return okay, [], [] if okay else [f"样本内弱转强仅研究弱市/退潮，候选日阶段为{regime}"]


def pullback_structure_check(execution, candidate_date, params, *, entry_price=None):
    try:
        structure = execution["structures"]["limit_pullback"]
        if structure["as_of_date"] != candidate_date or not structure["event_date"] < candidate_date:
            raise ValueError
        support, protection, resistance, atr, close = (float(structure[k]) for k in
            ("support", "protection", "resistance", "atr", "reference_close"))
        if not all(math.isfinite(v) and v > 0 for v in (support, protection, resistance, atr, close)):
            raise ValueError
        if not protection < support < resistance:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return False, ["prior_pullback_structure"], []
    denied = []
    if abs(close - support) / atr > params.pullback_max_support_atr:
        denied.append(f"候选收盘距支撑超过{params.pullback_max_support_atr:g}ATR")
    if entry_price is not None:
        price = float(entry_price)
        if not math.isfinite(price) or price <= protection:
            return False, ["pullback_execution_price"], []
        ratio = (resistance - price) / (price - protection)
        if ratio < params.pullback_reward_risk_min:
            denied.append(f"入场至已知前高空间/结构风险={ratio:.2f} < {params.pullback_reward_risk_min:g}")
    return not denied, [], denied


def failure_exit(bars, *, level, minutes, deadline, until_time=None, lower_limit=0,
                 start_time="09:31:00", buffer=0.0, require_volume=False):
    """Completed-bar thesis failure, followed by a tradable next-minute opening."""
    if not math.isfinite(level) or level <= 0:
        return None
    streak = 0
    previous = None
    for index, (_, row) in enumerate(bars.iterrows()):
        if str(row.time) > deadline:
            break
        if str(row.time) < start_time:
            streak, previous = 0, None
            continue
        contiguous = (previous is None or (datetime.strptime(str(row.time), "%H:%M:%S")
                      - datetime.strptime(previous, "%H:%M:%S")).total_seconds() == 60)
        previous = str(row.time)
        bad = float(row.close) < level * (1 - buffer) and float(row.close) < float(row.vwap) * (1 - buffer)
        if require_volume:
            bad = bad and math.isfinite(float(row.volume)) and float(row.volume) > 0
        streak = (streak + 1 if contiguous else 1) if bad else 0
        if streak < minutes or index + 1 >= len(bars):
            continue
        fill = bars.iloc[index + 1]
        gap = (datetime.strptime(str(fill.time), "%H:%M:%S")
               - datetime.strptime(str(row.time), "%H:%M:%S")).total_seconds()
        if gap != 60 or str(fill.time) > deadline or (until_time and str(fill.time) >= until_time):
            continue
        price, volume = float(fill.open), float(fill.volume)
        if not all(math.isfinite(x) and x > 0 for x in (price, volume)) or (lower_limit and price <= lower_limit * 1.002):
            continue
        return {"sell": True, "price": price, "time": str(fill.time),
                "confirm_time": str(row.time), "reason": "signal_failure_minute"}
    return None


class OptimizationEngine(ResearchEngine):
    def __init__(self, evidence, config, manifest, *, arm):
        super().__init__(evidence, config, manifest, arm=arm)
        if arm not in OPTIMIZATION_ARMS and arm not in REFINEMENT_ARMS and arm not in TUNING_ARMS:
            raise ValueError("未知确认质量实验组")
        overrides = TUNING_ARMS.get(arm, {}).get("parameter_overrides", {}) if self.spec.suite == "in_sample_tuning" else {}
        self.parameters = OptimizationParameters(**{**self.spec.optimization, **overrides})
        mode = "legacy" if arm in {"baseline", "fixed_risk"} else (
            "close_breakout" if arm == "true_breakout" else
            "pullback" if arm == "confirmed_pullback" else "sustain")
        self.minute_evaluator.confirmation_policy = ConfirmationPolicy(
            mode, self.parameters.sustain_minutes, self.parameters.pullback_window,
            self.parameters.pullback_tolerance)
        self._gap_cache = {}
        self.prior_regimes = manifest.get("regimes", {})

    def _check_buy_conditions(self, plan, date, stock_code, stock_name):
        if self.arm in QUALITY_ARMS:
            okay, missing, denied = quality_check(plan, self.parameters)
            if missing:
                raise ReplayEvidenceBlocked([{"trade_date": date, "code": stock_code,
                    "scope": "candidate", "evidence": "strategy_quality_factors",
                    "reason": "缺少策略质量证据：" + ",".join(missing)}], self.replay_data_quality())
            if not okay:
                self._record_gate_attempt(plan, date, "research_selection", "strategy_quality_rejected", "; ".join(denied))
                return False, 0
        checks = []
        if self.arm in REVERSAL_GATE_ARMS:
            checks.append(reversal_regime_check(plan, self.prior_regimes.get(date)))
        if self.arm == "combined_weak_regime":
            checks.append(weak_regime_check(plan, self.prior_regimes.get(date)))
        if self.arm in PULLBACK_GATE_ARMS and plan.get("策略ID") == "limit_pullback":
            contraction = quality_check(plan, OptimizationParameters(**{
                **asdict(self.parameters), "contraction_min": self.parameters.pullback_contraction_min}))
            checks.extend([contraction, pullback_structure_check(self._plan_execution(plan), self.calendar.prev(date), self.parameters)])
        for okay, missing, denied in checks:
            if missing:
                raise ReplayEvidenceBlocked([{"trade_date": date, "code": stock_code, "scope": "candidate",
                    "evidence": "refinement_selection", "reason": "缺少时点证据：" + ",".join(missing)}], self.replay_data_quality())
            if not okay:
                self._record_gate_attempt(plan, date, "research_selection", "refinement_rejected", "; ".join(denied))
                return False, 0
        allowed, price = super()._check_buy_conditions(plan, date, stock_code, stock_name)
        if allowed and self.arm in PULLBACK_GATE_ARMS and plan.get("策略ID") == "limit_pullback":
            okay, missing, denied = pullback_structure_check(self._plan_execution(plan), self.calendar.prev(date),
                                                            self.parameters, entry_price=price)
            if not okay:
                self.entry_attempts[-1].update(status="research_filtered", reason_code="pullback_space_rejected")
                self._record_gate_attempt(plan, date, "research_selection", "pullback_space_rejected", "; ".join(missing + denied))
                self._clear_entry_state(stock_code)
                return False, 0
        return allowed, price

    def _gap_risk(self, code, date):
        key = (code, date)
        if key not in self._gap_cache:
            dates = [d for d in self.calendar.dates if d < date][-self.parameters.gap_lookback:]
            losses = []
            for day in dates:
                bar = self.dm.get_stock_daily_data(code, day)
                opening, reference = float(bar.get("open") or 0), float(bar.get("pre_close") or 0)
                if all(math.isfinite(v) and v > 0 for v in (opening, reference)):
                    losses.append(max(0.0, 1 - opening / reference))
            if len(losses) < self.parameters.gap_min_samples:
                raise ReplayEvidenceBlocked([{"trade_date": date, "code": code, "scope": "candidate",
                    "evidence": "prior_gap_history", "reason": f"区间前隔夜风险日线不足：{len(losses)}/{self.parameters.gap_min_samples}"}], {})
            losses.sort()
            quantile = losses[max(0, math.ceil(self.parameters.gap_quantile * len(losses)) - 1)]
            self._gap_cache[key] = (max(self.parameters.gap_floor, quantile), len(losses))
        return self._gap_cache[key]

    def _calculate_position_size(self, plan):
        size = super()._calculate_position_size(plan)
        if self.arm not in RISK_ARMS:
            return size
        code = str(plan["代码"]).zfill(6)
        gap, samples = self._gap_risk(code, self._active_research_date)
        costs = 2 * self.config.commission_rate + self.config.stamp_duty_rate + self.config.slippage
        stop = float(self._execution_exit_config(self._plan_execution(plan))["hard_stop_loss"])
        distance = max(gap, stop) + costs
        equity = self.total_capital
        if not math.isfinite(equity) or equity <= 0:
            return 0.0
        used, sector_used = 0.0, 0.0
        sectors = set(normalize_sector_names(plan.get("共振板块") or plan.get("所属板块")))
        if not sectors:
            sectors = {"__unknown__"}
        for position in self.current_positions.values():
            exposure = float(position["market_value"]) * float(position.get("stress_risk_distance", distance))
            used += exposure
            groups = set(normalize_sector_names(position.get("resonance_sectors"))) or {"__unknown__"}
            if sectors & groups:
                sector_used += exposure
        remaining = min(equity * self.parameters.portfolio_risk_budget - used,
                        equity * self.parameters.sector_risk_budget - sector_used,
                        equity * self.spec.fixed_risk_per_trade)
        size = min(size, max(0, remaining) / distance)
        self._last_sizing_meta = {"method": "research_overnight_risk", "position_pct": size / equity,
            "sample_size": samples, "stress_distance": distance,
            "rationale": f"区间前跳空压力距离{distance:.2%}，组合剩余风险预算{max(0, remaining):.2f}；不是损失保证"}
        return size

    def _execute_buy(self, plan, date, *, entry_decision=None):
        self._active_research_date = date
        super()._execute_buy(plan, date, entry_decision=entry_decision)
        code = str(plan["代码"]).zfill(6)
        if code in self.current_positions and self.current_positions[code]["entry_date"] == date:
            position = self.current_positions[code]
            if self.arm in RISK_ARMS:
                gap, _ = self._gap_risk(code, date)
                costs = 2 * self.config.commission_rate + self.config.stamp_duty_rate + self.config.slippage
                position["stress_risk_distance"] = max(gap, position["exit_config"]["hard_stop_loss"]) + costs

    def _minute_exit_decision(self, stock_code, position, date, daily_bar, *, until_time=None):
        extra = None
        if self.arm in ({"exit_failure"} | PATIENT_EXIT_ARMS) and self.calendar.holding_days(position["entry_date"], date) == 1:
            from core.utils.price_limit import limit_down_price
            lower = limit_down_price(float(daily_bar.get("pre_close") or 0), stock_code, position["stock_name"])
            patient = self.arm in PATIENT_EXIT_ARMS
            extra = failure_exit(normalize_minute_bars(self._load_exit_minute_bars(stock_code, date)),
                level=float(position.get("breakout_level") or 0),
                minutes=self.parameters.patient_failure_minutes if patient else self.parameters.failure_minutes,
                deadline=self.parameters.failure_deadline, until_time=until_time, lower_limit=float(lower or 0),
                start_time=self.parameters.patient_failure_start if patient else "09:31:00",
                buffer=self.parameters.patient_failure_buffer if patient else 0.0, require_volume=patient)
            if extra and patient:
                extra["reason"] = "signal_failure_patient"
        # Preserve hard-stop priority and never let a later peak affect an earlier exit.
        result = super()._minute_exit_decision(stock_code, position, date, daily_bar,
                                              until_time=extra["time"] if extra else until_time)
        return result if result and result.get("sell") else (extra or result)


def paired_comparisons(report):
    rows = []
    suite = report.get("spec", {}).get("suite")
    registry = TUNING_ARMS if suite == "in_sample_tuning" else (REFINEMENT_ARMS if suite == "return_refinement" else OPTIMIZATION_ARMS)
    for arm, settings in registry.items():
        parent = settings["parent"]
        if not parent or arm not in report["results"] or parent not in report["results"]:
            continue
        child, control = report["results"][arm], report["results"][parent]
        complete = child["complete"] and control["complete"]
        rows.append({"arm": arm, "control": parent, "changed_variable": settings["change"],
            "complete": complete, "classification": "in_sample_fitted_comparison" if suite == "in_sample_tuning" else "exploratory_single_variable_account_comparison",
            "return_delta_pp": child["summary"]["return_pct"] - control["summary"]["return_pct"] if complete else None,
            "drawdown_delta_pp": child["summary"]["max_drawdown_pct"] - control["summary"]["max_drawdown_pct"] if complete else None,
            "closed_trades": child["summary"]["closed_trades"], "control_closed_trades": control["summary"]["closed_trades"],
            "release_allowed": False})
    return rows
