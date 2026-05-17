"""
板块分析统筹入口 - Sector Analysis Orchestrator

核心职责：
1. 统筹管理分析模块下的所有板块分析功能
2. 提供热点数据缓存机制，避免重复计算
3. 为main.py和模式扫描提供统一的分析数据接口

设计原则：
- 单一入口：所有板块分析通过此类获取
- 数据缓存：热点板块数据缓存，支持多次使用
- 延迟加载：按需计算，避免不必要的分析
- 数据一致性：确保各模块使用的是同一份热点数据

使用示例：
    orchestrator = SectorAnalysisOrchestrator(data_manager)
    
    # 执行完整分析（自动缓存）
    result = orchestrator.analyze_all(trade_date)
    
    # 获取缓存的热点数据（模式扫描使用）
    hot_concepts = orchestrator.get_cached_hot_concepts()
    hot_sectors = orchestrator.get_cached_hot_sectors()
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime
import loguru

from core.analysis.ths_sector_tracker import THSSectorTracker
from core.analysis.concept_board_hierarchy import ConceptBoardHierarchyAnalyzer

logger = loguru.logger


@dataclass
class SectorAnalysisResult:
    """板块分析完整结果"""
    trade_date: str
    
    # 热点板块数据（原始）
    hot_concepts_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    hot_industries_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    
    # 持续性分析结果
    concept_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    industry_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    
    # 主线识别结果（替代共振分析）
    main_themes_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    
    # 概念连板梯队
    concept_hierarchy: Dict = field(default_factory=dict)
    concept_hierarchy_report: str = ""
    
    # 汇总信息
    hot_concepts: List[str] = field(default_factory=list)
    hot_industries: List[str] = field(default_factory=list)
    main_themes: List[str] = field(default_factory=list)
    
    # 用于模式扫描的热点板块列表（格式化的）
    hot_sectors_for_pattern: List[Dict] = field(default_factory=list)
    
    # 元数据
    analysis_time: datetime = field(default_factory=datetime.now)
    is_cached: bool = False


class SectorAnalysisOrchestrator:
    """
    板块分析统筹入口
    
    统一管理板块分析流程，提供数据缓存功能
    """
    
    def __init__(self, data_manager, cache_enabled: bool = True):
        self.dm = data_manager
        self.cache_enabled = cache_enabled
        
        # 初始化分析器
        self.sector_tracker = THSSectorTracker(self.dm)
        self.concept_hierarchy_analyzer = ConceptBoardHierarchyAnalyzer(self.dm)
        
        # 缓存数据
        self._cache: Dict[str, SectorAnalysisResult] = {}
        self._current_date: Optional[str] = None
        self._current_result: Optional[SectorAnalysisResult] = None
        
        logger.info("[SectorAnalysisOrchestrator] 初始化完成")
    
    def analyze_all(self, trade_date: str, 
                    zt_pool: Optional[pd.DataFrame] = None,
                    use_cache: bool = True) -> SectorAnalysisResult:
        """
        执行完整的板块分析流程
        
        Args:
            trade_date: 交易日期（YYYYMMDD）
            zt_pool: 涨停池数据（可选，用于概念连板梯队分析）
            use_cache: 是否使用缓存
            
        Returns:
            SectorAnalysisResult: 完整的分析结果
        """
        # 检查缓存
        if use_cache and self.cache_enabled and trade_date in self._cache:
            logger.info(f"[SectorAnalysisOrchestrator] 使用缓存数据: {trade_date}")
            self._current_date = trade_date
            self._current_result = self._cache[trade_date]
            self._current_result.is_cached = True
            return self._current_result
        
        logger.info("=" * 80)
        logger.info(f"[SectorAnalysisOrchestrator] 开始完整板块分析: {trade_date}")
        logger.info("=" * 80)
        
        result = SectorAnalysisResult(trade_date=trade_date)
        
        # 1. 热点概念识别
        logger.info("[1/6] 识别热点概念板块...")
        result.hot_concepts_df = self.sector_tracker.analyze_concept_sectors(
            trade_date, top_n=20
        )
        if not result.hot_concepts_df.empty and 'is_hot' in result.hot_concepts_df.columns:
            hot_mask = result.hot_concepts_df['is_hot'].astype(bool)
            result.hot_concepts = result.hot_concepts_df[hot_mask]['name'].tolist()
            logger.info(f"[OK] 识别到 {len(result.hot_concepts)} 个热点概念")
        else:
            logger.info("  未识别到热点概念")
        
        # 2. 热点行业识别
        logger.info("[2/6] 识别热点行业板块...")
        result.hot_industries_df = self.sector_tracker.analyze_industry_sectors(
            trade_date, top_n=20
        )
        if not result.hot_industries_df.empty and 'is_hot' in result.hot_industries_df.columns:
            hot_mask = result.hot_industries_df['is_hot'].astype(bool)
            result.hot_industries = result.hot_industries_df[hot_mask]['name'].tolist()
            logger.info(f"[OK] 识别到 {len(result.hot_industries)} 个热点行业")
        else:
            logger.info("  未识别到热点行业")
        
        # 3. 概念持续性分析
        logger.info("[3/6] 分析概念板块持续性...")
        result.concept_persistence_df = self.sector_tracker.analyze_concept_persistence(
            trade_date, 
            top_n=10, 
            lookback_days=10, 
            hot_concepts_df=result.hot_concepts_df
        )
        if not result.concept_persistence_df.empty:
            logger.info(f"[OK] 发现 {len(result.concept_persistence_df)} 个持续热门概念")
        else:
            logger.info("  无持续热门概念")
        
        # 4. 行业持续性分析
        logger.info("[4/6] 分析行业板块持续性...")
        result.industry_persistence_df = self.sector_tracker.analyze_industry_persistence(
            trade_date, 
            top_n=10, 
            lookback_days=10, 
            hot_industries_df=result.hot_industries_df
        )
        if not result.industry_persistence_df.empty:
            logger.info(f"[OK] 发现 {len(result.industry_persistence_df)} 个持续热门行业")
        else:
            logger.info("  无持续热门行业")
        
        # 5. 主线识别（四维评分：涨停集中度+梯队完整性+持续性+龙头强度）
        logger.info("[5/6] 识别市场主线（四维评分模型）...")
        result.main_themes_df = self.sector_tracker.identify_main_themes(
            trade_date,
            hot_concepts_df=result.hot_concepts_df,
            hot_industries_df=result.hot_industries_df,
            concept_persistence_df=result.concept_persistence_df,
            industry_persistence_df=result.industry_persistence_df,
            top_n=10
        )
        if not result.main_themes_df.empty:
            result.main_themes = result.main_themes_df['板块名称'].tolist()
            logger.info(f"[OK] 识别 {len(result.main_themes_df)} 条市场主线")
        else:
            logger.info("  未识别到明显主线")
        
        # 6. 概念连板梯队分析
        logger.info("[6/6] 分析概念连板梯队...")
        if zt_pool is not None and not zt_pool.empty:
            result.concept_hierarchy = self.concept_hierarchy_analyzer.analyze_hierarchy(
                zt_pool, trade_date
            )
            if result.concept_hierarchy:
                result.concept_hierarchy_report = (
                    self.concept_hierarchy_analyzer.format_hierarchy_for_report(
                        result.concept_hierarchy, top_n=10
                    )
                )
                logger.info(f"[OK] 概念连板梯队分析完成，共 {len(result.concept_hierarchy)} 个概念")
            else:
                logger.info("  无概念连板梯队数据")
        else:
            logger.info("  涨停池为空，跳过概念连板梯队分析")
        
        # 构建用于模式扫描的热点板块列表
        result.hot_sectors_for_pattern = self._build_hot_sectors_for_pattern(result)
        
        # 保存到缓存
        if self.cache_enabled:
            self._cache[trade_date] = result
            logger.info(f"[SectorAnalysisOrchestrator] 数据已缓存: {trade_date}")
        
        self._current_date = trade_date
        self._current_result = result
        
        logger.info("=" * 80)
        logger.info(f"[SectorAnalysisOrchestrator] 板块分析完成")
        logger.info("=" * 80)
        
        return result
    
    def _build_hot_sectors_for_pattern(self, result: SectorAnalysisResult) -> List[Dict]:
        """
        构建用于模式扫描的热点板块列表

        优先使用主线识别结果，补充持续性分析中的活跃板块
        包含 ts_code 和 member_codes 用于个股-板块匹配
        """
        hot_sectors = []
        seen_names = set()

        def _enrich_member_codes(sector_name: str, ts_code: str) -> set:
            """获取板块成分股代码集合"""
            if not ts_code:
                return set()
            try:
                members_df = self.sector_tracker.get_sector_members(ts_code)
                if not members_df.empty:
                    if 'con_code' in members_df.columns:
                        return set(str(c).split('.')[0].zfill(6) for c in members_df['con_code'].values)
                    elif 'code' in members_df.columns:
                        return set(str(c).split('.')[0].zfill(6) for c in members_df['code'].values)
            except Exception as e:
                logger.debug(f"[_build_hot_sectors] 获取{sector_name}成分股失败: {e}")
            return set()

        # 优先从主线识别结果中提取
        if not result.main_themes_df.empty:
            for _, row in result.main_themes_df.iterrows():
                name = row.get('板块名称', '')
                if name in seen_names:
                    continue
                seen_names.add(name)

                stage = row.get('所处阶段', '')
                if stage in ['启动期', '加速期', '高潮期', '主升浪', '成长期', '成熟期']:
                    ts_code = row.get('板块代码', '')
                    member_codes = _enrich_member_codes(name, ts_code)

                    hot_sectors.append({
                        'sector_name': name,
                        'sector_type': row.get('板块类型', ''),
                        'ts_code': ts_code,
                        'member_codes': member_codes,
                        'stats': {
                            '涨停家数': row.get('涨停家数', 0),
                            '梯队详情': row.get('梯队详情', ''),
                            '最高连板': row.get('最高连板', 0),
                        },
                        'trend_stage': stage,
                        'action': row.get('操作建议', ''),
                        'confidence': row.get('综合评分', 50) / 100,
                        'hot_days': row.get('热点天数', 0),
                        'hot_frequency': row.get('持续性评分', 0) / 100,
                    })

        # 补充持续性分析中的活跃板块（未出现在主线中的）
        all_persistence_df = pd.concat(
            [result.concept_persistence_df, result.industry_persistence_df],
            ignore_index=True
        )

        if not all_persistence_df.empty:
            for _, row in all_persistence_df.iterrows():
                name = row.get('板块名称', '')
                if name in seen_names:
                    continue
                seen_names.add(name)

                stage = row.get('所处阶段', '')
                if stage in ['启动期', '加速期', '高潮期', '主升浪']:
                    ts_code = row.get('ts_code', '') or row.get('板块代码', '')
                    member_codes = _enrich_member_codes(name, ts_code)

                    hot_sectors.append({
                        'sector_name': name,
                        'sector_type': row.get('板块类型', ''),
                        'ts_code': ts_code,
                        'member_codes': member_codes,
                        'stats': {'涨停家数': row.get('涨停家数', 0)},
                        'trend_stage': stage,
                        'action': row.get('操作建议', ''),
                        'confidence': row.get('持续性评分', 50) / 100,
                        'hot_days': row.get('热点天数', 0),
                        'hot_frequency': row.get('热点频率', 0),
                    })

        hot_sectors.sort(key=lambda x: x['confidence'], reverse=True)

        logger.info(f"[SectorAnalysisOrchestrator] 构建热点板块列表: {len(hot_sectors)}个")
        return hot_sectors
    
    # ==================== 缓存数据获取接口 ====================
    
    def get_cached_result(self, trade_date: Optional[str] = None) -> Optional[SectorAnalysisResult]:
        """获取缓存的完整分析结果"""
        date = trade_date or self._current_date
        if date and date in self._cache:
            return self._cache[date]
        return self._current_result
    
    def get_cached_hot_concepts(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """获取缓存的热点概念数据"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.hot_concepts_df
        return pd.DataFrame()
    
    def get_cached_hot_industries(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """获取缓存的热点行业数据"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.hot_industries_df
        return pd.DataFrame()
    
    def get_cached_concept_persistence(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """获取缓存的概念持续性分析结果"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.concept_persistence_df
        return pd.DataFrame()
    
    def get_cached_industry_persistence(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """获取缓存的行业持续性分析结果"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.industry_persistence_df
        return pd.DataFrame()
    
    def get_cached_main_themes(self, trade_date: Optional[str] = None) -> pd.DataFrame:
        """获取缓存的主线识别结果"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.main_themes_df
        return pd.DataFrame()
    
    def get_cached_hot_sectors_for_pattern(self, trade_date: Optional[str] = None) -> List[Dict]:
        """
        获取缓存的、用于模式扫描的热点板块列表
        
        这是模式识别模块的主要接口
        """
        result = self.get_cached_result(trade_date)
        if result:
            count = len(result.hot_sectors_for_pattern)
            logger.info(f"[SectorAnalysisOrchestrator] get_cached_hot_sectors_for_pattern: "
                       f"trade_date={trade_date}, 缓存命中, hot_sectors={count}个")
            return result.hot_sectors_for_pattern
        logger.warning(f"[SectorAnalysisOrchestrator] get_cached_hot_sectors_for_pattern: "
                      f"trade_date={trade_date}, 缓存未命中! _cache keys={list(self._cache.keys())}, "
                      f"_current_date={self._current_date}")
        return []
    
    def get_cached_concept_hierarchy(self, trade_date: Optional[str] = None) -> Dict:
        """获取缓存的概念连板梯队数据"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.concept_hierarchy
        return {}
    
    # ==================== 缓存管理接口 ====================
    
    def clear_cache(self, trade_date: Optional[str] = None):
        """
        清除缓存
        
        Args:
            trade_date: 指定日期，如为None则清除所有缓存
        """
        if trade_date:
            if trade_date in self._cache:
                del self._cache[trade_date]
                logger.info(f"[SectorAnalysisOrchestrator] 已清除缓存: {trade_date}")
        else:
            self._cache.clear()
            logger.info("[SectorAnalysisOrchestrator] 已清除所有缓存")
    
    def is_cached(self, trade_date: str) -> bool:
        """检查指定日期是否有缓存"""
        return trade_date in self._cache
    
    def get_cache_info(self) -> Dict:
        """获取缓存信息"""
        return {
            'cached_dates': list(self._cache.keys()),
            'cache_count': len(self._cache),
            'cache_enabled': self.cache_enabled,
            'current_date': self._current_date,
        }
    
    # ==================== 便捷方法 ====================
    
    def get_hot_concept_names(self, trade_date: Optional[str] = None) -> List[str]:
        """获取热点概念名称列表"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.hot_concepts
        return []
    
    def get_hot_industry_names(self, trade_date: Optional[str] = None) -> List[str]:
        """获取热点行业名称列表"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.hot_industries
        return []
    
    def get_main_theme_names(self, trade_date: Optional[str] = None) -> List[str]:
        """获取市场主线名称列表"""
        result = self.get_cached_result(trade_date)
        if result:
            return result.main_themes
        return []
    
    def get_strong_concepts(self, trade_date: Optional[str] = None, 
                           min_board_height: int = 3) -> List[str]:
        """
        获取强势概念列表（有高度连板的概念）
        
        Args:
            trade_date: 交易日期
            min_board_height: 最小连板高度
            
        Returns:
            List[str]: 强势概念名称列表
        """
        result = self.get_cached_result(trade_date)
        if result and result.concept_hierarchy:
            strong_concepts = []
            for concept_name, data in result.concept_hierarchy.items():
                max_height = data.get('max_board_height', 0)
                if max_height >= min_board_height:
                    strong_concepts.append(concept_name)
            return strong_concepts
        return []
