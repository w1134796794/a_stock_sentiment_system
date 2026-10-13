"""Causal risk-sizing comparisons on identical sealed candidates and executions."""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass

from backtest.evidence_policy import ReplayEvidenceBlocked
from core.operations.replay_research import ResearchEngine, digest
from core.signals.trust_algorithms import beta_binomial_interval
from core.utils.sector_names import normalize_sector_names

SIZING_ARMS = {
    "baseline": {"name": "原34%三仓", "change": "因果修复后共同基准", "parent": None},
    "fixed_risk": {"name": "固定风险仓位", "change": "仅压力风险仓位及预算", "parent": "baseline"},
    "tiered_risk": {"name": "分档动态风险", "change": "仅按已成熟历史分档单笔预算", "parent": "fixed_risk"},
}


@dataclass(frozen=True)
class SizingParameters:
    gap_lookback: int = 20
    gap_min_samples: int = 10
    gap_quantile: float = .9
    gap_floor: float = .08
    portfolio_risk_budget: float = .015
    sector_risk_budget: float = .0075
    single_position_cap: float = .20
    low_risk: float = .0025
    standard_risk: float = .005
    high_risk: float = .0075
    min_samples: int = 50
    credibility: float = .8
    payoff_haircut: float = .8
    high_edge_min: float = .15
    high_win_lower: float = .5

    def __post_init__(self):
        for name in ("gap_lookback", "gap_min_samples", "min_samples"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Risk history and sample counts must be positive integers")
        if not self.gap_min_samples <= self.gap_lookback <= 120:
            raise ValueError("Invalid gap history window")
        for name in ("gap_quantile", "gap_floor", "portfolio_risk_budget", "sector_risk_budget",
                     "single_position_cap", "low_risk", "standard_risk", "high_risk", "credibility",
                     "payoff_haircut", "high_win_lower"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"Invalid sizing parameter: {name}")
        if (not .5 <= self.gap_quantile < 1 or not self.low_risk <= self.standard_risk <= self.high_risk
                or self.high_risk > .02 or self.sector_risk_budget > self.portfolio_risk_budget
                or not math.isfinite(self.high_edge_min) or self.high_edge_min < 0):
            raise ValueError("Invalid risk budget ordering or tier thresholds")


def matured_tier(trades, *, strategy, regime, regimes, train_start, cutoff, params):
    """Outcomes after the embargo cutoff, including today's exits, cannot enter."""
    rows = [r for r in trades if str(r.get("action", "")).startswith("SELL")
            and r.get("strategy_id") == strategy and regimes.get(r.get("entry_date")) == regime
            and train_start <= str(r.get("entry_date", "")) <= str(r.get("date", "")) <= cutoff]
    returns = [float(r["pnl_pct"]) for r in rows]
    if any(not math.isfinite(v) for v in returns):
        raise ValueError("Non-finite matured reference return")
    result = {"tier": "low", "risk_budget": params.low_risk, "sample_size": len(rows),
              "train_start": train_start, "outcome_cutoff": cutoff,
              "reason": "insufficient_matured_samples", "win_lower": None, "edge_lower": None}
    if len(rows) < params.min_samples or "unknown" in regime.lower():
        return result
    wins, losses = [v for v in returns if v > 0], [-v for v in returns if v < 0]
    if not wins or not losses:
        result["reason"] = "missing_two_sided_payoff_evidence"
        return result
    lower = beta_binomial_interval(len(wins), len(rows), credibility=params.credibility)["lower"]
    payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) * params.payoff_haircut
    edge = lower * payoff - (1 - lower)
    result.update(win_lower=lower, edge_lower=edge, reason="no_conservative_positive_edge")
    if edge > 0:
        high = edge >= params.high_edge_min and lower >= params.high_win_lower
        result.update(tier="high" if high else "standard",
                      risk_budget=params.high_risk if high else params.standard_risk,
                      reason="matured_conservative_positive_edge")
    return result


