"""
分析层模块 - 负责数据分析、模式识别和策略执行
"""
from core.analysis.pattern_recognition import PatternRecognition
from core.analysis.emotion_cycle_engine import EmotionCycleEngine, EmotionCycle
# 使用同花顺板块追踪器替换旧版
from core.analysis.ths_sector_tracker import THSSectorTracker as SectorRotationTracker
from core.analysis.ths_sector_tracker import THSSectorMetrics as SectorStage
from core.analysis.concept_industry_validator import (
    ConceptIndustryValidator,
    SignalType,
    SignalStrength,
    CrossValidationResult
)

__all__ = [
    'PatternRecognition',
    'EmotionCycleEngine',
    'EmotionCycle',
    'SectorRotationTracker',
    'SectorStage',
    'ConceptIndustryValidator',
    'SignalType',
    'SignalStrength',
    'CrossValidationResult',
]
