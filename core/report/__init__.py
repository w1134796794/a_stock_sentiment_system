"""
报告层模块 - 负责报告生成和输出

历史说明：
  - 旧版 ``ReportGenerator`` (V1) 已于 2026-05 删除。
  - 当前生产报告生成器为 ``ReportGeneratorV2``。
"""
from core.report.report_generator_v2 import ReportGeneratorV2
from core.report.sections import (
    ReportSection,
    BaseSection,
    LegacyMethodSection,
    default_sections,
)

__all__ = [
    'ReportGeneratorV2',
    'ReportSection',
    'BaseSection',
    'LegacyMethodSection',
    'default_sections',
]
