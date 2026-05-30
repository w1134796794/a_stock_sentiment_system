"""
执行层模块 - 负责交易计划生成和散户决策支持

历史说明：
  - 旧版 ``UnifiedExecutionEngine`` 已于 2026-05 移除，相关职责由
    ``core.pipeline.layer4_trade_plan.TradePlanLayer`` 接管。
  - 统一的交易计划契约定义在 ``core.pipeline.layer4_trade_plan.TradePlan``。
"""
from core.execution.retail_trader_support_v2 import RetailTraderSupportV2

__all__ = [
    'RetailTraderSupportV2',
]