"""
模式策略统一基础设施 - Pattern Strategy Base Infrastructure

设计目标：
  - 统一的 ``PatternSignal`` 输出契约
  - 统一的 ``PatternContext`` 输入数据载体
  - 最小化的 ``PatternStrategy`` Protocol（鸭子类型）
  - ``StrategyRegistry`` 注册中心，支持装饰器注册

向后兼容：
  - ``PatternSignal`` 字段与 ``core.analysis.pattern_recognition.PatternSignal`` 保持一致
  - 已有策略类（``WeakToStrongStrategy``、``HotspotFirstBoardStrategy`` 等）
    可保留原 API；只需创建一个薄适配器（实现 ``detect(ctx)``）即可加入注册中心。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable

import loguru
import pandas as pd

logger = loguru.logger


# =====================================================================
# 信号契约
# =====================================================================
@dataclass
class PatternSignal:
    """统一模式信号契约（与旧 `core.analysis.pattern_recognition.PatternSignal` 字段一致）"""

    pattern_type: str
    stock_code: str
    stock_name: str
    confidence: float = 0.0
    description: str = ""
    key_metrics: Dict[str, Any] = field(default_factory=dict)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size: str = "medium"  # light / medium / heavy
    validation_rules: List[str] = field(default_factory=list)
    l2_industry: str = ""
    is_dual_resonance: bool = False


# =====================================================================
# 输入上下文
# =====================================================================
@dataclass
class PatternContext:
    """所有模式策略共享的输入上下文。

    Strategy 在 ``detect(ctx)`` 中通过本对象按需取数；调度层只需构造一次。
    """

    trade_date: str
    prev_trade_date: str = ""
    day_before_prev: str = ""

    today_zt: Optional[pd.DataFrame] = None         # 今日涨停池
    yesterday_zt: Optional[pd.DataFrame] = None     # 昨日涨停池
    day_before_zt: Optional[pd.DataFrame] = None    # 前日涨停池
    history_pools: Dict[str, pd.DataFrame] = field(default_factory=dict)

    today_daily: Optional[pd.DataFrame] = None       # 今日全市场日线
    today_tick: Dict[str, Any] = field(default_factory=dict)

    stock_to_ths_industry: Dict[str, str] = field(default_factory=dict)
    stock_to_ths_concept: Dict[str, str] = field(default_factory=dict)
    stock_to_hot_sectors: Dict[str, list] = field(default_factory=dict)
    all_hot_member_codes: set = field(default_factory=set)

    hot_sectors: List[str] = field(default_factory=list)
    market_emotion: str = "震荡期"

    extras: Dict[str, Any] = field(default_factory=dict)


# =====================================================================
# Strategy 协议
# =====================================================================
@runtime_checkable
class PatternStrategy(Protocol):
    """模式策略最小契约（鸭子类型）

    实现该协议只需提供：
      - ``name``: 策略名称（用于日志、注册键）
      - ``detect(ctx: PatternContext) -> List[PatternSignal]``: 扫描入口

    可选实现：
      - ``required_inputs() -> List[str]``: 声明依赖的 ctx 字段（用于校验/调试）
      - ``priority``: int，用于排序
    """

    name: str

    def detect(self, ctx: PatternContext) -> List[PatternSignal]:
        ...


# =====================================================================
# 注册中心
# =====================================================================
class StrategyRegistry:
    """模式策略注册中心。

    使用方式：
        registry = StrategyRegistry()

        @registry.register("弱转强")
        class WeakToStrongAdapter:
            name = "弱转强"
            def detect(self, ctx): ...
    """

    def __init__(self):
        self._strategies: Dict[str, PatternStrategy] = {}

    def register(self, name: Optional[str] = None):
        """装饰器：把策略类注册进 registry。

        - 如果传入 ``name``，使用该名字作为 key
        - 否则使用类的 ``name`` 类属性或 ``__name__``
        """

        def _decorator(cls):
            inst = cls() if isinstance(cls, type) else cls
            key = name or getattr(inst, 'name', None) or type(inst).__name__
            if key in self._strategies:
                logger.warning(f"[StrategyRegistry] 重复注册 {key}，覆盖旧实现")
            self._strategies[key] = inst
            return cls

        return _decorator

    def register_instance(self, name: str, instance: PatternStrategy) -> None:
        """直接注册一个已实例化的策略对象（用于运行时注入）"""
        if name in self._strategies:
            logger.warning(f"[StrategyRegistry] 重复注册 {name}，覆盖旧实现")
        self._strategies[name] = instance

    def unregister(self, name: str) -> None:
        self._strategies.pop(name, None)

    def get(self, name: str) -> Optional[PatternStrategy]:
        return self._strategies.get(name)

    def list_names(self) -> List[str]:
        return list(self._strategies.keys())

    def all(self) -> Iterable[PatternStrategy]:
        return list(self._strategies.values())

    def run_all(self, ctx: PatternContext) -> Dict[str, List[PatternSignal]]:
        """遍历所有策略执行 detect，按策略名归集信号。"""
        results: Dict[str, List[PatternSignal]] = {}
        for name, strategy in self._strategies.items():
            try:
                signals = strategy.detect(ctx) or []
                results[name] = signals
                logger.info(f"[StrategyRegistry] {name}: 检出 {len(signals)} 条信号")
            except Exception as e:
                logger.error(f"[StrategyRegistry] {name} 执行失败: {e}")
                results[name] = []
        return results


# 进程级默认注册中心（也可创建独立 registry 用于测试）
default_registry = StrategyRegistry()


__all__ = [
    "PatternSignal",
    "PatternContext",
    "PatternStrategy",
    "StrategyRegistry",
    "default_registry",
]
