"""
Layer 4.5: 风控闸门（R-2）

把 ``risk/`` 包真正接进实盘流水线：Layer4 产出的交易计划必须**逐条过闸**，否则
组合层硬约束（单票/总仓位/板块集中度/持仓数/现金底线）和账户级熔断（R-3）形同虚设。

闸门是"数据说话"的：它不偷偷改结果，而是对每条计划给出
``PASS / DOWNGRADE / REJECT`` 决策 + 可解释理由，并把调整后的仓位写回计划，
同时产出 ``RiskGateResult`` 供报告层展示。

执行顺序（重要）：
1. 先跑账户级熔断（CircuitBreaker）——触发 halt 则当日所有买入直接 REJECT。
2. 再按优先级累计校验组合层约束——前面接受的计划会占用预算，后面的据此收紧。

总仓位有效上限 = min(配置上限, 1-现金底线, 熔断上限, Layer4 大盘环境建议上限)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd
import loguru

from risk.risk_config import RiskConfig
from risk.circuit_breaker import CircuitBreaker, CircuitBreakerStatus

logger = loguru.logger

_EPS = 1e-6


@dataclass
class GateDecision:
    """单条计划的风控决策。"""
    stock_code: str
    stock_name: str
    pattern_type: str
    original_position_pct: float
    final_position_pct: float
    action: str                       # PASS / DOWNGRADE / REJECT
    reasons: List[str] = field(default_factory=list)

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "通过"


@dataclass
class RiskGateResult:
    """风控闸门整体结果。"""
    trade_date: str = ""
    decisions: List[GateDecision] = field(default_factory=list)
    cb_status: Optional[CircuitBreakerStatus] = None
    effective_total_cap: float = 1.0
    total_position_before: float = 0.0
    sector_exposure_before: Dict[str, float] = field(default_factory=dict)
    equity: float = 0.0
    summary: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for d in self.decisions if d.action == "PASS")

    @property
    def downgraded(self) -> int:
        return sum(1 for d in self.decisions if d.action == "DOWNGRADE")

    @property
    def rejected(self) -> int:
        return sum(1 for d in self.decisions if d.action == "REJECT")


class RiskGateLayer:
    """风控闸门层。"""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()
        self.breaker = CircuitBreaker(self.cfg)

    # ------------------------------------------------------------------
    def gate(self,
             trade_plan_result: Any,
             portfolio_state: Any,
             *,
             emotion_cycle: str = "",
             price_map: Optional[Dict[str, float]] = None,
             trade_date: str = "") -> RiskGateResult:
        """对一批交易计划执行风控闸门，原地调整 plan 的 position_pct / position_level。"""
        price_map = price_map or {}
        plans = list(getattr(trade_plan_result, "plans", []) or [])

        equity = portfolio_state.total_equity(price_map)
        if equity <= 0:
            equity = self.cfg.initial_capital

        result = RiskGateResult(
            trade_date=trade_date or getattr(trade_plan_result, "trade_date", ""),
            equity=equity,
        )

        # 1) 账户级熔断 -----------------------------------------------------
        cb = self.breaker.evaluate(portfolio_state, emotion_cycle, price_map, trade_date)
        result.cb_status = cb

        # 2) 有效总仓位上限 -------------------------------------------------
        market_cap = float(getattr(trade_plan_result, "max_position_pct", 1.0) or 1.0)
        effective_cap = min(
            self.cfg.max_total_position,
            1.0 - self.cfg.min_cash_ratio,
            cb.position_cap,
            market_cap if market_cap > 0 else 1.0,
        )
        result.effective_total_cap = effective_cap

        # 3) 起始组合快照（从持久化账户出发，逐条累计）---------------------
        sim_positions = portfolio_state.to_risk_positions(price_map)
        current_total_value = sum(p.get("market_value", 0.0) for p in sim_positions.values())
        sector_value: Dict[str, float] = {}
        for p in sim_positions.values():
            sec = p.get("sector") or "未知"
            sector_value[sec] = sector_value.get(sec, 0.0) + p.get("market_value", 0.0)
        held_codes = {c for c, p in sim_positions.items() if p.get("market_value", 0.0) > 0}

        result.total_position_before = current_total_value / equity if equity > 0 else 0.0
        result.sector_exposure_before = portfolio_state.sector_exposure(price_map)

        total_budget = equity * effective_cap
        per_stock_cap_value = equity * self.cfg.max_position_per_stock
        sector_cap_value = equity * self.cfg.max_sector_concentration

        # 按优先级从优到劣处理（priority 越小越优先）
        ordered = sorted(plans, key=lambda p: getattr(p, "priority", 99))

        for plan in ordered:
            decision = self._gate_single(
                plan, equity, cb, total_budget, current_total_value,
                per_stock_cap_value, sector_cap_value, sector_value, held_codes,
            )
            result.decisions.append(decision)

            # 接受的计划占用预算，影响后续计划
            if decision.action in ("PASS", "DOWNGRADE") and decision.final_position_pct > 0:
                accepted_value = equity * decision.final_position_pct
                current_total_value += accepted_value
                sec = self._plan_sector(plan)
                sector_value[sec] = sector_value.get(sec, 0.0) + accepted_value
                held_codes.add(plan.stock_code)

        result.summary = self._build_summary(result)
        logger.info(
            f"[L4.5] 风控闸门: 通过{result.passed} 降级{result.downgraded} 拒绝{result.rejected}, "
            f"有效总仓位上限={effective_cap:.0%}, 熔断={cb.level}"
        )
        return result

    # ------------------------------------------------------------------
    def _gate_single(self, plan, equity, cb, total_budget, current_total_value,
                     per_stock_cap_value, sector_cap_value, sector_value,
                     held_codes) -> GateDecision:
        code = getattr(plan, "stock_code", "")
        name = getattr(plan, "stock_name", "")
        ptype = getattr(plan, "pattern_type", "")
        orig_pct = float(getattr(plan, "position_pct", 0.0) or 0.0)

        decision = GateDecision(
            stock_code=code, stock_name=name, pattern_type=ptype,
            original_position_pct=orig_pct, final_position_pct=orig_pct, action="PASS",
        )

        # 本就是观察/回避（仓位 0），不参与买入，直接放行不计入风控调整
        if orig_pct <= _EPS:
            decision.action = "PASS"
            return decision

        # 账户级熔断：禁止开新仓
        if cb.halt_new_buys:
            decision.action = "REJECT"
            decision.final_position_pct = 0.0
            decision.reasons.extend(cb.triggers or ["账户级熔断：当日停开新仓"])
            self._apply_to_plan(plan, 0.0)
            return decision

        desired_value = equity * orig_pct
        allowed = desired_value
        reasons: List[str] = []

        # a) 持仓数上限（新标的才计数）
        if code not in held_codes and len(held_codes) >= self.cfg.max_positions:
            decision.action = "REJECT"
            decision.final_position_pct = 0.0
            decision.reasons.append(
                f"持仓数已达上限{self.cfg.max_positions}只，不再开新仓"
            )
            self._apply_to_plan(plan, 0.0)
            return decision

        # b) 单票上限
        if allowed > per_stock_cap_value + _EPS:
            allowed = per_stock_cap_value
            reasons.append(f"单票超限→封顶{self.cfg.max_position_per_stock:.0%}")

        # c) 总仓位上限（含现金底线 + 熔断 + 大盘建议）
        remaining_total = total_budget - current_total_value
        if allowed > remaining_total + _EPS:
            allowed = max(remaining_total, 0.0)
            reasons.append(
                f"总仓位超限→剩余额度{max(remaining_total, 0.0) / equity:.0%}"
            )

        # d) 板块集中度
        sec = self._plan_sector(plan)
        if sec and sec != "未知":
            remaining_sector = sector_cap_value - sector_value.get(sec, 0.0)
            if allowed > remaining_sector + _EPS:
                allowed = max(remaining_sector, 0.0)
                reasons.append(
                    f"板块[{sec}]集中度超限→封顶{self.cfg.max_sector_concentration:.0%}"
                )

        # 决策判定
        if allowed <= equity * 0.005:  # 小于 0.5% 视为无意义额度
            decision.action = "REJECT"
            decision.final_position_pct = 0.0
            decision.reasons = reasons or ["可用额度不足"]
            self._apply_to_plan(plan, 0.0)
            return decision

        final_pct = allowed / equity
        if final_pct < orig_pct - _EPS:
            decision.action = "DOWNGRADE"
            decision.final_position_pct = round(final_pct, 4)
            decision.reasons = reasons
            self._apply_to_plan(plan, decision.final_position_pct)
        else:
            decision.action = "PASS"
            decision.final_position_pct = orig_pct
        return decision

    @staticmethod
    def _plan_sector(plan) -> str:
        sectors = getattr(plan, "resonance_sectors", None) or []
        if isinstance(sectors, str):
            sectors = [s.strip() for s in sectors.split(",") if s.strip()]
        return sectors[0] if sectors else "未知"

    @staticmethod
    def _apply_to_plan(plan, final_pct: float) -> None:
        """把风控后仓位写回 plan；归零时降级为'回避'。"""
        plan.position_pct = final_pct
        if final_pct <= _EPS:
            try:
                from core.pipeline.layer4_trade_plan import PositionLevel

                plan.position_level = PositionLevel.AVOID
            except Exception:
                pass

    # ------------------------------------------------------------------
    def apply_to_dataframe(self, df: pd.DataFrame, result: RiskGateResult) -> pd.DataFrame:
        """给交易计划 DataFrame 增列：风控动作 / 风控后仓位 / 风控提示。"""
        if df is None or df.empty or not result.decisions:
            return df
        by_key: Dict[tuple, GateDecision] = {
            (d.stock_code, d.pattern_type): d for d in result.decisions
        }
        action_label = {"PASS": "通过", "DOWNGRADE": "降级", "REJECT": "拒绝"}

        actions, final_pcts, notes = [], [], []
        code_col = "股票代码" if "股票代码" in df.columns else None
        ptype_col = "模式类型" if "模式类型" in df.columns else None
        for _, row in df.iterrows():
            code = str(row.get(code_col, "")) if code_col else ""
            ptype = str(row.get(ptype_col, "")) if ptype_col else ""
            d = by_key.get((code, ptype))
            if d is None:
                actions.append("通过")
                final_pcts.append(row.get("建议仓位", ""))
                notes.append("")
            else:
                actions.append(action_label.get(d.action, d.action))
                final_pcts.append(f"{d.final_position_pct:.0%}")
                notes.append(d.reason_text)
        out = df.copy()
        out["风控动作"] = actions
        out["风控后仓位"] = final_pcts
        out["风控提示"] = notes
        return out

    @staticmethod
    def _build_summary(result: RiskGateResult) -> str:
        lines = [f"=== 风控闸门 ({result.trade_date}) ==="]
        cb = result.cb_status
        if cb and cb.is_active:
            lines.append(f"熔断状态: {cb.level}")
            for t in cb.triggers:
                lines.append(f"  - {t}")
        else:
            lines.append("熔断状态: NORMAL（无触发）")
        lines.append(
            f"有效总仓位上限: {result.effective_total_cap:.0%} | "
            f"开闸前持仓: {result.total_position_before:.0%}"
        )
        lines.append(
            f"决策: 通过 {result.passed} / 降级 {result.downgraded} / 拒绝 {result.rejected}"
        )
        for d in result.decisions:
            if d.action != "PASS":
                lines.append(
                    f"  [{d.action}] {d.stock_name}({d.stock_code}) "
                    f"{d.original_position_pct:.0%}→{d.final_position_pct:.0%} | {d.reason_text}"
                )
        return "\n".join(lines)


__all__ = ["RiskGateLayer", "RiskGateResult", "GateDecision"]