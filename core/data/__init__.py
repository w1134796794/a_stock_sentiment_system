"""
数据层模块 - 负责数据获取、缓存和管理
"""
from core.data.data_manager import DataManager
from core.data.industry_mapper import (
    DCIndustryMapper,
    THSIndustryMapper,
    IndustryMapper,  # 向后兼容别名
)

__all__ = [
    'DataManager',
    'DCIndustryMapper',
    'THSIndustryMapper',
    'IndustryMapper',  # 向后兼容
]
