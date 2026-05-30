"""
模式策略层 - Pattern Strategy Layer

公共契约（推荐新代码使用）：
  - ``PatternSignal``：统一信号契约
  - ``PatternContext``：统一输入上下文
  - ``PatternStrategy``：策略 Protocol
  - ``StrategyRegistry``、``default_registry``：注册中心

历史策略类（保持向后兼容）：
  - ``WeakToStrongStrategy``
  - ``SecondBoardDragonStrategy``
  - ``HotspotFirstBoardStrategy``
  - ``DragonSecondWaveStrategyV2``
  - ``DragonDynamicManager``
"""

from core.pattern.base import (
    PatternSignal,
    PatternContext,
    PatternStrategy,
    StrategyRegistry,
    default_registry,
)

# 调度器（聚合所有策略，提供 scan_all_patterns 入口）
from core.pattern.pattern_recognition import PatternRecognition

__all__ = [
    'PatternSignal',
    'PatternContext',
    'PatternStrategy',
    'StrategyRegistry',
    'default_registry',
    'PatternRecognition',
]
