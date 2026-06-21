"""重演引擎的交易计划来源。"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pandas as pd
import loguru

from backtest.replay_engine import ReplayPlan

logger = loguru.logger

# Layer4 落盘 CSV 里 仓位 字段 → 仓位比例
_POSITION_PCT = {"light": 0.10, "medium": 0.15, "heavy": 0.20}


def _clean_str(value) -> str:
    """CSV 单元格安全转字符串：None / 空 / pandas NaN → 空串。

    pandas 把空单元格读成 ``float('nan')``，``str(nan)`` 会得到字面量 ``"nan"``；
    若当板块名会把"无板块"的票误并成同一假板块，``bool(nan)`` 还恒为 True。
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_str(value).lower() in {"true", "1", "是", "yes", "y"}


class CsvPlanProvider:
    """从 ``交易计划_{date}.csv`` 读取买入计划。"""

    def __init__(self, trade_plans_dir, position_pct_map: Optional[dict] = None):
        self.dir = Path(trade_plans_dir)
        self.pct_map = position_pct_map or _POSITION_PCT

    def __call__(self, making_date: str) -> List[ReplayPlan]:
        path = self.dir / f"交易计划_{making_date}.csv"
        if not path.exists():
            path = self.dir / f"trade_plans_{making_date}.csv"
            if not path.exists():
                return []
        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(f"[CsvPlanProvider] 读取 {path} 失败: {e}")
            return []
        if df.empty:
            return []
        if "动作" in df.columns:
            df = df[df["动作"] == "买入"]

        plans: List[ReplayPlan] = []
        for _, row in df.iterrows():
            sectors_raw = _clean_str(row.get("共振板块")) or _clean_str(row.get("所属板块"))
            sectors = [s.strip() for s in sectors_raw.split(",") if s.strip()]
            pos_str = (_clean_str(row.get("仓位")) or "light").lower()
            plans.append(ReplayPlan(
                code=_clean_str(row.get("代码")),
                name=_clean_str(row.get("名称")),
                pattern=_clean_str(row.get("模式")),
                target_price=float(row.get("目标价", 0) or 0),
                stop_price=float(row.get("止损价", 0) or 0),
                take_profit_price=float(row.get("止盈价", 0) or 0),
                position_pct=self.pct_map.get(pos_str, 0.10),
                sectors=sectors,
                hot_resonance=_as_bool(row.get("热点共振", False)),
            ))
        return plans


__all__ = ["CsvPlanProvider"]
