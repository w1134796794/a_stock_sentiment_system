"""
Layer 2: 板块分析层 - 看板块（定方向）

核心职责：
  1. 热点板块检测（当日热门 + 持续性）
  2. 概念-行业共振分析（市场主线识别）
  3. 板块梯队完整性评估
  4. 板块轮动与持续性追踪

输入：涨停池数据、前一交易日数据
输出：热点概念/行业列表、主线主题、板块梯队报告
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


@dataclass
class SectorAnalysisResult:
    """板块分析结果"""
    trade_date: str = ""

    hot_concepts: List[str] = field(default_factory=list)
    hot_industries: List[str] = field(default_factory=list)
    hot_concepts_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    hot_industries_df: pd.DataFrame = field(default_factory=pd.DataFrame)

    concept_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    industry_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)

    main_themes_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    main_themes: List[str] = field(default_factory=list)

    concept_hierarchy: Dict = field(default_factory=dict)
    concept_hierarchy_report: str = ""

    sector_rotation_signal: str = ""
    sector_analysis_summary: str = ""


class SectorAnalysisLayer:
    """
    Layer 2: 板块分析层

    整合热点检测、持续性分析、共振分析、梯队评估
    """

    def __init__(self, data_manager):
        self.dm = data_manager
        self._orchestrator = None

    @property
    def orchestrator(self):
        if self._orchestrator is None:
            from core.analysis.sector_analysis_orchestrator import SectorAnalysisOrchestrator
            self._orchestrator = SectorAnalysisOrchestrator(self.dm, cache_enabled=True)
        return self._orchestrator

    def analyze(self, trade_date: str, zt_pool: pd.DataFrame,
                prev_trade_date: str = "") -> SectorAnalysisResult:
        """
        执行板块分析

        Args:
            trade_date: 交易日期
            zt_pool: 涨停池数据
            prev_trade_date: 前一交易日

        Returns:
            SectorAnalysisResult: 板块分析结果
        """
        result = SectorAnalysisResult(trade_date=trade_date)

        try:
            sector_result = self.orchestrator.analyze_all(trade_date, zt_pool=zt_pool)

            result.hot_concepts_df = sector_result.hot_concepts_df
            result.hot_industries_df = sector_result.hot_industries_df
            result.hot_concepts = sector_result.hot_concepts
            result.hot_industries = sector_result.hot_industries
            result.concept_persistence_df = sector_result.concept_persistence_df
            result.industry_persistence_df = sector_result.industry_persistence_df
            result.main_themes_df = sector_result.main_themes_df
            result.main_themes = sector_result.main_themes_df['板块名称'].tolist() if not sector_result.main_themes_df.empty else []
            result.concept_hierarchy = sector_result.concept_hierarchy
            result.concept_hierarchy_report = sector_result.concept_hierarchy_report
            result.hot_sectors_for_pattern = sector_result.hot_sectors_for_pattern

            result.sector_analysis_summary = self._generate_summary(result)

            logger.info(f"[Layer2] 板块分析完成: 热点概念{len(result.hot_concepts)}个, "
                       f"热点行业{len(result.hot_industries)}个, 主线{len(result.main_themes)}条")

        except Exception as e:
            logger.error(f"[Layer2] 板块分析失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

        return result

    def _generate_summary(self, result: SectorAnalysisResult) -> str:
        """生成板块分析摘要"""
        lines = []
        lines.append(f"=== 板块分析摘要 ({result.trade_date}) ===")

        if result.hot_concepts:
            lines.append(f"\n🔥 当日热门概念 (Top 5):")
            for i, c in enumerate(result.hot_concepts[:5], 1):
                lines.append(f"  {i}. {c}")

        if result.hot_industries:
            lines.append(f"\n🏭 当日热门行业 (Top 5):")
            for i, ind in enumerate(result.hot_industries[:5], 1):
                lines.append(f"  {i}. {ind}")

        if result.main_themes:
            lines.append(f"\n🎯 市场主线:")
            for theme in result.main_themes:
                lines.append(f"  - {theme}")

        if result.concept_hierarchy_report:
            lines.append(f"\n📊 板块梯队:")
            lines.append(result.concept_hierarchy_report)

        return "\n".join(lines)

    def get_hot_sectors_for_pattern(self, trade_date: str) -> List[Dict]:
        """获取热点板块数据（供模式识别使用）"""
        try:
            return self.orchestrator.get_cached_hot_sectors_for_pattern(trade_date)
        except Exception:
            return []