"""
凯利仓位（R-4 / C-2）

用回测统计出的胜率 W 与盈亏比 R 计算凯利最优下注比例：

    f* = W - (1 - W) / R

工程化处理（避免"赌徒破产"与过拟合）：
- **半凯利**：实际仓位 = ``kelly_fraction`` × f*（默认 0.5），凯利本身波动极大。
- **样本不足回退**：单模式样本 < ``kelly_min_samples`` 时回退到固定基础仓位。
- **负边过滤**：f* ≤ 0（无正期望）→ 建议仓位 0（该模式不交易）。
- **双重封顶**：不超过 ``kelly_max_position``，也不超过 ``max_position_per_stock``。

本模块只依赖数字 / 鸭子类型的 stat 对象（含 ``win_rate / payoff_ratio / n``），
**不 import backtest**，以免与回测模块形成循环依赖。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import loguru

from risk.risk_config import RiskConfig

logger = loguru.logger


class KellySizer:
    """基于胜率/盈亏比的凯利仓位计算器。"""

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()

    # ------------------------------------------------------------------
    def full_kelly(self, win_rate: float, payoff_ratio: float) -> float:
        """完整凯利比例 f* = W - (1-W)/R；R<=0 时返回 0。"""
        if payoff_ratio <= 0:
            return 0.0
        w = min(max(win_rate, 0.0), 1.0)
        return w - (1 - w) / payoff_ratio

    def size(self, win_rate: float, payoff_ratio: float, n: int,
             base_position_pct: float) -> Dict:
        """
        返回 ``{position_pct, method, full_kelly, rationale}``。

        Args:
            base_position_pct: 样本不足时回退使用的固定基础仓位。
        """
        cap = min(self.cfg.kelly_max_position, self.cfg.max_position_per_stock)

        if n < self.cfg.kelly_min_samples:
            pct = min(base_position_pct, cap)
            return {
                "position_pct": round(pct, 4),
                "method": "fallback_insufficient_samples",
                "full_kelly": 0.0,
                "rationale": (f"样本{n} < 阈值{self.cfg.kelly_min_samples}，"
                              f"回退固定仓位 {pct:.1%}"),
            }

        f_full = self.full_kelly(win_rate, payoff_ratio)
        if f_full <= 0:
            return {
                "position_pct": 0.0,
                "method": "reject_negative_edge",
                "full_kelly": round(f_full, 4),
                "rationale": (f"W={win_rate:.0%}, R={payoff_ratio:.2f} → f*={f_full:.3f}≤0，"
                              f"无正期望，建议不交易"),
            }

        pct = self.cfg.kelly_fraction * f_full
        pct = min(max(pct, 0.0), cap)
        return {
            "position_pct": round(pct, 4),
            "method": "kelly",
            "full_kelly": round(f_full, 4),
            "rationale": (f"W={win_rate:.0%}, R={payoff_ratio:.2f} → f*={f_full:.3f}，"
                          f"{self.cfg.kelly_fraction:g}×半凯利封顶 {cap:.0%} = {pct:.1%}"),
        }

    def size_from_stat(self, stat, base_position_pct: float) -> Dict:
        """从 stat-like 对象（win_rate/payoff_ratio/n）计算仓位。"""
        return self.size(
            win_rate=float(getattr(stat, "win_rate", 0.0)),
            payoff_ratio=float(getattr(stat, "payoff_ratio", 0.0)),
            n=int(getattr(stat, "n", 0)),
            base_position_pct=base_position_pct,
        )

    def build_pattern_table(self, stats_result, base_position_pct: float = 0.10) -> Dict[str, Dict]:
        """
        从 ``StrategyStatsResult`` 生成"分模式仓位标定表"。

        Returns: ``{pattern: sizing_dict}``，另含 ``__overall__`` 兜底项。
        """
        table: Dict[str, Dict] = {}
        by_pattern = getattr(stats_result, "by_pattern", {}) or {}
        for pattern, stat in by_pattern.items():
            table[pattern] = self.size_from_stat(stat, base_position_pct)
        overall = getattr(stats_result, "overall", None)
        if overall is not None:
            table["__overall__"] = self.size_from_stat(overall, base_position_pct)
        return table

    # ------------------------------------------------------------------
    # 持久化（闭环：标定结果落盘，供 Layer4 / 实盘消费）
    # ------------------------------------------------------------------
    @staticmethod
    def save_table(table: Dict[str, Dict], path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(table, f, ensure_ascii=False, indent=2)
        logger.info(f"[KellySizer] 仓位标定表已保存: {p}")

    @staticmethod
    def load_table(path) -> Dict[str, Dict]:
        p = Path(path)
        if not p.exists():
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # pragma: no cover - 文件容错
            logger.warning(f"[KellySizer] 读取 {p} 失败: {e}")
            return {}


__all__ = ["KellySizer"]
