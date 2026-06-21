"""核心模块 - A股短线情绪量化系统。"""

# 数据层
from core.data import (
    DataManager,
    IndustryMapper,
)

# 分析层
from core.analysis import (
    EmotionCycleEngine,
    EmotionCycle,
    SectorRotationTracker,
    SectorStage,
)

# 工具层 - 导出工具类和函数
from core.utils import (
    # 工具类
    DateUtils,
    StockCodeUtils,
    TimeUtils,
    CalculationUtils,
    ValidationUtils,
    # 日期工具函数（向后兼容）
    is_trade_date,
    get_nearest_trade_date,
    get_prev_trade_date,
    get_next_trade_date,
)

# 异常层级（P3-1）
from core.exceptions import (
    StockSentimentError,
    DataError,
    DataFetchError,
    ApiRateLimitError,
    CacheError,
    PipelineError,
    LayerExecutionError,
    EmptyResultError,
    PatternEvaluationError,
    ConfigError,
    ReportGenerationError,
)

__all__ = [
    # 数据层
    'DataManager',
    'IndustryMapper',
    # 分析层
    'EmotionCycleEngine',
    'EmotionCycle',
    'SectorRotationTracker',
    'SectorStage',
    # 工具层 - 工具类
    'DateUtils',
    'StockCodeUtils',
    'TimeUtils',
    'CalculationUtils',
    'ValidationUtils',
    # 工具层 - 日期函数
    'is_trade_date',
    'get_nearest_trade_date',
    'get_prev_trade_date',
    'get_next_trade_date',
    # 异常层级
    'StockSentimentError',
    'DataError',
    'DataFetchError',
    'ApiRateLimitError',
    'CacheError',
    'PipelineError',
    'LayerExecutionError',
    'EmptyResultError',
    'PatternEvaluationError',
    'ConfigError',
    'ReportGenerationError',
]
