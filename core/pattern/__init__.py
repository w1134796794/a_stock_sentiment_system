"""
模式策略层 - Pattern Strategy Layer

公共契约（推荐新代码使用）：
  - ``PatternSignal``：统一信号契约
  - ``PatternContext``：统一输入上下文
  - ``PatternStrategy``：策略 Protocol
  - ``StrategyRegistry``、``default_registry``：注册中心

策略实现（由 ``PatternRecognition`` 聚合调度）：
  - ``WeakToStrongStrategy``（弱转强 / 动态龙头池）
  - ``HotspotFirstBoardStrategy``（首板突破）
  - ``DragonSecondWaveStrategyV2``（龙二波）
  - 二板定龙：逻辑内联于 ``PatternRecognition.detect_second_board_dragon``
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