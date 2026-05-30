"""
重演引擎的交易计划来源（B-2c）

两种 ``plan_provider`` 实现，均为可调用对象 ``(making_date) -> List[ReplayPlan]``：

- ``CsvPlanProvider``：读取已落盘的 ``交易计划_{date}.csv``（Layer4 产出、且已过
  L4.5 风控闸门）。轻量、离线，适合对历史已跑过的日子做执行级回放。

- ``PipelinePlanProvider``：以历史某日为"今天"重新跑整条 ``ReviewPipeline``（含
  L4.5 风控闸门），从 ``trade_plan_result`` 取**风控后仓位 > 0** 的计划——这是真正的
  "历史重演"。它依赖数据/Tushare token，按交易日 point-in-time 取数（pipeline 本身
  就是按 trade_date 驱动的），因此天然规避未来函数；本类不做单测（需实数据），但保证
  import 安全、逻辑可读。
"""
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


class PipelinePlanProvider:
    """历史重演：以 making_date 为"今天"重跑流水线，取风控后仓位>0 的计划。"""

    def __init__(self, data_manager, industry_mapper=None, config=None):
        self.dm = data_manager
        self.mapper = industry_mapper
        self.config = config
        self._pipeline = None

    def _get_pipeline(self):
        if self._pipeline is None:
            from core.pipeline.review_pipeline import ReviewPipeline

            self._pipeline = ReviewPipeline(self.dm, self.mapper)
        return self._pipeline

    def __call__(self, making_date: str) -> List[ReplayPlan]:
        try:
            ctx = self._get_pipeline().execute(making_date)
        except Exception as e:
            logger.warning(f"[PipelinePlanProvider] {making_date} 流水线执行失败: {e}")
            return []

        result = getattr(ctx, "trade_plan_result", None)
        if result is None or not getattr(result, "plans", None):
            return []

        plans: List[ReplayPlan] = []
        for p in result.plans:
            pct = float(getattr(p, "position_pct", 0.0) or 0.0)
            if pct <= 0:
                continue  # 已被风控闸门拒绝 / 观察
            entry = float(getattr(p, "entry_price", 0.0) or 0.0)
            stop_pct = float(getattr(p, "stop_loss_pct", 0.0) or 0.0)
            tp_pct = float(getattr(p, "take_profit_pct", 0.0) or 0.0)
            sectors = list(getattr(p, "resonance_sectors", []) or [])
            plans.append(ReplayPlan(
                code=str(getattr(p, "stock_code", "")),
                name=str(getattr(p, "stock_name", "")),
                pattern=str(getattr(p, "pattern_type", "")),
                target_price=entry,
                stop_price=entry * (1 + stop_pct / 100) if entry > 0 else 0.0,
                take_profit_price=entry * (1 + tp_pct / 100) if entry > 0 else 0.0,
                position_pct=pct,
                sectors=sectors,
                hot_resonance=bool(getattr(p, "hot_resonance", False)),
            ))
        return plans


__all__ = ["CsvPlanProvider", "PipelinePlanProvider"]
