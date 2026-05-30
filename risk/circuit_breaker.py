"""
账户级熔断 / kill switch（R-3）

在"信号层/组合层"之上再加一层"账户/时间"防线，三类触发：

1. 单日亏损熔断：当日已实现亏损 ≥ ``max_daily_loss`` → 当日停开新仓。
2. 回撤熔断：权益较峰值回撤 ≥ ``max_drawdown`` → 强制把总仓位降到
   ``drawdown_reduce_to``，并进入 ``cooldown_days`` 个交易日冷静期（冷静期内持续降仓）。
3. 情绪冰点熔断：情绪周期命中 ``freeze_cycles`` → 总仓位封顶 ``freeze_position_cap``。

输出 ``CircuitBreakerStatus``：``halt_new_buys``（是否禁止开新仓）+ ``position_cap``
（当日允许的总仓位上限，0~1）+ 触发明细。由 Layer4.5 风控闸门消费。

纯函数式：只读 ``PortfolioState`` 与 ``emotion_cycle``，不产生副作用（cooldown 推进在
闸门里显式写回，便于测试）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import loguru

from risk.risk_config import RiskConfig

logger = loguru.logger


@dataclass
class CircuitBreakerStatus:
    """熔断评估结果。"""
    level: str = "NORMAL"                 # NORMAL / CAUTION / HALT
    halt_new_buys: bool = False           # 是否禁止开新仓
    position_cap: float = 1.0             # 当日允许的总仓位上限（0~1）
    triggers: List[str] = field(default_factory=list)
    cooldown_until: str = ""              # 若本次触发冷静期，回填截止交易日

    @property
    def is_active(self) -> bool:
        return self.halt_new_buys or self.position_cap < 1.0


class CircuitBreaker:
    """账户级熔断器。"""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()

    def evaluate(self,
                 portfolio_state,
                 emotion_cycle: str = "",
                 price_map: Optional[Dict[str, float]] = None,
                 trade_date: str = "") -> CircuitBreakerStatus:
        """
        评估当前账户的熔断状态。

        Args:
            portfolio_state: ``PortfolioState`` 实例
            emotion_cycle:   当日情绪周期名（如 "冰点期"）
            price_map:       {code: 最新价}，用于盯市估值
            trade_date:      当前交易日 YYYYMMDD（判断是否仍在冷静期）

        Returns:
            CircuitBreakerStatus
        """
        status = CircuitBreakerStatus()
        ps = portfolio_state
        price_map = price_map or {}

        equity = ps.total_equity(price_map)
        if equity <= 0:
            return status

        # 1) 单日亏损熔断 ----------------------------------------------------
        daily_loss_ratio = ps.realized_pnl_today / equity if equity > 0 else 0.0
        if daily_loss_ratio <= -abs(self.cfg.max_daily_loss):
            status.halt_new_buys = True
            status.level = "HALT"
            status.triggers.append(
                f"单日亏损熔断: 当日已实现 {daily_loss_ratio:.2%} ≤ -{self.cfg.max_daily_loss:.0%}，今日停开新仓"
            )

        # 2) 回撤熔断（含冷静期）-------------------------------------------
        dd = ps.drawdown(price_map)
        in_cooldown = bool(
            trade_date and ps.cooldown_until and trade_date <= ps.cooldown_until
        )
        if dd <= -abs(self.cfg.max_drawdown):
            status.position_cap = min(status.position_cap, self.cfg.drawdown_reduce_to)
            status.level = "HALT" if status.halt_new_buys else "CAUTION"
            status.triggers.append(
                f"回撤熔断: 权益回撤 {dd:.2%} ≥ -{self.cfg.max_drawdown:.0%}，"
                f"总仓位降至 ≤{self.cfg.drawdown_reduce_to:.0%}，冷静 {self.cfg.cooldown_days} 个交易日"
            )
            status.cooldown_until = self._cooldown_until(trade_date)
        elif in_cooldown:
            status.position_cap = min(status.position_cap, self.cfg.drawdown_reduce_to)
            status.level = "HALT" if status.halt_new_buys else "CAUTION"
            status.triggers.append(
                f"回撤冷静期: 截至 {ps.cooldown_until}，总仓位仍封顶 ≤{self.cfg.drawdown_reduce_to:.0%}"
            )

        # 3) 情绪冰点熔断 ----------------------------------------------------
        if emotion_cycle and emotion_cycle in (self.cfg.freeze_cycles or []):
            status.position_cap = min(status.position_cap, self.cfg.freeze_position_cap)
            if status.level == "NORMAL":
                status.level = "CAUTION"
            status.triggers.append(
                f"情绪冰点熔断: 当前 {emotion_cycle}，总仓位封顶 ≤{self.cfg.freeze_position_cap:.0%}"
            )

        if status.triggers:
            logger.warning(
                f"[CircuitBreaker] {status.level} | cap={status.position_cap:.0%} | "
                + " / ".join(status.triggers)
            )
        return status

    def _cooldown_until(self, trade_date: str) -> str:
        """从 trade_date 起推 ``cooldown_days`` 个交易日作为冷静期截止。"""
        if not trade_date:
            return ""
        try:
            from core.utils.date_utils import DateUtils

            du = DateUtils()
            cur = trade_date
            for _ in range(max(1, self.cfg.cooldown_days)):
                cur = du.get_next_trade_date(cur)
            return cur
        except Exception:
            # 退化：按自然日近似（不精确，但不阻断）
            from datetime import datetime, timedelta

            try:
                dt = datetime.strptime(trade_date, "%Y%m%d") + timedelta(days=self.cfg.cooldown_days)
                return dt.strftime("%Y%m%d")
            except Exception:
                return trade_date


__all__ = ["CircuitBreaker", "CircuitBreakerStatus"]
