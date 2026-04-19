"""
回测框架模块
提供基于交易计划的历史回测功能
"""
from .backtest_engine import BacktestEngine
from .trade_simulator import TradeSimulator
from .performance_analyzer import PerformanceAnalyzer

__all__ = ['BacktestEngine', 'TradeSimulator', 'PerformanceAnalyzer']