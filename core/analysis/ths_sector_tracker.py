"""
同花顺板块追踪器 - 概念+行业统一分析

核心设计：
1. 统一使用同花顺数据（ths_index/ths_daily/ths_member）
2. 同时追踪概念指数和行业指数
3. 通过成分股重叠度建立概念-行业关联
4. 热点板块识别基于涨幅+成交额+涨停家数

架构重构：
- 热点识别逻辑 -> HotSpotDetector
- 持续性分析逻辑 -> SectorPersistenceAnalyzer
- THSSectorTracker 负责协调和数据获取

字段命名规范：
- 股票代码统一使用 'code'（6位数字）
- 股票名称统一使用 'name'
- 板块代码统一使用 'ts_code'（带后缀）
- 板块名称统一使用 'name'
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import loguru

from core.utils import DataFrameFieldMapper, StockCodeUtils
from core.analysis.sector_hotspot_detector import HotSpotDetector
from core.analysis.sector_persistence_analyzer import SectorPersistenceAnalyzer

# 导入外置配置
from config.config_loader import (
    get_sector_tracker_config,
    get_sector_params,
    get_sector_analyze_config,
    get_persistence_config,
    get_internal_structure_config,
    get_resonance_config,
    get_sector_relation_config,
)

logger = loguru.logger


@dataclass
class THSSectorData:
    """同花顺板块数据"""
    ts_code: str               # 板块代码（如 885001.TI）
    name: str                  # 板块名称
    sector_type: str           # 类型：概念/行业
    trade_date: str            # 交易日期
    pct_change: float          # 涨跌幅
    amount: float              # 成交额（千元）
    vol: float                 # 成交量（手）
    up_count: int = 0          # 涨停家数（需额外计算）
    cons_count: int = 0        # 连板家数（需额外计算）


@dataclass
class THSSectorMetrics:
    """板块指标"""
    # 基础指标
    pct_change: float = 0.0        # 当日涨跌幅
    amount: float = 0.0            # 成交额
    amount_change: float = 0.0     # 成交额变化率

    # 强度指标
    up_count: int = 0              # 涨停家数
    cons_count: int = 0            # 连板家数
    up_ratio: float = 0.0          # 涨停占比（涨停数/成分股数）

    # 趋势指标
    rank: int = 0                  # 涨幅排名
    rank_change: int = 0           # 排名变化

    # 综合评分
    composite_score: float = 0.0   # 综合得分
    is_hot: bool = False           # 是否热点


class THSSectorTracker:
    """
    同花顺板块追踪器

    功能：
    1. 获取同花顺概念和行业板块数据
    2. 计算板块强度指标
    3. 建立概念-行业关联（通过成分股重叠）
    4. 识别热点板块
    """

    def __init__(self, data_manager, config: Optional[Dict] = None):
        self.dm = data_manager
        self._concept_list: Optional[pd.DataFrame] = None
        self._industry_list: Optional[pd.DataFrame] = None
        self._member_cache: Dict[str, pd.DataFrame] = {}  # 成分股缓存
        
        # 加载配置（优先使用传入配置，其次YAML配置，最后默认配置）
        if config:
            # 使用传入的配置
            self.config = config
        else:
            # 从YAML加载配置
            self.config = self._load_yaml_config()
        
        # 加载各模块配置
        sector_params = self.config.get('sector_params', self._get_default_sector_params())
        # 转换YAML格式（concept/industry）到代码格式（概念/行业）
        self.sector_params = self._convert_sector_params(sector_params)
        self.resonance_config = self.config.get('resonance', get_resonance_config())
        self.structure_config = self.config.get('internal_structure', get_internal_structure_config())
        
        # 初始化热点识别器和持续性分析器
        self.hotspot_detector = HotSpotDetector(data_manager, self.sector_params)
        self.persistence_analyzer = SectorPersistenceAnalyzer(
            data_manager, 
            self.config.get('persistence', get_persistence_config())
        )
    
    def _convert_sector_params(self, params: Dict) -> Dict:
        """转换板块参数字段名（YAML格式 -> 代码格式）"""
        result = {}
        # 从YAML加载的是 concept/industry，代码中使用的是 概念/行业
        if 'concept' in params:
            result['概念'] = params['concept']
        if 'industry' in params:
            result['行业'] = params['industry']
        
        # 如果转换后为空，使用默认值
        if not result:
            return self._get_default_sector_params()
        
        return result
    
    def _load_yaml_config(self) -> Dict:
        """从YAML配置文件加载配置"""
        try:
            config = get_sector_tracker_config()
            if config:
                logger.info("[THSSectorTracker] 从YAML加载配置成功")
                return config
        except Exception as e:
            logger.warning(f"[THSSectorTracker] 加载YAML配置失败: {e}")
        
        # 回退到默认配置
        return self._get_default_config()
    
    def _load_default_config(self) -> Dict:
        """加载默认配置（从YAML或settings.py）"""
        # 优先从YAML加载
        yaml_config = get_sector_tracker_config()
        if yaml_config:
            logger.info("[THSSectorTracker] 从YAML加载默认配置")
            return yaml_config
        
        # 回退到settings.py
        try:
            from config.settings import THS_SECTOR_CONFIG
            return THS_SECTOR_CONFIG
        except ImportError:
            logger.warning("[THSSectorTracker] 无法加载配置文件，使用硬编码默认配置")
            return self._get_hardcoded_default_config()
    
    def _get_hardcoded_default_config(self) -> Dict:
        """获取硬编码默认配置（当所有配置文件都不可用时）"""
        return {
            "analyze_sectors": get_sector_analyze_config() or {"top_n": 20, "use_limit_cpt": True, "min_member_count": 10},
            "sector_params": self._get_default_sector_params(),
            "sector_relation": get_sector_relation_config() or {"min_overlap": 0.05, "default_overlap": 0.1},
            "resonance": get_resonance_config() or {"top_n": 20, "min_overlap": 0.1},
            "persistence": get_persistence_config() or {"lookback_days": 10, "hot_threshold_days": 3, "top_n": 10},
            "internal_structure": get_internal_structure_config() or {
                "hierarchy_weights": {
                    "has_leader": 20, "has_second_board": 20, "multiple_second_board": 10,
                    "has_third_plus": 20, "first_board_count_3": 20, "first_board_count_5": 10,
                },
                "leader_score": {"space_leader": 10, "strength_leader": 10, "time_leader": 10},
                "mid_cap_min_amount": 100000000,
            },
        }
    
    def _get_default_sector_params(self) -> Dict:
        """获取默认板块参数（优先从YAML加载）"""
        # 硬编码默认值
        default_params = {
            '概念': {
                'min_pct_change': 5.0,
                'price_weight': 0.5,
                'amount_weight': 0.2,
                'limit_weight': 0.3,
                'hot_threshold_pct': 0.15,
            },
            '行业': {
                'min_pct_change': 3.0,
                'price_weight': 0.35,
                'amount_weight': 0.35,
                'limit_weight': 0.3,
                'hot_threshold_pct': 0.2,
            }
        }
        
        yaml_params = get_sector_params()
        if yaml_params:
            # 转换YAML格式（concept/industry）到代码格式（概念/行业）
            params = {}
            if 'concept' in yaml_params:
                params['概念'] = yaml_params['concept']
            else:
                params['概念'] = default_params['概念']
                
            if 'industry' in yaml_params:
                params['行业'] = yaml_params['industry']
            else:
                params['行业'] = default_params['行业']
            
            return params
        
        return default_params

    def _load_sector_list(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """加载同花顺板块列表（概念+行业）"""
        if self._concept_list is None or self._industry_list is None:
            # 获取概念指数
            self._concept_list = self.dm.get_ths_index(index_type='N')
            # 获取行业指数
            self._industry_list = self.dm.get_ths_index(index_type='I')

            if self._concept_list.empty:
                logger.warning("[THSSectorTracker] 无法获取同花顺概念指数列表")
            else:
                logger.info(f"[THSSectorTracker] 加载 {len(self._concept_list)} 个概念板块")

            if self._industry_list.empty:
                logger.warning("[THSSectorTracker] 无法获取同花顺行业指数列表")
            else:
                logger.info(f"[THSSectorTracker] 加载 {len(self._industry_list)} 个行业板块")

        return self._concept_list, self._industry_list

    def get_sector_members(self, ts_code: str) -> pd.DataFrame:
        """
        获取板块成分股（带缓存）

        Args:
            ts_code: 板块代码（如 885001.TI）

        Returns:
            DataFrame: 成分股列表
        """
        if ts_code not in self._member_cache:
            members = self.dm.get_ths_member(ts_code=ts_code)
            self._member_cache[ts_code] = members
        return self._member_cache.get(ts_code, pd.DataFrame())

    def analyze_concept_sectors(self, trade_date: str, top_n: int = 20,
                                   use_limit_cpt: bool = True) -> pd.DataFrame:
        """
        分析概念板块（独立方法）
        
        概念板块特点：
        - 波动大、弹性高
        - 涨停家数多，情绪驱动明显
        - 持续性相对较短
        
        分析逻辑：
        1. 获取概念板块行情数据
        2. 重点考虑涨停家数和情绪热度
        3. 使用概念专属的评分权重

        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 返回前N个热点概念
            use_limit_cpt: 是否使用limit_cpt_list数据辅助判断

        Returns:
            DataFrame: 概念板块分析结果
        """
        logger.info("=" * 80)
        logger.info(f"【analyze_concept_sectors】开始分析概念板块，日期: {trade_date}")
        
        # 1. 获取概念板块列表
        concept_list, _ = self._load_sector_list()
        if concept_list.empty:
            logger.warning("[analyze_concept_sectors] 无法获取概念板块列表")
            return pd.DataFrame()
        
        concept_codes = set(concept_list['ts_code'].tolist())
        code_to_name = dict(zip(concept_list['ts_code'], concept_list['name']))
        
        # 2. 获取行情数据
        daily_df = self.dm.get_ths_daily(trade_date=trade_date)
        if daily_df.empty:
            logger.warning(f"[analyze_concept_sectors] 无法获取 {trade_date} 板块行情")
            return pd.DataFrame()
        
        # 3. 筛选概念板块
        concept_daily = daily_df[daily_df['ts_code'].isin(concept_codes)].copy()
        if concept_daily.empty:
            logger.warning(f"[analyze_concept_sectors] 无概念板块行情数据")
            return pd.DataFrame()
        
        logger.info(f"[analyze_concept_sectors] 获取到 {len(concept_daily)} 个概念板块行情")
        
        # 4. 获取limit_cpt_list数据
        limit_cpt_df = pd.DataFrame()
        if use_limit_cpt:
            try:
                limit_cpt_df = self.dm.get_limit_cpt_list(trade_date)
                if not limit_cpt_df.empty:
                    logger.info(f"[analyze_concept_sectors] 获取到limit_cpt_list数据: {len(limit_cpt_df)}条")
            except Exception as e:
                logger.warning(f"[analyze_concept_sectors] 获取limit_cpt_list失败: {e}")
        
        # 5. 使用HotSpotDetector识别热点概念
        result_df = self.hotspot_detector.detect_concept_hotspots(
            concept_daily, concept_codes, code_to_name, limit_cpt_df
        )
        
        if result_df.empty:
            return pd.DataFrame()
        
        # 只返回热点概念
        hot_concepts = result_df[result_df['is_hot'] == True].copy()
        
        logger.info(f"[analyze_concept_sectors] 分析完成，共{len(result_df)}个概念，其中热点概念{len(hot_concepts)}个")
        if not hot_concepts.empty:
            logger.info(f"[analyze_concept_sectors] 热点概念: {hot_concepts['name'].head(top_n).tolist()}")
        logger.info("=" * 80)
        
        return hot_concepts.head(top_n)
    
    def analyze_industry_sectors(self, trade_date: str, top_n: int = 20,
                                  use_limit_cpt: bool = True) -> pd.DataFrame:
        """
        分析行业板块（独立方法）
        
        行业板块特点：
        - 更稳定、资金容量大
        - 涨停家数相对较少但更有持续性
        - 受基本面驱动更多
        
        分析逻辑：
        1. 获取行业板块行情数据
        2. 重点考虑资金流向和持续性
        3. 使用行业专属的评分权重

        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 返回前N个热点行业
            use_limit_cpt: 是否使用limit_cpt_list数据辅助判断

        Returns:
            DataFrame: 行业板块分析结果
        """
        logger.info("=" * 80)
        logger.info(f"【analyze_industry_sectors】开始分析行业板块，日期: {trade_date}")
        
        # 1. 获取行业板块列表
        _, industry_list = self._load_sector_list()
        if industry_list.empty:
            logger.warning("[analyze_industry_sectors] 无法获取行业板块列表")
            return pd.DataFrame()
        
        industry_codes = set(industry_list['ts_code'].tolist())
        code_to_name = dict(zip(industry_list['ts_code'], industry_list['name']))
        
        # 2. 获取行情数据
        daily_df = self.dm.get_ths_daily(trade_date=trade_date)
        if daily_df.empty:
            logger.warning(f"[analyze_industry_sectors] 无法获取 {trade_date} 板块行情")
            return pd.DataFrame()
        
        # 3. 筛选行业板块
        industry_daily = daily_df[daily_df['ts_code'].isin(industry_codes)].copy()
        if industry_daily.empty:
            logger.warning(f"[analyze_industry_sectors] 无行业板块行情数据")
            return pd.DataFrame()
        
        logger.info(f"[analyze_industry_sectors] 获取到 {len(industry_daily)} 个行业板块行情")

        # 4. 使用HotSpotDetector识别热点行业
        result_df = self.hotspot_detector.detect_industry_hotspots(
            industry_daily, industry_codes, code_to_name, trade_date
        )
        
        if result_df.empty:
            return pd.DataFrame()

        # 只返回热点行业
        hot_industries = result_df[result_df['is_hot'] == True].copy()

        logger.info(f"[analyze_industry_sectors] 分析完成，共{len(result_df)}个行业，其中热点行业{len(hot_industries)}个")
        if not hot_industries.empty:
            logger.info(f"[analyze_industry_sectors] 热点行业: {hot_industries['name'].head(top_n).tolist()}")
        logger.info("=" * 80)

        return hot_industries.head(top_n)
    


    def calculate_sector_overlap(self, ts_code1: str, ts_code2: str) -> float:
        """
        计算两个板块的成分股重叠度

        Returns:
            float: 重叠度（0-1），越高表示关联越强
        """
        members1 = self.get_sector_members(ts_code1)
        members2 = self.get_sector_members(ts_code2)

        if members1.empty or members2.empty:
            return 0.0

        # 获取股票代码集合（ths_member使用con_code字段）
        def extract_codes(df):
            """从成分股DataFrame提取代码（统一格式化为6位数字）"""
            if 'con_code' in df.columns:
                codes = df['con_code'].astype(str).tolist()
            elif 'code' in df.columns:
                codes = df['code'].astype(str).tolist()
            else:
                return set()
            # 统一格式化为6位数字（移除后缀）
            return set([c.split('.')[0].zfill(6) for c in codes])
        
        codes1 = extract_codes(members1)
        codes2 = extract_codes(members2)

        if not codes1 or not codes2:
            return 0.0

        # 计算重叠度（Jaccard系数）
        intersection = len(codes1 & codes2)
        union = len(codes1 | codes2)

        return intersection / union if union > 0 else 0.0

    def find_related_sectors(self, ts_code: str, sector_type: str = None,
                            min_overlap: float = 0.3) -> List[Dict]:
        """
        查找与指定板块关联度高的其他板块

        Args:
            ts_code: 板块代码
            sector_type: 查找类型（'概念'/'行业'），None表示查找相反类型
            min_overlap: 最小重叠度阈值

        Returns:
            List[Dict]: 关联板块列表，包含 ts_code, name, overlap
        """
        concept_list, industry_list = self._load_sector_list()

        # 确定当前板块类型和要查找的类型
        current_type = None
        if concept_list is not None and ts_code in concept_list['ts_code'].values:
            current_type = '概念'
        elif industry_list is not None and ts_code in industry_list['ts_code'].values:
            current_type = '行业'

        if current_type is None:
            logger.warning(f"[find_related_sectors] 无法确定板块 {ts_code} 的类型")
            return []

        # 如果未指定查找类型，查找相反类型
        if sector_type is None:
            sector_type = '行业' if current_type == '概念' else '概念'

        # 获取目标列表
        target_list = industry_list if sector_type == '行业' else concept_list
        if target_list.empty:
            return []

        # 计算与所有目标板块的重叠度
        related = []
        for _, row in target_list.iterrows():
            target_code = row['ts_code']
            if target_code == ts_code:
                continue

            overlap = self.calculate_sector_overlap(ts_code, target_code)
            if overlap >= min_overlap:
                related.append({
                    'ts_code': target_code,
                    'name': row.get('name', ''),
                    'type': sector_type,
                    'overlap': overlap,
                    'overlap_pct': f"{overlap*100:.1f}%"
                })

        # 按重叠度排序
        related.sort(key=lambda x: x['overlap'], reverse=True)
        return related

    def get_sector_stocks(self, ts_code: str, trade_date: str,
                         limit_up_df: pd.DataFrame = None) -> Dict:
        """
        获取板块内的股票详情

        Args:
            ts_code: 板块代码
            trade_date: 交易日期
            limit_up_df: 涨停池数据（用于标记涨停股）

        Returns:
            Dict: 板块内股票信息
        """
        # 1. 获取成分股
        members = self.get_sector_members(ts_code)
        if members.empty:
            return {}

        # 2. 获取板块行情
        sector_daily = self.dm.get_ths_daily(ts_code=ts_code, trade_date=trade_date)

        # 3. 统计涨停股
        up_stocks = []
        if limit_up_df is not None and not limit_up_df.empty:
            # 从涨停池筛选属于该板块的股票
            # 使用统一的字段映射工具提取代码（成分股使用 con_code 字段）
            member_codes = set(DataFrameFieldMapper.extract_codes(
                members, 
                remove_suffix=True, 
                fields=['con_code', 'code']  # 优先使用 con_code
            ))
            
            # 获取涨停池代码列
            zt_code_col = DataFrameFieldMapper.get_code_column(limit_up_df)
            if zt_code_col:
                for _, stock in limit_up_df.iterrows():
                    # 统一代码格式为6位数字
                    code = StockCodeUtils.remove_suffix(str(stock.get(zt_code_col, ''))).zfill(6)
                    if code in member_codes:
                        # 获取名称列
                        name_col = DataFrameFieldMapper.get_name_column(limit_up_df)
                        up_stocks.append({
                            'code': code,
                            'name': stock.get(name_col, '') if name_col else '',
                            'limit_up_time': stock.get('首次封板时间', ''),
                            'board_height': stock.get('连板数', 1)
                        })

        return {
            'ts_code': ts_code,
            'member_count': len(members),
            'up_count': len(up_stocks),
            'up_stocks': up_stocks,
            'sector_daily': sector_daily.to_dict() if not sector_daily.empty else {}
        }

    def analyze_sector_internal_structure(self, ts_code: str, trade_date: str,
                                          limit_up_df: pd.DataFrame = None,
                                          hierarchy_df: pd.DataFrame = None) -> Dict:
        """
        分析板块内部结构 - 涨停梯队、龙头股、中军股等
        
        分析维度：
        1. 涨停梯队完整性：从首板到高板的完整度
        2. 龙头股识别：最高板、封单最大、最先涨停
        3. 中军股识别：大市值、高成交额的核心标的
        4. 跟风股分布：涨停时间分布、封单强度分布
        
        Args:
            ts_code: 板块代码
            trade_date: 交易日期
            limit_up_df: 涨停池数据
            hierarchy_df: 连板梯队数据
            
        Returns:
            Dict: 板块内部结构分析结果
        """
        logger.info(f"[analyze_sector_internal_structure] 分析板块 {ts_code} 内部结构...")
        
        # 1. 获取板块成分股
        members = self.get_sector_members(ts_code)
        if members.empty:
            return {}
        
        # 提取成分股代码集合
        member_codes = set(DataFrameFieldMapper.extract_codes(
            members, 
            remove_suffix=True, 
            fields=['con_code', 'code']
        ))
        
        result = {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'member_count': len(member_codes),
            'limit_up_structure': {},
            'leading_stocks': [],
            'mid_cap_stocks': [],
            'structure_score': 0,
            'structure_assessment': ''
        }
        
        # 2. 涨停梯队分析
        if limit_up_df is not None and not limit_up_df.empty:
            # 筛选属于该板块的涨停股
            zt_code_col = DataFrameFieldMapper.get_code_column(limit_up_df)
            zt_name_col = DataFrameFieldMapper.get_name_column(limit_up_df)
            
            sector_zt_stocks = []
            for _, stock in limit_up_df.iterrows():
                code = StockCodeUtils.remove_suffix(str(stock.get(zt_code_col, ''))).zfill(6)
                if code in member_codes:
                    stock_info = {
                        'code': code,
                        'name': stock.get(zt_name_col, '') if zt_name_col else '',
                        'board_height': stock.get('连板数', 1),
                        'limit_up_time': stock.get('首次封板时间', ''),
                        'limit_up_amount': stock.get('封单金额', 0),
                        'limit_up_ratio': stock.get('封单金额/流通市值', 0),
                        'pct_change': stock.get('涨跌幅', 0)
                    }
                    sector_zt_stocks.append(stock_info)
            
            # 分析涨停梯队
            if sector_zt_stocks:
                result['limit_up_structure'] = self._analyze_limit_up_hierarchy(sector_zt_stocks)
                result['leading_stocks'] = self._identify_leading_stocks(sector_zt_stocks)
        
        # 3. 中军股识别（需要额外获取市值和成交额数据）
        # 简化版本：从涨停股中识别大市值标的
        if result['leading_stocks']:
            result['mid_cap_stocks'] = self._identify_mid_cap_stocks(result['leading_stocks'])
        
        # 4. 计算结构得分和评估
        result['structure_score'], result['structure_assessment'] = self._calculate_structure_score(result)
        
        return result
    
    def _analyze_limit_up_hierarchy(self, zt_stocks: List[Dict]) -> Dict:
        """
        分析涨停梯队结构
        
        使用公共组件库计算
        
        Returns:
            Dict: 梯队分析结果
        """
        from core.analysis.market_indicators import analyze_limit_up_hierarchy
        
        # 获取配置中的权重
        weights = self.structure_config.get('hierarchy_weights', {
            "has_leader": 20, "has_second_board": 20, "multiple_second_board": 10,
            "has_third_plus": 20, "first_board_count_3": 20, "first_board_count_5": 10,
        })
        
        # 使用公共组件计算
        result = analyze_limit_up_hierarchy(zt_stocks, hierarchy_weights=weights)
        
        # 转换为原有格式保持兼容
        return {
            'max_height': result.max_height,
            'total_zt_count': result.total_count,
            'hierarchy_stats': result.stats_by_height,
            'completeness_score': min(100, result.completeness_score),
            'structure_type': result.structure_type
        }
    
    def _classify_hierarchy_structure(self, max_height: int, height_groups: Dict) -> str:
        """
        分类梯队结构类型
        
        使用公共组件库计算，保持向后兼容
        """
        from core.analysis.market_indicators import classify_hierarchy_structure
        return classify_hierarchy_structure(max_height, height_groups)
    
    def _identify_leading_stocks(self, zt_stocks: List[Dict]) -> List[Dict]:
        """
        识别龙头股
        
        龙头标准：
        1. 空间龙头：连板数最高
        2. 强度龙头：封单金额最大
        3. 时间龙头：最先涨停
        
        使用公共组件库计算
        """
        from core.analysis.market_indicators import identify_leaders
        
        if not zt_stocks:
            return []
        
        # 获取配置中的权重
        leader_weights = self.structure_config.get('leader_score', {
            'space_leader': 10, 'strength_leader': 10, 'time_leader': 10
        })
        
        # 使用公共组件计算
        result = identify_leaders(zt_stocks, leader_weights=leader_weights)
        
        # 转换为原有格式保持兼容
        leaders = []
        for leader in result.leaders:
            leader_dict = {
                'type': leader.type,
                'code': leader.code,
                'name': leader.name,
                'board_height': leader.board_height,
                'reason': leader.reason
            }
            if leader.limit_up_amount > 0:
                leader_dict['limit_up_amount'] = leader.limit_up_amount
            if leader.first_limit_time:
                leader_dict['limit_up_time'] = leader.first_limit_time
            leaders.append(leader_dict)
        
        return leaders
    
    def _identify_mid_cap_stocks(self, leading_stocks: List[Dict]) -> List[Dict]:
        """
        识别中军股
        
        中军特征：
        1. 大市值（从封单金额推断）
        2. 行业地位稳固
        3. 通常不是连板最高的，但封单稳定
        """
        mid_caps = []
        
        # 获取配置中的封单金额阈值
        min_amount = self.structure_config.get('mid_cap_min_amount', 100000000)  # 默认1亿
        
        # 从龙头股中筛选潜在的"中军"
        # 简化逻辑：封单金额大但连板数不是最高的
        for stock in leading_stocks:
            if stock.get('limit_up_amount', 0) > min_amount:
                mid_caps.append({
                    'code': stock['code'],
                    'name': stock['name'],
                    'board_height': stock.get('board_height', 1),
                    'limit_up_amount': stock.get('limit_up_amount', 0),
                    'type': '潜在中军'
                })
        
        return mid_caps[:3]  # 最多返回3个
    
    def _calculate_structure_score(self, structure_data: Dict) -> Tuple[int, str]:
        """
        计算板块内部结构得分
        
        Returns:
            Tuple[int, str]: (得分, 评估描述)
        """
        score = 0
        
        # 获取配置中的龙头股评分权重
        leader_weights = self.structure_config.get('leader_score', {
            'space_leader': 10, 'strength_leader': 10, 'time_leader': 10
        })
        
        # 1. 梯队完整性得分
        hierarchy = structure_data.get('limit_up_structure', {})
        completeness = hierarchy.get('completeness_score', 0)
        score += completeness * 0.4  # 占比40%
        
        # 2. 龙头股得分（使用配置权重）
        leaders = structure_data.get('leading_stocks', [])
        leader_score_per_type = leader_weights.get('space_leader', 10)
        if len(leaders) >= 3:
            score += leader_score_per_type * 3  # 有空间、强度、时间龙头
        elif len(leaders) >= 2:
            score += leader_score_per_type * 2
        elif len(leaders) >= 1:
            score += leader_score_per_type
        
        # 3. 中军股得分
        mid_caps = structure_data.get('mid_cap_stocks', [])
        if mid_caps:
            score += min(20, len(mid_caps) * 10)
        
        # 4. 涨停数量得分
        total_zt = hierarchy.get('total_zt_count', 0)
        if total_zt >= 10:
            score += 10
        elif total_zt >= 5:
            score += 5
        
        final_score = int(min(100, score))
        
        # 生成评估描述
        structure_type = hierarchy.get('structure_type', '未知')
        if final_score >= 80:
            assessment = f'结构优秀，{structure_type}，适合积极参与'
        elif final_score >= 60:
            assessment = f'结构良好，{structure_type}，可适度参与'
        elif final_score >= 40:
            assessment = f'结构一般，{structure_type}，谨慎参与'
        else:
            assessment = f'结构较弱，{structure_type}，建议观望'
        
        return final_score, assessment

    # ==================== 兼容旧接口的方法 ====================

    def analyze_sectors_persistence(self, trade_date: str, top_n: int = None,
                                     lookback_days: int = None, 
                                     hot_threshold_days: int = None) -> pd.DataFrame:
        """
        分析板块持续性热度
        
        "持续性"定义：板块在最近 lookback_days 个交易日内，至少有 hot_threshold_days 天
        进入涨幅排名前 top_n，且平均涨幅保持较高水平。
        
        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 每日热点板块排名阈值（进入前N名才算当日热门）
            lookback_days: 回溯交易日数量（默认5天）
            hot_threshold_days: 判定为持续热门的最少天数（默认3天）
            
        Returns:
            DataFrame: 包含持续性分析的板块数据
            
        输出字段说明：
            - 板块名称: 板块名称
            - 板块类型: 概念/行业
            - 持续天数: 在lookback_days天内进入top_n的天数
            - 平均排名: 在lookback_days天内的平均排名
            - 排名趋势: 排名变化趋势（上升/下降/平稳）
            - 平均涨幅: 在lookback_days天内的平均涨跌幅
            - 最新涨幅: 当日涨跌幅
            - 最新排名: 当日排名
            - 持续性评分: 综合持续性强度（0-100）
            - 是否持续热门: 是否满足 hot_threshold_days 条件
            - 所处阶段: 根据涨幅和持续性判断（启动期/加速期/高潮期/衰退期）
        """
        # 使用配置参数（如果未提供）
        persistence_config = self.config.get('persistence', {})
        top_n = top_n or persistence_config.get('top_n', 10)
        lookback_days = lookback_days or persistence_config.get('lookback_days', 5)
        hot_threshold_days = hot_threshold_days or persistence_config.get('hot_threshold_days', 3)
        
        # 1. 计算回溯日期列表（使用DateUtils，正确处理节假日）
        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)
        
        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_sectors_persistence] 只能获取 {len(date_list)} 个交易日，少于要求的 {lookback_days} 天")
        
        # 2. 收集多日的板块分析数据
        daily_results = {}
        all_sectors = set()
        
        for date in date_list:
            try:
                # 分别获取概念和行业数据并合并
                concept_df = self.analyze_concept_sectors(date, top_n=top_n * 3)
                industry_df = self.analyze_industry_sectors(date, top_n=top_n * 3)
                
                # 合并概念和行业数据
                if not concept_df.empty and not industry_df.empty:
                    daily_df = pd.concat([concept_df, industry_df], ignore_index=True)
                elif not concept_df.empty:
                    daily_df = concept_df
                elif not industry_df.empty:
                    daily_df = industry_df
                else:
                    continue
                    
                if not daily_df.empty:
                    daily_results[date] = daily_df
                    all_sectors.update(daily_df['ts_code'].tolist())
            except Exception as e:
                logger.warning(f"[analyze_sectors_persistence] 获取 {date} 数据失败: {e}")
        
        if not daily_results:
            logger.warning(f"[analyze_sectors_persistence] 无法获取任何历史数据")
            return pd.DataFrame()
        
        # 3. 分析每个板块的持续性
        results = []
        limit_up_df = self.dm.get_limit_up_pool(trade_date)
        
        for ts_code in all_sectors:
            # 收集该板块在多日的数据
            sector_history = []
            
            for date in date_list:
                if date in daily_results:
                    daily_df = daily_results[date]
                    sector_row = daily_df[daily_df['ts_code'] == ts_code]
                    if not sector_row.empty:
                        sector_history.append({
                            'date': date,
                            'rank': sector_row.iloc[0]['rank'],
                            'pct_change': sector_row.iloc[0]['pct_change'],
                            'composite_score': sector_row.iloc[0]['composite_score'],
                            'is_hot': sector_row.iloc[0]['is_hot'],
                            'name': sector_row.iloc[0]['name'],
                            'type': sector_row.iloc[0]['type']
                        })
            
            if not sector_history:
                continue
            
            # 获取最新数据
            latest = sector_history[0]  # 第一个是最新的（date_list按时间倒序）
            
            # 计算持续性指标
            # 3.1 持续天数：进入top_n的天数
            hot_days = sum(1 for h in sector_history if h['rank'] <= top_n)
            
            # 3.2 平均排名
            avg_rank = sum(h['rank'] for h in sector_history) / len(sector_history)
            
            # 3.3 排名趋势（最近3天 vs 前3天）
            if len(sector_history) >= 4:
                recent_avg = sum(h['rank'] for h in sector_history[:3]) / 3
                earlier_avg = sum(h['rank'] for h in sector_history[3:]) / (len(sector_history) - 3)
                rank_trend = earlier_avg - recent_avg  # 正值表示排名上升（数字变小）
                
                if rank_trend > 5:
                    trend_desc = '快速上升'
                elif rank_trend > 2:
                    trend_desc = '稳步上升'
                elif rank_trend < -5:
                    trend_desc = '快速下降'
                elif rank_trend < -2:
                    trend_desc = '逐步下降'
                else:
                    trend_desc = '相对平稳'
            else:
                trend_desc = '数据不足'
            
            # 3.4 平均涨幅
            avg_pct_change = sum(h['pct_change'] for h in sector_history) / len(sector_history)
            
            # 3.5 涨幅趋势（最近3天 vs 前3天）
            if len(sector_history) >= 4:
                recent_pct = sum(h['pct_change'] for h in sector_history[:3]) / 3
                earlier_pct = sum(h['pct_change'] for h in sector_history[3:]) / (len(sector_history) - 3)
                pct_trend = recent_pct - earlier_pct
            else:
                pct_trend = 0
            
            # 3.6 判断是否持续热门
            is_persistent_hot = hot_days >= hot_threshold_days
            
            # 3.7 计算持续性评分（0-100）
            # 因素：涨停强度(35%) + 持续天数占比(25%) + 平均排名(20%) + 平均涨幅(20%)
            # 涨停强度是判断热点最核心的指标
            
            # 获取当日涨停家数和连板家数（用于计算涨停强度）
            sector_detail = self.get_sector_stocks(ts_code, trade_date, limit_up_df)
            up_count = sector_detail.get('up_count', 0) if sector_detail else 0
            up_stocks = sector_detail.get('up_stocks', []) if sector_detail else []
            cons_count = sum(1 for s in up_stocks if s.get('board_height', 1) >= 2)
            
            # 涨停强度得分：基于涨停家数和连板家数
            # 涨停家数越多得分越高，有连板加分
            limit_up_score = min(100, up_count * 5 + cons_count * 10)  # 每只涨停5分，每只连板额外10分
            
            persistence_ratio = hot_days / len(sector_history) * 100  # 持续天数占比
            rank_score = max(0, (100 - avg_rank))  # 排名得分（排名越靠前得分越高）
            pct_score = min(100, max(0, avg_pct_change * 10))  # 涨幅得分（1% = 10分）
            
            persistence_score = (
                limit_up_score * 0.35 +      # 涨停强度权重最高
                persistence_ratio * 0.25 +   # 持续性次之
                rank_score * 0.20 +          # 排名再次
                pct_score * 0.20             # 涨幅最后
            )
            
            # 3.8 判断所处阶段
            if is_persistent_hot:
                if latest['pct_change'] > 5 and pct_trend > 1:
                    stage = '高潮期'
                elif latest['pct_change'] > 3 and pct_trend > 0:
                    stage = '加速期'
                elif latest['pct_change'] > 1:
                    stage = '主升浪'
                else:
                    stage = '震荡整理'
            else:
                if hot_days >= 2:
                    stage = '启动期'
                else:
                    stage = '观察期'
            
            # 3.9 获取操作建议（涨停家数已在前面计算）
            operation_advice = self._get_operation_advice(is_persistent_hot, stage, pct_trend)
            
            # 映射操作建议到仓位建议
            position_mapping = {
                '积极关注': 'medium',
                '重点关注': 'medium',
                '逢低布局': 'medium',
                '持有观察': 'light',
                '谨慎追高': 'light',
                '观望': 'light',
                '观察': 'light'
            }
            
            # 映射操作建议到紧急度
            urgency_mapping = {
                '积极关注': '高',
                '重点关注': '高',
                '逢低布局': '中',
                '持有观察': '中',
                '谨慎追高': '低',
                '观望': '低',
                '观察': '低'
            }
            
            results.append({
                '板块名称': latest['name'],
                '板块类型': latest['type'],
                'ts_code': ts_code,
                '持续天数': hot_days,
                '统计天数': len(sector_history),
                '平均排名': round(avg_rank, 1),
                '最新排名': int(latest['rank']),
                '排名趋势': trend_desc,
                '平均涨幅': round(avg_pct_change, 2),
                '最新涨幅': round(latest['pct_change'], 2),
                '涨幅趋势': round(pct_trend, 2),
                '持续性评分': round(persistence_score, 1),
                '是否持续热门': is_persistent_hot,
                '所处阶段': stage,
                '涨停家数': up_count,
                '连板家数': cons_count,
                '操作建议': operation_advice,
                '建议仓位': position_mapping.get(operation_advice, 'light'),
                '紧急度': urgency_mapping.get(operation_advice, '低'),
                '策略理由': self._get_strategy_reason(latest, hot_days, trend_desc, stage),
                # 兼容旧接口字段
                '当前排名': int(latest['rank']),
                '综合评分': round(persistence_score, 1),
                '市场周期': '上升期' if is_persistent_hot else '震荡期',
                '成交额变化': 0.0,
                '换手率': 0.0,
                '排名动量': round(pct_trend, 2),
                '涨停趋势': float(cons_count)
            })
        
        if not results:
            return pd.DataFrame()
        
        # 4. 过滤涨停家数为0的板块（没有涨停的板块不可能是热点）
        result_df = pd.DataFrame(results)
        result_df = result_df[result_df['涨停家数'] > 0]
        
        if result_df.empty:
            logger.warning(f"[analyze_sectors_persistence] 过滤后没有板块满足条件（涨停家数>0）")
            return pd.DataFrame()
        
        # 5. 按持续性评分排序
        result_df = result_df.sort_values('持续性评分', ascending=False)
        
        return result_df.head(top_n)
    
    def _get_operation_advice(self, is_persistent_hot: bool, stage: str, pct_trend: float) -> str:
        """根据持续性状态生成操作建议"""
        if not is_persistent_hot:
            return '观望'
        
        if stage == '高潮期':
            return '谨慎追高' if pct_trend < 0 else '持有观察'
        elif stage == '加速期':
            return '积极关注'
        elif stage == '主升浪':
            return '重点关注'
        elif stage == '启动期':
            return '逢低布局'
        else:
            return '观察'
    
    def _get_strategy_reason(self, latest: dict, hot_days: int, trend_desc: str, stage: str) -> str:
        """生成策略理由"""
        reasons = []
        
        if hot_days >= 3:
            reasons.append(f"连续{hot_days}天热门")
        
        reasons.append(f"排名{trend_desc}")
        reasons.append(f"当前处于{stage}")
        
        if latest['is_hot']:
            reasons.append("当日热点")
        
        return "；".join(reasons)

    def analyze_concept_persistence(self, trade_date: str, top_n: int = 10,
                                     lookback_days: int = 10,
                                     hot_concepts_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        分析概念板块持续性（独立方法）- M天内N次模式
        
        基于当前热点概念，分析其历史持续性。采用"M天内N次"模式：
        - 不是要求连续N天都是热点
        - 而是在M天内有N天被识别为热点
        
        Args:
            trade_date: 交易日期（YYYYMMDD）
            top_n: 返回前N个持续热门概念
            lookback_days: 回溯交易日数量M（默认10天）
            hot_concepts_df: 当前热点概念分析结果（可选，如不提供则重新计算）
            
        Returns:
            DataFrame: 概念持续性分析结果
        """
        logger.info("=" * 80)
        logger.info(f"【analyze_concept_persistence】开始分析概念板块持续性，日期: {trade_date}")
        logger.info(f"[analyze_concept_persistence] 采用M天内N次模式，M={lookback_days}")
        
        # 使用配置参数
        persistence_config = self.config.get('persistence', {})
        hot_threshold_days = persistence_config.get('hot_threshold_days', 3)  # N值：最少热点天数
        
        # 1. 获取当前热点概念列表（如果未提供则重新计算）
        if hot_concepts_df is None or hot_concepts_df.empty:
            hot_concepts_df = self.analyze_concept_sectors(trade_date, top_n=top_n * 2)
        
        if hot_concepts_df.empty or 'is_hot' not in hot_concepts_df.columns:
            logger.warning("[analyze_concept_persistence] 无法获取当前热点概念")
            return pd.DataFrame()
        
        # 只关注当前被标记为热点的概念
        current_hot_concepts = hot_concepts_df[hot_concepts_df['is_hot'] == True]
        if current_hot_concepts.empty:
            logger.warning("[analyze_concept_persistence] 当前无热点概念")
            return pd.DataFrame()
        
        logger.info(f"[analyze_concept_persistence] 当前热点概念: {len(current_hot_concepts)}个")
        logger.info(f"[analyze_concept_persistence] 当前热点概念列表: {current_hot_concepts['name'].tolist()}")
        logger.info(f"[analyze_concept_persistence] 当前热点概念ts_code: {current_hot_concepts['ts_code'].tolist()}")
        
        # 2. 计算回溯日期列表
        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)
        
        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_concept_persistence] 只能获取 {len(date_list)} 个交易日")
        
        # 3. 收集多日的概念板块分析数据（只分析当前热点概念的历史）
        daily_results = {}
        # 使用名称来匹配，而不是ts_code（因为ts_code可能在不同日期不一致）
        target_concept_names = set(current_hot_concepts['name'].tolist())
        
        for date in date_list:
            try:
                # 获取所有概念（而不仅是前N个），以确保能匹配到当前热点概念的历史数据
                daily_df = self.analyze_concept_sectors(date, top_n=100)
                if not daily_df.empty:
                    # 只保留当前热点概念的历史数据（使用名称匹配）
                    matched_df = daily_df[daily_df['name'].isin(target_concept_names)]
                    if not matched_df.empty:
                        daily_results[date] = matched_df
            except Exception as e:
                logger.warning(f"[analyze_concept_persistence] 获取 {date} 数据失败: {e}")
        
        if not daily_results:
            logger.warning("[analyze_concept_persistence] 无法获取任何历史数据")
            return pd.DataFrame()
        
        # 4. 使用SectorPersistenceAnalyzer分析持续性
        result_df = self.persistence_analyzer.analyze_persistence_with_history(
            trade_date=trade_date,
            hot_sectors_df=hot_concepts_df,
            historical_data=daily_results,
            lookback_days=lookback_days,
            top_n=top_n
        )
        
        if result_df.empty:
            logger.warning("[analyze_concept_persistence] 无持续性分析结果")
            return pd.DataFrame()
        
        logger.info(f"[analyze_concept_persistence] 分析完成，返回 {len(result_df)} 个持续热门概念")
        for _, row in result_df.head(5).iterrows():
            logger.info(f"  - {row['板块名称']}: {lookback_days}天内{row['热点天数']}次热点, 评分{row['持续性评分']:.1f}, 阶段[{row['所处阶段']}]")
        logger.info("=" * 80)
        
        return result_df

    def analyze_industry_persistence(self, trade_date: str, top_n: int = 10,
                                      lookback_days: int = 10,
                                      hot_industries_df: pd.DataFrame = None) -> pd.DataFrame:
        """
        分析行业板块持续性（独立方法）- M天内N次模式
        
        基于当前热点行业，分析其历史持续性。采用"M天内N次"模式：
        - 不是要求连续N天都是热点
        - 而是在M天内有N天被识别为热点
        
        Args:
            trade_date: 交易日期（YYYYMMDD）
            hot_industries_df: 当前热点行业DataFrame（包含'is_hot'列）
            
        Returns:
            DataFrame: 行业持续性分析结果
        """
        logger.info("=" * 80)
        logger.info(f"【analyze_industry_persistence】开始分析行业板块持续性，日期: {trade_date}")
        logger.info(f"[analyze_industry_persistence] 采用M天内N次模式，M={lookback_days}")
        
        # 使用配置参数
        persistence_config = self.config.get('persistence', {})
        hot_threshold_days = persistence_config.get('hot_threshold_days', 3)  # N值
        
        # 1. 获取当前热点行业列表（如果未提供则重新计算）
        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=top_n * 2)
        
        if hot_industries_df.empty or 'is_hot' not in hot_industries_df.columns:
            logger.warning("[analyze_industry_persistence] 无法获取当前热点行业")
            return pd.DataFrame()
        
        # 只关注当前被标记为热点的行业
        current_hot_industries = hot_industries_df[hot_industries_df['is_hot'] == True]
        if current_hot_industries.empty:
            logger.warning("[analyze_industry_persistence] 当前无热点行业")
            return pd.DataFrame()
        
        logger.info(f"[analyze_industry_persistence] 当前热点行业: {len(current_hot_industries)}个")
        logger.info(f"[analyze_industry_persistence] 当前热点行业列表: {current_hot_industries['name'].tolist()}")
        logger.info(f"[analyze_industry_persistence] 当前热点行业ts_code: {current_hot_industries['ts_code'].tolist()}")
        
        # 2. 计算回溯日期列表
        from core.utils import DateUtils
        date_utils = DateUtils
        logger.info(f"[analyze_industry_persistence] 采用M天内N次模式，M={lookback_days}")
        
        # 使用配置参数
        persistence_config = self.config.get('persistence', {})
        hot_threshold_days = persistence_config.get('hot_threshold_days', 3)  # N值
        
        # 1. 获取当前热点行业列表（如果未提供则重新计算）
        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=top_n * 2)
        
        if hot_industries_df.empty or 'is_hot' not in hot_industries_df.columns:
            logger.warning("[analyze_industry_persistence] 无法获取当前热点行业")
            return pd.DataFrame()
        
        # 只关注当前被标记为热点的行业
        current_hot_industries = hot_industries_df[hot_industries_df['is_hot'] == True]
        if current_hot_industries.empty:
            logger.warning("[analyze_industry_persistence] 当前无热点行业")
            return pd.DataFrame()
        
        logger.info(f"[analyze_industry_persistence] 当前热点行业: {len(current_hot_industries)}个")
        
        # 2. 计算回溯日期列表
        from core.utils import DateUtils
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(lookback_days, trade_date)
        
        if len(date_list) < lookback_days:
            logger.warning(f"[analyze_industry_persistence] 只能获取 {len(date_list)} 个交易日")
        
        # 3. 收集多日的行业板块分析数据（只分析当前热点行业的历史）
        daily_results = {}
        # 使用名称来匹配，而不是ts_code（因为ts_code可能在不同日期不一致）
        target_industry_names = set(current_hot_industries['name'].tolist())
        
        for date in date_list:
            try:
                # 获取所有行业（而不仅是前N个），以确保能匹配到当前热点行业的历史数据
                daily_df = self.analyze_industry_sectors(date, top_n=100)
                if not daily_df.empty:
                    # 只保留当前热点行业的历史数据（使用名称匹配）
                    matched_df = daily_df[daily_df['name'].isin(target_industry_names)]
                    if not matched_df.empty:
                        daily_results[date] = matched_df
            except Exception as e:
                logger.warning(f"[analyze_industry_persistence] 获取 {date} 数据失败: {e}")
        
        if not daily_results:
            logger.warning("[analyze_industry_persistence] 无法获取任何历史数据")
            return pd.DataFrame()
        
        # 4. 使用SectorPersistenceAnalyzer分析持续性
        result_df = self.persistence_analyzer.analyze_persistence_with_history(
            trade_date=trade_date,
            hot_sectors_df=hot_industries_df,
            historical_data=daily_results,
            lookback_days=lookback_days,
            top_n=top_n
        )
        
        if result_df.empty:
            logger.warning("[analyze_industry_persistence] 无持续性分析结果")
            return pd.DataFrame()
        
        logger.info(f"[analyze_industry_persistence] 分析完成，返回 {len(result_df)} 个持续热门行业")
        for _, row in result_df.head(5).iterrows():
            logger.info(f"  - {row['板块名称']}: {lookback_days}天内{row['热点天数']}次热点, 评分{row['持续性评分']:.1f}, 阶段[{row['所处阶段']}]")
        logger.info("=" * 80)
        
        return result_df

    def _analyze_persistence_for_sectors(self, all_sectors: set, daily_results: dict,
                                          date_list: list, hot_threshold_days: int,
                                          lookback_days: int, sector_type: str) -> list:
        """
        分析板块持续性的通用方法
        
        Args:
            all_sectors: 所有板块代码集合
            daily_results: 每日分析结果字典
            date_list: 日期列表
            hot_threshold_days: 判定为持续热门的最少天数
            lookback_days: 回溯交易日数量
            sector_type: 板块类型（概念/行业）
            
        Returns:
            list: 持续性分析结果列表
        """
        results = []
        
        for ts_code in all_sectors:
            sector_history = []
            
            for date in date_list:
                if date in daily_results:
                    daily_df = daily_results[date]
                    sector_row = daily_df[daily_df['ts_code'] == ts_code]
                    if not sector_row.empty:
                        sector_history.append({
                            'date': date,
                            'rank': sector_row.iloc[0]['rank'],
                            'pct_change': sector_row.iloc[0]['pct_change'],
                            'composite_score': sector_row.iloc[0]['composite_score'],
                            'is_hot': sector_row.iloc[0].get('is_hot', False),
                            'name': sector_row.iloc[0]['name'],
                            'type': sector_row.iloc[0]['type']
                        })
            
            if not sector_history:
                continue
            
            latest = sector_history[0]
            
            # 计算持续天数（is_hot为True的天数）
            hot_days = sum(1 for h in sector_history if h['is_hot'])
            is_persistent_hot = hot_days >= hot_threshold_days
            
            # 计算平均排名和涨幅
            avg_rank = np.mean([h['rank'] for h in sector_history])
            avg_pct_change = np.mean([h['pct_change'] for h in sector_history])
            
            # 计算排名趋势
            if len(sector_history) >= 2:
                early_rank = np.mean([h['rank'] for h in sector_history[-2:]])
                recent_rank = np.mean([h['rank'] for h in sector_history[:2]])
                rank_trend = early_rank - recent_rank
            else:
                rank_trend = 0
            
            # 判断趋势方向
            if rank_trend > 5:
                trend_desc = '上升'
            elif rank_trend < -5:
                trend_desc = '下降'
            else:
                trend_desc = '平稳'
            
            # 计算持续性评分
            persistence_score = (
                hot_days / len(date_list) * 50 +
                (100 - avg_rank) / 100 * 30 +
                max(0, rank_trend) / len(date_list) * 20
            )
            
            # 判断所处阶段
            if hot_days >= lookback_days * 0.8:
                stage = '高潮期'
            elif hot_days >= lookback_days * 0.6:
                stage = '加速期'
            elif hot_days >= lookback_days * 0.4:
                stage = '主升浪'
            elif hot_days >= lookback_days * 0.2:
                stage = '启动期'
            else:
                stage = '观察期'
            
            # 获取涨停数据 - 从板块分析结果中获取涨停家数
            # 使用最新的板块分析数据中的涨停家数
            up_count = 0
            cons_count = 0
            
            # 从最新的daily_results中获取涨停数据
            if date_list[0] in daily_results:
                latest_df = daily_results[date_list[0]]
                # 根据匹配方式选择列
                if match_by == 'name':
                    sector_data = latest_df[latest_df['name'] == sector_key]
                else:
                    sector_data = latest_df[latest_df['ts_code'] == sector_key]
                if not sector_data.empty:
                    up_count = sector_data.iloc[0].get('limit_up_count', 0)
                    cons_count = sector_data.iloc[0].get('consecutive_count', 0)
            
            # 生成操作建议
            operation_advice = self._get_operation_advice(is_persistent_hot, stage, rank_trend)
            
            results.append({
                '板块名称': latest['name'],
                '板块类型': latest['type'],
                'ts_code': latest.get('ts_code', ''),
                '持续天数': hot_days,
                '统计天数': len(sector_history),
                '平均排名': round(avg_rank, 1),
                '最新排名': int(latest['rank']),
                '排名趋势': trend_desc,
                '平均涨幅': round(avg_pct_change, 2),
                '最新涨幅': round(latest['pct_change'], 2),
                '持续性评分': round(persistence_score, 1),
                '是否持续热门': is_persistent_hot,
                '所处阶段': stage,
                '涨停家数': up_count,
                '连板家数': cons_count,
                '操作建议': operation_advice,
                '策略理由': self._get_strategy_reason(latest, hot_days, trend_desc, stage)
            })
        
        return results

    def analyze_concept_industry_resonance(self, trade_date: str,
                                            hot_concepts_df: pd.DataFrame = None,
                                            hot_industries_df: pd.DataFrame = None,
                                            lookback_days: int = 10) -> pd.DataFrame:
        """
        分析概念-行业共振（市场主线）- 多维度共振模型
        
        改进点：
        1. 降低成分股重叠度阈值（15%），概念和行业成分股数量差异大
        2. 增加涨停股重叠度维度（权重30%）
        3. 增加热度趋势同步性维度（权重25%）
        4. 增加领涨股重叠度维度（权重20%）
        5. 成分股重叠度权重降至25%
        
        Args:
            trade_date: 交易日期（YYYYMMDD）
            hot_concepts_df: 热点概念分析结果（可选，如不提供则重新计算）
            hot_industries_df: 热点行业分析结果（可选，如不提供则重新计算）
            lookback_days: 回溯交易日数量（默认10天）
            
        Returns:
            DataFrame: 共振分析结果（市场主线）
        """
        logger.info("=" * 80)
        logger.info(f"【analyze_concept_industry_resonance】开始分析概念-行业共振，日期: {trade_date}")
        
        # 1. 获取热点概念和热点行业（如果未提供）
        if hot_concepts_df is None or hot_concepts_df.empty:
            hot_concepts_df = self.analyze_concept_sectors(trade_date, top_n=20)
        
        if hot_industries_df is None or hot_industries_df.empty:
            hot_industries_df = self.analyze_industry_sectors(trade_date, top_n=20)
        
        # 2. 获取持续热门概念和持续热门行业
        concept_persistence_df = self.analyze_concept_persistence(trade_date, top_n=15, lookback_days=lookback_days)
        industry_persistence_df = self.analyze_industry_persistence(trade_date, top_n=15, lookback_days=lookback_days)
        
        # 3. 获取涨停池数据（用于涨停股重叠度分析）
        limit_up_df = self.dm.get_limit_up_pool(trade_date)
        
        # 4. 获取全市场股票涨幅数据（用于龙头股分析）
        all_stocks_df = self._get_all_stocks_performance(trade_date)
        
        # 5. 分析共振关系
        main_themes = []
        
        # 遍历持续热门概念
        for _, concept_row in concept_persistence_df.iterrows():
            concept_name = concept_row['板块名称']
            concept_stocks = self._get_sector_stocks_set(concept_name, trade_date)
            
            if not concept_stocks:
                continue
            
            # 获取概念的涨停股
            concept_limit_up_stocks = self._get_sector_limit_up_stocks(concept_name, trade_date, limit_up_df)
            
            # 获取概念的龙头股（涨幅前10）
            concept_top_performers = self._get_sector_top_performers(concept_name, trade_date, all_stocks_df, top_n=10)
            
            # 获取概念近5日涨幅数据（用于趋势同步性）
            concept_trend = self._get_sector_trend(concept_name, trade_date, days=5)
            
            # 获取概念近5日资金流向（用于资金流向共振）
            concept_moneyflow = self._get_sector_moneyflow_trend(concept_name, trade_date, days=5)
            
            # 找与该概念共振的行业
            for _, industry_row in industry_persistence_df.iterrows():
                industry_name = industry_row['板块名称']
                industry_stocks = self._get_sector_stocks_set(industry_name, trade_date)
                
                if not industry_stocks:
                    continue
                
                # ========== 多维度共振分析（六维模型）==========
                
                # 维度1：成分股重叠度（权重15%）
                overlap = concept_stocks & industry_stocks
                stock_overlap_score = len(overlap) / len(concept_stocks) * 100 if concept_stocks else 0
                
                # 维度2：涨停股重叠度（权重15%）
                industry_limit_up_stocks = self._get_sector_limit_up_stocks(industry_name, trade_date, limit_up_df)
                limit_up_overlap = concept_limit_up_stocks & industry_limit_up_stocks
                limit_up_overlap_score = len(limit_up_overlap) / max(len(concept_limit_up_stocks), 1) * 100 if concept_limit_up_stocks else 0
                
                # 维度3：龙头股重叠度（权重20%）- 基于涨幅前10
                industry_top_performers = self._get_sector_top_performers(industry_name, trade_date, all_stocks_df, top_n=10)
                top_performer_overlap = concept_top_performers & industry_top_performers
                top_performer_score = len(top_performer_overlap) / max(len(concept_top_performers), 1) * 100 if concept_top_performers else 0
                
                # 维度4：领涨股重叠度（权重10%）- 涨停股中封单最大的前3只
                concept_leaders = self._get_sector_leaders(concept_name, trade_date, limit_up_df, top_n=3)
                industry_leaders = self._get_sector_leaders(industry_name, trade_date, limit_up_df, top_n=3)
                leader_overlap = concept_leaders & industry_leaders
                leader_overlap_score = len(leader_overlap) / max(len(concept_leaders), 1) * 100 if concept_leaders else 0
                
                # 维度5：热度趋势同步性（权重20%）
                industry_trend = self._get_sector_trend(industry_name, trade_date, days=5)
                trend_sync_score = self._calculate_trend_sync(concept_trend, industry_trend)
                
                # 维度6：资金流向共振（权重20%）
                industry_moneyflow = self._get_sector_moneyflow_trend(industry_name, trade_date, days=5)
                moneyflow_sync_score = self._calculate_moneyflow_sync(concept_moneyflow, industry_moneyflow)
                
                # 综合共振度（加权平均）
                composite_resonance = (
                    stock_overlap_score * 0.15 +
                    limit_up_overlap_score * 0.15 +
                    top_performer_score * 0.20 +
                    leader_overlap_score * 0.10 +
                    trend_sync_score * 0.20 +
                    moneyflow_sync_score * 0.20
                )
                
                # 降低阈值到12%，六维模型更容易识别弱共振
                if composite_resonance >= 12:
                    # 计算综合评分
                    concept_score = concept_row['持续性评分']
                    industry_score = industry_row['持续性评分']
                    persistence_avg = (concept_score + industry_score) / 2
                    composite_score = (composite_resonance * 0.5 + persistence_avg * 0.5)
                    
                    # 判断所处阶段（使用'热点天数'字段）
                    concept_days = concept_row['热点天数']
                    industry_days = industry_row['热点天数']
                    min_days = min(concept_days, industry_days)
                    
                    if min_days >= lookback_days * 0.7:
                        stage = '成熟期'
                    elif min_days >= lookback_days * 0.5:
                        stage = '成长期'
                    elif min_days >= lookback_days * 0.3:
                        stage = '萌芽期'
                    else:
                        stage = '衰退期'
                    
                    # 生成操作建议
                    if stage == '成熟期':
                        advice = '持有观察，谨慎追高'
                    elif stage == '成长期':
                        advice = '积极关注，逢低布局'
                    elif stage == '萌芽期':
                        advice = '重点关注，试错参与'
                    else:
                        advice = '观望，等待信号'
                    
                    main_themes.append({
                        '主线名称': f"{concept_name}+{industry_name}",
                        '核心概念': concept_name,
                        '核心行业': industry_name,
                        '共振度': round(composite_resonance, 1),  # 保留原字段名兼容
                        '综合共振度': round(composite_resonance, 1),
                        '成分股重叠': round(stock_overlap_score, 1),
                        '涨停股重叠': round(limit_up_overlap_score, 1),
                        '龙头股重叠': round(top_performer_score, 1),
                        '领涨股重叠': round(leader_overlap_score, 1),
                        '趋势同步性': round(trend_sync_score, 1),
                        '资金共振度': round(moneyflow_sync_score, 1),
                        '概念持续性': concept_days,
                        '行业持续性': industry_days,
                        '重叠股票数': len(overlap),
                        '涨停重叠数': len(limit_up_overlap),
                        '龙头重叠数': len(top_performer_overlap),
                        '领涨重叠数': len(leader_overlap),
                        '综合评分': round(composite_score, 1),
                        '所处阶段': stage,
                        '操作建议': advice
                    })
        
        if not main_themes:
            logger.warning("[analyze_concept_industry_resonance] 未找到概念和行业的强共振关系")
            return pd.DataFrame()
        
        # 5. 按综合评分排序
        result_df = pd.DataFrame(main_themes)
        result_df = result_df.sort_values('综合评分', ascending=False)
        
        logger.info("-" * 80)
        logger.info(f"【共振分析结果】共识别 {len(result_df)} 条市场主线")
        for idx, row in result_df.head(5).iterrows():
            logger.info(f"  Top{idx+1}: {row['主线名称']} - "
                       f"综合共振度{row['综合共振度']}% "
                       f"(成分股{row['成分股重叠']}%|涨停{row['涨停股重叠']}%|龙头{row['龙头股重叠']}%|领涨{row['领涨股重叠']}%|趋势{row['趋势同步性']}%|资金{row['资金共振度']}%), "
                       f"综合评分{row['综合评分']}, 阶段:{row['所处阶段']}")
        logger.info("=" * 80)
        
        return result_df
    
    def _get_sector_stocks_set(self, sector_name: str, trade_date: str) -> set:
        """
        获取板块的成分股代码集合
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            
        Returns:
            set: 成分股代码集合
        """
        try:
            # 先获取板块代码
            concept_list, industry_list = self._load_sector_list()
            
            # 在概念列表中查找
            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                # 在行业列表中查找
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return set()
            
            # 获取成分股
            members = self.get_sector_members(ts_code)
            if not members.empty and 'ts_code' in members.columns:
                return set(members['ts_code'].tolist())
            return set()
        except Exception as e:
            logger.warning(f"[_get_sector_stocks_set] 获取 {sector_name} 成分股失败: {e}")
            return set()
    
    def _get_sector_limit_up_stocks(self, sector_name: str, trade_date: str, 
                                     limit_up_df: pd.DataFrame = None) -> set:
        """
        获取板块的涨停股代码集合
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            limit_up_df: 涨停池数据
            
        Returns:
            set: 涨停股代码集合（6位数字格式）
        """
        try:
            if limit_up_df is None or limit_up_df.empty:
                return set()
            
            # 获取板块成分股
            sector_stocks = self._get_sector_stocks_set(sector_name, trade_date)
            if not sector_stocks:
                return set()
            
            # 标准化成分股代码（去除后缀）
            sector_codes = set()
            for code in sector_stocks:
                clean_code = str(code).split('.')[0].zfill(6)
                sector_codes.add(clean_code)
            
            # 从涨停池筛选属于该板块的股票
            limit_up_codes = set()
            code_col = None
            if '代码' in limit_up_df.columns:
                code_col = '代码'
            elif 'code' in limit_up_df.columns:
                code_col = 'code'
            elif 'ts_code' in limit_up_df.columns:
                code_col = 'ts_code'
            
            if code_col:
                for _, row in limit_up_df.iterrows():
                    code = str(row[code_col]).split('.')[0].zfill(6)
                    if code in sector_codes:
                        limit_up_codes.add(code)
            
            return limit_up_codes
        except Exception as e:
            logger.warning(f"[_get_sector_limit_up_stocks] 获取 {sector_name} 涨停股失败: {e}")
            return set()
    
    def _get_sector_leaders(self, sector_name: str, trade_date: str,
                            limit_up_df: pd.DataFrame = None, top_n: int = 3) -> set:
        """
        获取板块的领涨股代码集合（封单金额最大的前N只）
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            limit_up_df: 涨停池数据
            top_n: 取前N只领涨股
            
        Returns:
            set: 领涨股代码集合
        """
        try:
            if limit_up_df is None or limit_up_df.empty:
                return set()
            
            # 获取板块涨停股
            limit_up_stocks = self._get_sector_limit_up_stocks(sector_name, trade_date, limit_up_df)
            if not limit_up_stocks:
                return set()
            
            # 标准化涨停池代码列
            code_col = None
            if '代码' in limit_up_df.columns:
                code_col = '代码'
            elif 'code' in limit_up_df.columns:
                code_col = 'code'
            elif 'ts_code' in limit_up_df.columns:
                code_col = 'ts_code'
            
            if not code_col:
                return set()
            
            # 添加标准化代码列用于筛选
            limit_up_df = limit_up_df.copy()
            limit_up_df['clean_code'] = limit_up_df[code_col].astype(str).str.split('.').str[0].str.zfill(6)
            
            # 筛选属于该板块的涨停股
            sector_limit_up = limit_up_df[limit_up_df['clean_code'].isin(limit_up_stocks)]
            if sector_limit_up.empty:
                return set()
            
            # 按封单金额排序，取前N只
            # 尝试不同的封单金额字段名
            amount_col = None
            for col in ['封单额', '封单金额', 'seal_amount', 'bid_amount']:
                if col in sector_limit_up.columns:
                    amount_col = col
                    break
            
            if amount_col:
                sector_limit_up = sector_limit_up.sort_values(amount_col, ascending=False)
            
            leaders = set(sector_limit_up.head(top_n)['clean_code'].tolist())
            return leaders
        except Exception as e:
            logger.warning(f"[_get_sector_leaders] 获取 {sector_name} 领涨股失败: {e}")
            return set()
    
    def _get_sector_trend(self, sector_name: str, trade_date: str, days: int = 5) -> List[float]:
        """
        获取板块近N日的涨幅趋势
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            days: 回溯天数
            
        Returns:
            List[float]: 每日涨幅列表（从最早到最近）
        """
        try:
            # 获取板块代码
            concept_list, industry_list = self._load_sector_list()
            
            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return []
            
            # 获取近N日行情数据
            end_date = datetime.strptime(trade_date, "%Y%m%d")
            start_date = end_date - timedelta(days=days * 2)  # 多取一些，防止节假日
            
            daily_data = self.dm.get_ths_daily(ts_code=ts_code, 
                                               start_date=start_date.strftime("%Y%m%d"),
                                               end_date=trade_date)
            
            if daily_data.empty or 'pct_change' not in daily_data.columns:
                return []
            
            # 按日期排序，取最近N天
            daily_data = daily_data.sort_values('trade_date')
            pct_changes = daily_data['pct_change'].tail(days).tolist()
            
            return pct_changes
        except Exception as e:
            logger.warning(f"[_get_sector_trend] 获取 {sector_name} 趋势失败: {e}")
            return []
    
    def _calculate_trend_sync(self, trend1: List[float], trend2: List[float]) -> float:
        """
        计算两个趋势的同步性得分
        
        Args:
            trend1: 第一个趋势的涨幅列表
            trend2: 第二个趋势的涨幅列表
            
        Returns:
            float: 同步性得分（0-100）
        """
        try:
            if not trend1 or not trend2 or len(trend1) < 2 or len(trend2) < 2:
                return 50.0  # 默认中等同步性
            
            # 确保长度一致
            min_len = min(len(trend1), len(trend2))
            trend1 = trend1[-min_len:]
            trend2 = trend2[-min_len:]
            
            # 计算相关系数
            if len(trend1) < 2:
                return 50.0
            
            correlation = np.corrcoef(trend1, trend2)[0, 1]
            
            # 将相关系数（-1到1）映射到得分（0到100）
            # 相关系数越接近1，同步性越高
            if np.isnan(correlation):
                return 50.0
            
            sync_score = (correlation + 1) / 2 * 100
            return round(sync_score, 1)
        except Exception as e:
            logger.warning(f"[_calculate_trend_sync] 计算趋势同步性失败: {e}")
            return 50.0
    
    def _get_all_stocks_performance(self, trade_date: str) -> pd.DataFrame:
        """
        获取全市场股票的涨幅数据
        
        Args:
            trade_date: 交易日期
            
        Returns:
            DataFrame: 包含股票代码和涨幅的数据
        """
        try:
            # 使用get_all_rt_k_data获取当日所有股票行情（包含pct_change涨跌幅）
            daily_data = self.dm.get_all_rt_k_data(trade_date=trade_date)
            if daily_data.empty:
                return pd.DataFrame()
            
            # 标准化代码格式
            if 'ts_code' in daily_data.columns:
                daily_data['code'] = daily_data['ts_code'].astype(str).str.split('.').str[0].str.zfill(6)
            elif 'code' in daily_data.columns:
                daily_data['code'] = daily_data['code'].astype(str).str.zfill(6)
            
            return daily_data
        except Exception as e:
            logger.warning(f"[_get_all_stocks_performance] 获取全市场数据失败: {e}")
            return pd.DataFrame()
    
    def _get_sector_top_performers(self, sector_name: str, trade_date: str,
                                    all_stocks_df: pd.DataFrame = None, top_n: int = 10) -> set:
        """
        获取板块的涨幅龙头股票（涨幅前N）
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            all_stocks_df: 全市场股票数据
            top_n: 取前N只
            
        Returns:
            set: 龙头股代码集合
        """
        try:
            # 获取板块成分股
            sector_stocks = self._get_sector_stocks_set(sector_name, trade_date)
            if not sector_stocks:
                return set()
            
            # 标准化成分股代码
            sector_codes = set()
            for code in sector_stocks:
                clean_code = str(code).split('.')[0].zfill(6)
                sector_codes.add(clean_code)
            
            # 如果没有提供全市场数据，则获取
            if all_stocks_df is None or all_stocks_df.empty:
                all_stocks_df = self._get_all_stocks_performance(trade_date)
            
            if all_stocks_df.empty:
                return set()
            
            # 筛选板块内股票
            if 'code' not in all_stocks_df.columns:
                return set()
            
            sector_df = all_stocks_df[all_stocks_df['code'].isin(sector_codes)]
            if sector_df.empty:
                return set()
            
            # 按涨幅排序，取前N
            if 'pct_change' in sector_df.columns:
                sector_df = sector_df.sort_values('pct_change', ascending=False)
            elif '涨跌幅' in sector_df.columns:
                sector_df = sector_df.sort_values('涨跌幅', ascending=False)
            else:
                return set()
            
            top_performers = set(sector_df.head(top_n)['code'].tolist())
            return top_performers
        except Exception as e:
            logger.warning(f"[_get_sector_top_performers] 获取 {sector_name} 龙头股失败: {e}")
            return set()
    
    def _get_sector_moneyflow_trend(self, sector_name: str, trade_date: str, days: int = 5) -> List[float]:
        """
        获取板块近N日的资金流向趋势
        
        Args:
            sector_name: 板块名称
            trade_date: 交易日期
            days: 回溯天数
            
        Returns:
            List[float]: 每日资金净流入（亿元）列表
        """
        try:
            # 获取板块代码
            concept_list, industry_list = self._load_sector_list()
            
            sector_row = concept_list[concept_list['name'] == sector_name]
            if not sector_row.empty:
                ts_code = sector_row.iloc[0]['ts_code']
            else:
                sector_row = industry_list[industry_list['name'] == sector_name]
                if not sector_row.empty:
                    ts_code = sector_row.iloc[0]['ts_code']
                else:
                    return []
            
            # 获取近N日行情数据（包含成交额）
            end_date = datetime.strptime(trade_date, "%Y%m%d")
            start_date = end_date - timedelta(days=days * 2)
            
            daily_data = self.dm.get_ths_daily(ts_code=ts_code,
                                               start_date=start_date.strftime("%Y%m%d"),
                                               end_date=trade_date)
            
            if daily_data.empty:
                return []
            
            # 计算资金净流入（使用成交额和涨跌幅估算）
            # 简化模型：涨幅>0认为资金净流入，涨幅<0认为资金净流出
            # 实际应该使用MoneyFlowAnalyzer获取真实的资金流向数据
            daily_data = daily_data.sort_values('trade_date')
            
            moneyflow_list = []
            for _, row in daily_data.tail(days).iterrows():
                pct_change = row.get('pct_change', 0)
                amount = row.get('amount', 0)  # 千元
                
                # 估算资金净流入（简化模型）
                # 涨幅越大，净流入比例越高
                if pct_change > 0:
                    net_flow = amount * pct_change / 100  # 简化估算
                else:
                    net_flow = amount * pct_change / 100  # 负值表示流出
                
                moneyflow_list.append(net_flow / 100000)  # 转换为亿元
            
            return moneyflow_list
        except Exception as e:
            logger.warning(f"[_get_sector_moneyflow_trend] 获取 {sector_name} 资金流向失败: {e}")
            return []
    
    def _calculate_moneyflow_sync(self, moneyflow1: List[float], moneyflow2: List[float]) -> float:
        """
        计算两个资金流向序列的同步性得分
        
        Args:
            moneyflow1: 第一个资金流向列表
            moneyflow2: 第二个资金流向列表
            
        Returns:
            float: 资金同步性得分（0-100）
        """
        try:
            if not moneyflow1 or not moneyflow2 or len(moneyflow1) < 2 or len(moneyflow2) < 2:
                return 50.0  # 默认中等同步性
            
            # 确保长度一致
            min_len = min(len(moneyflow1), len(moneyflow2))
            moneyflow1 = moneyflow1[-min_len:]
            moneyflow2 = moneyflow2[-min_len:]
            
            # 判断资金流向方向（正=流入，负=流出）
            direction1 = [1 if m > 0 else -1 for m in moneyflow1]
            direction2 = [1 if m > 0 else -1 for m in moneyflow2]
            
            # 计算方向一致率
            same_direction_count = sum(1 for d1, d2 in zip(direction1, direction2) if d1 == d2)
            direction_sync = same_direction_count / len(direction1) * 100
            
            # 计算相关系数（数值相关性）
            try:
                correlation = np.corrcoef(moneyflow1, moneyflow2)[0, 1]
                if np.isnan(correlation):
                    correlation = 0
            except:
                correlation = 0
            
            # 综合得分：方向一致率60% + 数值相关性40%
            correlation_score = (correlation + 1) / 2 * 100  # 映射到0-100
            sync_score = direction_sync * 0.6 + correlation_score * 0.4
            
            return round(sync_score, 1)
        except Exception as e:
            logger.warning(f"[_calculate_moneyflow_sync] 计算资金同步性失败: {e}")
            return 50.0


