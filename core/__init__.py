"""
核心模块 - A股短线情绪量化系统

目录结构:
- data/: 数据获取层
- analysis/: 分析引擎层
- pattern/: 策略模式层
- execution/: 执行层
- report/: 报告层
"""

# 数据层
from core.data import (
    DataManager,
    IndustryMapper,
)

# 分析层
from core.analysis import (
    PatternRecognition,
    EmotionCycleEngine,
    EmotionCycle,
    SectorRotationTracker,
    SectorStage,
)

# 执行层
from core.execution import (
    RetailTraderSupportV2,
)

# 报告层
from core.report import ReportGeneratorV2

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
    'PatternRecognition',
    'EmotionCycleEngine',
    'EmotionCycle',
    'SectorRotationTracker',
    'SectorStage',
    # 执行层
    'RetailTraderSupportV2',
    # 报告层
    'ReportGeneratorV2',
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
