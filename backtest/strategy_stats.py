"""
策略统计归因（C-1）

把回测 / 历史重演的成交记录（``trade_history``）归因成"分模式 / 分共振 / 整体"的
胜率、盈亏比、期望，作为闭环参数标定与凯利仓位（C-2）的输入。

只统计**已实现**的卖出腿（``SELL`` / ``SELL_PARTIAL``）。每笔用 ``pnl_pct``
（相对持仓成本的收益率）作为"下注回报"——正是凯利公式需要的量纲。

纯函数，离线可测；兼容 ``TradeRecord`` 对象与 dict 两种成交记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

_SELL_ACTIONS = ("SELL", "SELL_PARTIAL")


def _attr(obj, name, default=0):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class Stat:
    """单个分组的统计量。"""
    key: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0       # 盈利单平均收益率（正）
    avg_loss_pct: float = 0.0      # 亏损单平均亏损率（取正值幅度）
    payoff_ratio: float = 0.0      # 盈亏比 = avg_win_pct / avg_loss_pct
    expectancy_pct: float = 0.0    # 单笔期望 = W*avg_win - (1-W)*avg_loss
    total_pnl: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "key": self.key, "n": self.n, "wins": self.wins, "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "payoff_ratio": round(self.payoff_ratio, 4),
            "expectancy_pct": round(self.expectancy_pct, 4),
            "total_pnl": round(self.total_pnl, 2),
        }


def _build_stat(key: str, rows: List[Dict]) -> Stat:
    n = len(rows)
    if n == 0:
        return Stat(key=key)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    nw, nl = len(wins), len(losses)
    win_rate = nw / n if n > 0 else 0.0
    avg_win = sum(r["pnl_pct"] for r in wins) / nw if nw else 0.0
    avg_loss = abs(sum(r["pnl_pct"] for r in losses) / nl) if nl else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    return Stat(
        key=key, n=n, wins=nw, losses=nl, win_rate=win_rate,
        avg_win_pct=avg_win, avg_loss_pct=avg_loss, payoff_ratio=payoff,
        expectancy_pct=expectancy, total_pnl=sum(r["pnl"] for r in rows),
    )


@dataclass
class StrategyStatsResult:
    overall: Stat
    by_pattern: Dict[str, Stat] = field(default_factory=dict)
    by_resonance: Dict[str, Stat] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "overall": self.overall.to_dict(),
            "by_pattern": {k: v.to_dict() for k, v in self.by_pattern.items()},
            "by_resonance": {k: v.to_dict() for k, v in self.by_resonance.items()},
        }


def compute_strategy_stats(trade_history: List) -> StrategyStatsResult:
    """从成交记录计算分模式 / 分共振 / 整体统计。"""
    rows: List[Dict] = []
    for t in trade_history or []:
        if _attr(t, "action", "") not in _SELL_ACTIONS:
            continue
        rows.append({
            "pattern": str(_attr(t, "pattern_type", "") or "未知"),
            "resonance": bool(_attr(t, "hot_resonance", False)),
            "pnl": float(_attr(t, "pnl", 0.0) or 0.0),
            "pnl_pct": float(_attr(t, "pnl_pct", 0.0) or 0.0),
        })

    overall = _build_stat("overall", rows)

    by_pattern: Dict[str, Stat] = {}
    patterns = sorted({r["pattern"] for r in rows})
    for p in patterns:
        by_pattern[p] = _build_stat(p, [r for r in rows if r["pattern"] == p])

    by_resonance: Dict[str, Stat] = {}
    for label, flag in (("共振", True), ("非共振", False)):
        sub = [r for r in rows if r["resonance"] == flag]
        if sub:
            by_resonance[label] = _build_stat(label, sub)

    return StrategyStatsResult(overall=overall, by_pattern=by_pattern, by_resonance=by_resonance)


__all__ = ["Stat", "StrategyStatsResult", "compute_strategy_stats"]