if __name__ == "__main__":
    # 测试
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from core.data.data_manager import DataManager
    from config.settings import TUSHARE_TOKEN, CACHE_DIR

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    tracker = THSSectorTracker(dm)

    # 测试板块分析
    print("="*80)
    print("同花顺板块分析测试")
    print("="*80)

    trade_date = datetime.now().strftime("%Y%m%d")
    
    # 测试概念板块分析
    concept_df = tracker.analyze_concept_sectors(trade_date, top_n=10)
    if not concept_df.empty:
        print(f"\n热点概念TOP10:")
        print(concept_df[['name', 'pct_change', 'composite_score', 'is_hot']].to_string(index=False))
    
    # 测试行业板块分析
    industry_df = tracker.analyze_industry_sectors(trade_date, top_n=10)
    if not industry_df.empty:
        print(f"\n热点行业TOP10:")
        print(industry_df[['name', 'pct_change', 'composite_score', 'is_hot']].to_string(index=False))
    
    # 测试概念-行业共振
    resonance_df = tracker.analyze_concept_industry_resonance(trade_date, lookback_days=5)
    if not resonance_df.empty:
        print(f"\n概念-行业共振:")
        print(resonance_df[['主线名称', '共振度', '综合评分', '所处阶段']].head().to_string(index=False))
    else:
        print("未获取到数据")
