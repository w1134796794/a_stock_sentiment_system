"""
因子引擎核心模块

提供因子注册、配置加载、动态计算等核心功能。
支持通过YAML配置文件动态启用/禁用因子和调整权重。
"""
from .factor_registry import FactorRegistry, FactorDefinition, FactorCategory
from .factor_computer import FactorComputer, FactorResult

__all__ = [
    'FactorRegistry',
    'FactorDefinition',
    'FactorCategory',
    'FactorComputer',
    'FactorResult',
]