class SizingResearchEngine(ResearchEngine):
    def __init__(self, evidence, config, manifest, *, arm):
        super().__init__(evidence, config, manifest, arm=arm)
        if arm not in SIZING_ARMS:
            raise ValueError("Unknown sizing research arm")
        self.parameters = SizingParameters(**self.spec.optimization)
        self.regimes = manifest.get("regimes", {})
        self._gap_cache = {}
        self.sizing_decisions = []
        self.reference_trades = []
        if arm != "baseline":
            path = evidence.directory / "states/baseline.json"
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            if (checkpoint.get("contract_hash") != manifest["contract_hash"]
                    or checkpoint.get("state_hash") != digest(checkpoint["state"])):
                raise ValueError("Sizing reference account is not the sealed baseline")
            self.reference_trades = checkpoint["state"]["trade_history"]

    def export_state(self):
        return {**super().export_state(), "sizing_decisions": self.sizing_decisions}

    def import_state(self, state):
        super().import_state(state)
        self.sizing_decisions = list(state.get("sizing_decisions", []))

    def _gap_risk(self, code, date):
        key = (code, date)
        if key not in self._gap_cache:
            dates = [d for d in self.calendar.dates if d < date][-self.parameters.gap_lookback:]
            losses = []
            for day in dates:
                bar = self.dm.get_stock_daily_data(code, day)
                opening, reference = float(bar.get("open") or 0), float(bar.get("pre_close") or 0)
                if all(math.isfinite(v) and v > 0 for v in (opening, reference)):
                    losses.append(max(0., 1 - opening / reference))
            if len(losses) < self.parameters.gap_min_samples:
                raise ReplayEvidenceBlocked([{"trade_date": date, "code": code, "scope": "candidate",
                    "evidence": "prior_gap_history", "reason": f"Prior gap history {len(losses)}/{self.parameters.gap_min_samples}"}], {})
            losses.sort()
            quantile = losses[math.ceil(len(losses) * self.parameters.gap_quantile) - 1]
            self._gap_cache[key] = (max(self.parameters.gap_floor, quantile), len(losses))
        return self._gap_cache[key]

    def _tier(self, plan):
        prior = [d for d in self.calendar.dates if d < self._active_sizing_date]
        matured = prior[:-self.spec.embargo_days] if self.spec.embargo_days else prior
        train = matured[-self.spec.train_days:]
        return matured_tier(self.reference_trades, strategy=str(plan.get("策略ID") or ""),
                            regime=self.regimes.get(self._active_sizing_date, "unknown"), regimes=self.regimes,
                            train_start=train[0] if train else "99999999", cutoff=train[-1] if train else "",
                            params=self.parameters)

    def _calculate_position_size(self, plan):
        if self.arm == "baseline":
            return super()._calculate_position_size(plan)
        code = str(plan["代码"]).zfill(6)
        gap, samples = self._gap_risk(code, self._active_sizing_date)
        entry = self.research_entry_prices[code]
        stop = float(self._execution_exit_config(self._plan_execution(plan))["hard_stop_loss"])
        protection = float(self._last_entry_meta.get(code, {}).get("structural_stop") or 0)
        if 0 < protection < entry:
            stop = min(stop, 1 - protection / entry)
        costs = 2 * self.config.commission_rate + self.config.stamp_duty_rate + 2 * self.config.slippage
        distance = max(gap, stop) + costs
        tier = self._tier(plan) if self.arm == "tiered_risk" else {
            "tier": "fixed", "risk_budget": self.spec.fixed_risk_per_trade, "sample_size": 0,
            "reason": "preregistered_fixed_risk"}
        equity = self.total_capital
        used, sector_used = 0., 0.
        sectors = set(normalize_sector_names(plan.get("共振板块") or plan.get("所属板块"))) or {"__unknown__"}
        for position in self.current_positions.values():
            exposure = float(position["market_value"]) * float(position["stress_risk_distance"])
            used += exposure
            groups = set(normalize_sector_names(position.get("resonance_sectors"))) or {"__unknown__"}
            if sectors & groups:
                sector_used += exposure
        remaining = max(0., min(equity * self.parameters.portfolio_risk_budget - used,
                               equity * self.parameters.sector_risk_budget - sector_used,
                               equity * tier["risk_budget"]))
        size = max(0., min(remaining / distance, equity * self.parameters.single_position_cap,
                          equity * self.config.max_position_per_stock))
        self._last_sizing_meta = {**tier, "method": f"research_{self.arm}", "stress_distance": distance,
            "position_pct": size / equity if equity > 0 else 0, "gap_sample_size": samples,
            "remaining_risk_budget": remaining,
            "rationale": f"{tier['tier']}; risk={tier['risk_budget']:.2%}; stress={distance:.2%}; {tier['reason']}"}
        self.sizing_decisions.append({"date": self._active_sizing_date, "code": code,
                                      "strategy_id": str(plan.get("策略ID") or ""), **self._last_sizing_meta})
        return size

    def _execute_buy(self, plan, date, *, entry_decision=None):
        self._active_sizing_date = date
        code = str(plan["代码"]).zfill(6)
        already_held = code in self.current_positions
        super()._execute_buy(plan, date, entry_decision=entry_decision)
        position = self.current_positions.get(code)
        if self.arm != "baseline" and not already_held and position and position["entry_date"] == date:
            position["stress_risk_distance"] = self._last_sizing_meta["stress_distance"]
            position["sizing_tier"] = self._last_sizing_meta["tier"]


def tail_metrics(state, initial):
    previous, returns = initial, []
    for row in state.get("daily_nav", []):
        value = float(row["total_value"])
        returns.append(value / previous - 1)
        previous = value
    tail = sorted(returns)[:max(1, math.ceil(len(returns) * .05))]
    gaps = [r for r in state.get("trade_history", []) if r.get("exit_reason") == "stop_loss_gap_minute"]
    return {"daily_expected_shortfall_95_pct": sum(tail) / len(tail) * 100 if tail else None,
            "gap_stop_count": len(gaps), "gap_stop_net_pnl": sum(float(r["pnl"]) for r in gaps),
            "sizing_tier_counts": dict(Counter(r["tier"] for r in state.get("sizing_decisions", []))),
            "stress_budget_is_loss_guarantee": False}


def paired_comparisons(report):
    rows = []
    for arm, settings in SIZING_ARMS.items():
        parent = settings["parent"]
        if parent is None:
            continue
        child, control = report["results"][arm], report["results"][parent]
        complete = child["complete"] and control["complete"]
        rows.append({"arm": arm, "control": parent, "changed_variable": settings["change"],
            "complete": complete, "classification": "exploratory_reused_history_sizing_comparison",
            "return_delta_pp": child["summary"]["return_pct"] - control["summary"]["return_pct"] if complete else None,
            "drawdown_delta_pp": child["summary"]["max_drawdown_pct"] - control["summary"]["max_drawdown_pct"] if complete else None,
            "closed_trades": child["summary"]["closed_trades"],
            "control_closed_trades": control["summary"]["closed_trades"],
            "release_allowed": False})
    return rows
