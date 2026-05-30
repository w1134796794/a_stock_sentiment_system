"""
回测框架模块
提供基于交易计划的历史回测功能
"""
from .backtest_engine import BacktestEngine
from .trade_simulator import TradeSimulator
from .performance_analyzer import PerformanceAnalyzer
from .trade_calendar import TradeCalendar
from .replay_engine import ReplayEngine, ReplayPlan
from .plan_providers import CsvPlanProvider, PipelinePlanProvider
from .point_in_time import (
    AsOfPriceProvider,
    StaticPriceProvider,
    assert_no_future_data,
    has_future_data,
)
from .strategy_stats import Stat, StrategyStatsResult, compute_strategy_stats
from .walk_forward import WalkForwardValidator, WalkForwardResult
from .monte_carlo import (
    extract_trade_pnls,
    monte_carlo_resample,
    monte_carlo_from_report,
)

__all__ = [
    'BacktestEngine', 'TradeSimulator', 'PerformanceAnalyzer',
    'TradeCalendar', 'ReplayEngine', 'ReplayPlan',
    'CsvPlanProvider', 'PipelinePlanProvider',
    'AsOfPriceProvider', 'StaticPriceProvider',
    'assert_no_future_data', 'has_future_data',
    'Stat', 'StrategyStatsResult', 'compute_strategy_stats',
    'WalkForwardValidator', 'WalkForwardResult',
    'extract_trade_pnls', 'monte_carlo_resample', 'monte_carlo_from_report',
]

