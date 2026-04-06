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
    TradeDateManager,
    get_trade_date_manager,
    is_trade_date,
    get_nearest_trade_date,
    get_prev_trade_date,
    get_next_trade_date,
    validate_trade_date,
    IndustryMapper,
    TushareShareholderFetcher,
)

# 分析层
from core.analysis import (
    SentimentEngine,
    SectorHeatCalculatorV2,
    TrendStage,
    SectorHeatCalculatorV3,
    PatternRecognition,
)

# 执行层
from core.execution import (
    UnifiedExecutionEngine,
    RetailTraderSupportV2,
)

# 报告层
from core.report import ReportGenerator

__all__ = [
    # 数据层
    'DataManager',
    'TradeDateManager',
    'get_trade_date_manager',
    'is_trade_date',
    'get_nearest_trade_date',
    'get_prev_trade_date',
    'get_next_trade_date',
    'validate_trade_date',
    'IndustryMapper',
    'TushareShareholderFetcher',
    # 分析层
    'SentimentEngine',
    'SectorHeatCalculatorV2',
    'TrendStage',
    'SectorHeatCalculatorV3',
    'PatternRecognition',
    # 执行层
    'UnifiedExecutionEngine',
    'RetailTraderSupportV2',
    # 报告层
    'ReportGenerator',
]
