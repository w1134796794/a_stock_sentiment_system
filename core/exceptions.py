"""
项目统一异常层级（P3-1）

设计目标：
1. 区分"业务空数据"与"系统错误"，前者上层可继续，后者应中断流程。
2. 每个 Layer / 模块的失败都能用具体子异常表达，便于日志检索与重试策略。
3. 全部继承自 `StockSentimentError`，方便顶层 catch-all 拦截。

层级：
    StockSentimentError                     -- 项目根异常
    ├── DataError                           -- 数据相关
    │   ├── DataFetchError                  -- API 拉取失败
    │   ├── ApiRateLimitError               -- 接口限流
    │   ├── CacheError                      -- 缓存读写失败
    │   └── SchemaValidationError           -- 数据契约违反（与 schema_validator 共享）
    ├── PipelineError                       -- 流程相关
    │   ├── LayerExecutionError             -- 某一层执行失败
    │   └── EmptyResultError                -- 业务空数据（非异常但需告警）
    ├── PatternEvaluationError              -- 形态识别失败
    ├── ConfigError                         -- 配置加载/校验失败
    └── ReportGenerationError               -- 报告生成失败
"""
from __future__ import annotations

from typing import Optional


class StockSentimentError(Exception):
    """项目根异常 —— 所有自定义异常的统一基类"""

    def __init__(self, message: str = "", *, cause: Optional[BaseException] = None,
                 context: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.context = context or {}

    def __str__(self) -> str:
        parts = [self.message]
        if self.context:
            parts.append(f"context={self.context}")
        if self.cause is not None:
            parts.append(f"cause={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


# =============================================================================
# 数据相关异常
# =============================================================================

class DataError(StockSentimentError):
    """数据相关错误的基类"""


class DataFetchError(DataError):
    """从 Tushare / AkShare / 其他数据源拉取数据失败"""

    def __init__(self, message: str, *, source: str = "", endpoint: str = "",
                 **kwargs):
        ctx = kwargs.pop("context", {}) or {}
        ctx.update({"source": source, "endpoint": endpoint})
        super().__init__(message, context=ctx, **kwargs)


class ApiRateLimitError(DataFetchError):
    """API 限流（区别于一般 fetch 失败，可被调用方触发退避重试）"""

    def __init__(self, message: str = "API rate limit exceeded",
                 *, retry_after: Optional[float] = None, **kwargs):
        ctx = kwargs.pop("context", {}) or {}
        if retry_after is not None:
            ctx["retry_after"] = retry_after
        super().__init__(message, context=ctx, **kwargs)
        self.retry_after = retry_after


class CacheError(DataError):
    """本地缓存（内存或磁盘）读写失败"""


# 兼容 schema_validator.SchemaValidationError —— 既继承 ValueError 又纳入项目体系
try:
    from core.utils.schema_validator import SchemaValidationError as _SchemaValidationError
except Exception:  # pragma: no cover  (循环 import 兜底)
    _SchemaValidationError = None


class SchemaValidationError(DataError, ValueError):
    """数据契约违反"""


# 若 schema_validator 已经定义了 SchemaValidationError，将其追溯到本异常树
if _SchemaValidationError is not None and _SchemaValidationError is not SchemaValidationError:
    # 不强行替换 schema_validator 模块的类（避免破坏 isinstance 检查），
    # 而是允许两者并存：业务代码可统一 catch 本模块的 SchemaValidationError。
    pass


# =============================================================================
# 流程 / Pipeline 相关
# =============================================================================

class PipelineError(StockSentimentError):
    """Pipeline / 多层调度相关错误"""


class LayerExecutionError(PipelineError):
    """某一层 (Layer1~Layer5) 执行失败"""

    def __init__(self, message: str, *, layer: str = "", stage: str = "",
                 **kwargs):
        ctx = kwargs.pop("context", {}) or {}
        ctx.update({"layer": layer, "stage": stage})
        super().__init__(message, context=ctx, **kwargs)
        self.layer = layer
        self.stage = stage


class EmptyResultError(PipelineError):
    """业务空数据 —— 不是系统错误，但调用方需要感知"""


# =============================================================================
# 其它专用异常
# =============================================================================

class PatternEvaluationError(StockSentimentError):
    """形态识别策略评估失败"""

    def __init__(self, message: str, *, strategy: str = "", stock_code: str = "",
                 **kwargs):
        ctx = kwargs.pop("context", {}) or {}
        ctx.update({"strategy": strategy, "stock_code": stock_code})
        super().__init__(message, context=ctx, **kwargs)


class ConfigError(StockSentimentError):
    """配置加载 / 校验失败"""


class ReportGenerationError(StockSentimentError):
    """报告生成失败（Excel / Markdown / LLM）"""


__all__ = [
    "StockSentimentError",
    "DataError",
    "DataFetchError",
    "ApiRateLimitError",
    "CacheError",
    "SchemaValidationError",
    "PipelineError",
    "LayerExecutionError",
    "EmptyResultError",
    "PatternEvaluationError",
    "ConfigError",
    "ReportGenerationError",
]
