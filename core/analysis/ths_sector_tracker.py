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
from core.analysis.ths_persistence_mixin import THSPersistenceMixin
from core.analysis.ths_resonance_mixin import THSResonanceMixin
from core.analysis.ths_main_theme_mixin import THSMainThemeMixin

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


class THSSectorTracker(
    THSPersistenceMixin,
    THSResonanceMixin,
    THSMainThemeMixin,
):
    """
    同花顺板块追踪器

    功能：
    1. 获取同花顺概念和行业板块数据
    2. 计算板块强度指标
    3. 建立概念-行业关联（通过成分股重叠）
    4. 识别热点板块

    架构（拆分自原 2425 行单体类）：
      - ``THSPersistenceMixin``   : 板块持续性分析 (analyze_*_persistence)
      - ``THSResonanceMixin``     : 概念-行业六维共振 (analyze_concept_industry_resonance + 辅助方法)
      - ``THSMainThemeMixin``     : 市场主线识别四维评分 (identify_main_themes + _calc_*)
      - 主类本体                  : 数据获取、配置加载、板块基础分析与内部结构分析

    外部 API 完全兼容，所有原方法仍可在 ``THSSectorTracker`` 实例上直接调用。
    """

    def __init__(self, data_manager, config: Optional[Dict] = None, repo=None):
        self.dm = data_manager
        if repo is None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(data_manager)
        self.repo = repo
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
            self._concept_list = self.repo.get_ths_index(index_type='N')
            # 获取行业指数
            self._industry_list = self.repo.get_ths_index(index_type='I')

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
            members = self.repo.get_ths_member(ts_code=ts_code)
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
        daily_df = self.repo.get_ths_daily(trade_date=trade_date)
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
                limit_cpt_df = self.repo.get_limit_cpt_list(trade_date)
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
        daily_df = self.repo.get_ths_daily(trade_date=trade_date)
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
        sector_daily = self.repo.get_ths_daily(ts_code=ts_code, trade_date=trade_date)

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

if __name__ == "__main__":
    # 测试
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from core.data.data_manager_main import DataManager
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