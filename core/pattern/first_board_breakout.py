"""
热点首板突破策略 - 基于二级行业的首板分析
核心逻辑：
1. 从sector_heat_v2获取热点二级行业
2. 分析每个热点行业中的首板股票
3. 结合技术指标筛选优质首板
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
import loguru
import sys

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
# 使用新版同花顺板块追踪器
from core.analysis.ths_sector_tracker import THSSectorTracker as SectorRotationTracker
from config.pattern_params import get_params

logger = loguru.logger


class PatternType(Enum):
    HOTSPOT_FIRST_BOARD = "热点首板突破"


@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str
    reason: str
    key_metrics: Dict
    validation_rules: List[str]
    l2_industry: str = ""  # 二级行业
    buy_strategy: str = ""  # 买点策略：主买点/次买点
    next_day_expectation: str = ""  # 次日预期
    is_dual_resonance: bool = False  # 是否双热点共振（概念+行业同时热点）
    is_hot_sector: bool = False  # 是否属于热点板块
    matched_sector_name: str = ""  # 匹配到的热点板块名称（概念或行业）
    matched_sector_type: str = ""  # 匹配到的热点板块类型（概念/行业）


class HotspotFirstBoardStrategy:
    def __init__(self, data_manager, sector_engine=None, mapper=None):
        """
        热点首板突破策略 - 基于套牢盘动态调整因子要求

        核心逻辑：
        - 上方套牢盘多时，量能、封单等因子要求更高
        - 上方套牢盘少时，相关要求降低

        Args:
            data_manager: 数据管理器（DataManager）
            sector_engine: 板块热度引擎（可选）
            mapper: 行业映射器（可选）
        """
        self.dm = data_manager
        self.sector_engine = sector_engine
        self.mapper = mapper

        # 基础参数（默认值见 config/pattern_params.py，支持网页覆盖）
        self.params = get_params("first_board_breakout")

    def _get_dynamic_thresholds(self, distance_from_high_pct: float, breakout_type: str) -> Dict:
        """
        根据套牢盘情况获取动态阈值

        套牢盘判断标准（距前高距离）：
        - 突破前高：套牢盘已完全解放，要求最低
        - <10%：套牢盘很少，要求较低
        - 10%-20%：套牢盘适中，标准要求
        - >=20%：套牢盘较多，要求最高（但本策略最大允许20%）

        Args:
            distance_from_high_pct: 距前高距离（0.0-1.0）
            breakout_type: 突破类型（"前高突破"/"平台突破"）

        Returns:
            Dict: 动态阈值配置
        """
        # 判断套牢盘等级
        if breakout_type == "前高突破":
            chip_level = "break_high"  # 突破前高，无套牢盘
        elif distance_from_high_pct < 0.15:
            chip_level = "low"         # 套牢盘很少
        elif distance_from_high_pct < 0.25:
            chip_level = "medium"      # 套牢盘适中
        else:
            chip_level = "high"        # 套牢盘较多（理论上不会进入，因为max_distance_from_high=0.25）

        # 根据套牢盘等级返回动态阈值
        # 逻辑：套牢盘越少，抛压越小，封板时间要求越宽松
        thresholds = {
            "break_high": {
                "min_volume_ratio": 0.8,      # 量能>80%（无套牢盘，要求最低）
                "max_limit_up_time": "11:00", # 最晚11:00前涨停（最宽松）
                "min_seal_ratio": 0.015,       # 封单>1.5%
                "volume_desc": "突破前高，无套牢盘"
            },
            "low": {
                "min_volume_ratio": 1.15,      # 量能>115%（套牢盘少，要求较低）
                "max_limit_up_time": "10:45", # 最晚10:45前涨停（较宽松）
                "min_seal_ratio": 0.025,       # 封单>2.5%
                "volume_desc": f"距前高{distance_from_high_pct*100:.1f}%<15%，套牢盘少"
            },
            "medium": {
                "min_volume_ratio": 1.5,      # 量能>150%（标准要求）
                "max_limit_up_time": "10:15", # 最晚10:15前涨停（标准）
                "min_seal_ratio": 0.035,       # 封单>3.5%
                "volume_desc": f"距前高{distance_from_high_pct*100:.1f}%<25%，套牢盘适中"
            },
            "high": {
                "min_volume_ratio": 2.0,      # 量能>200%（套牢盘多，要求最高）
                "max_limit_up_time": "09:45", # 最晚9:45前涨停（最严格）
                "min_seal_ratio": 0.04,       # 封单>4%
                "volume_desc": f"距前高{distance_from_high_pct*100:.1f}%>=25%，套牢盘多"
            }
        }

        return thresholds.get(chip_level, thresholds["medium"])

    def _calculate_sector_effect_score(self, stock: pd.Series, sector_info: Dict,
                                        today_zt_for_sector: pd.DataFrame = None,
                                        is_hot_sector: bool = False,
                                        is_dual_resonance: bool = False) -> float:
        """
        Layer 4 评分：板块效应（加分项，非硬排除）

        设计原则：板块效应是加分维度，不是硬排除条件。
        好的首板本身就是板块效应的发起者（先有龙头首板，后带动板块发酵）。
        因此板块效应弱不等于标的不行，只是置信度降低。

        评分逻辑：
        - 板块涨停家数 → 板块强度分（0.00 ~ 0.06）
        - 是否热点板块 → 热点分（0.00 ~ 0.02）
        - 概念+行业双共振 → 共振分（0.00 ~ 0.02）
        - 总分上限 0.10

        Args:
            stock: 股票数据
            sector_info: 行业/板块信息
            today_zt_for_sector: 今日涨停池（用于统计板块内涨停数，可选）
            is_hot_sector: 是否热点板块
            is_dual_resonance: 是否双共振

        Returns:
            float: 板块效应评分（0.00 ~ 0.10）
        """
        score = 0.0

        # 板块涨停家数得分（板块效应强度的直接衡量）
        sector_stats = sector_info.get('stats', {})
        sector_limit_up_count = sector_stats.get('涨停家数', 0)

        if sector_limit_up_count >= 8:
            score += 0.06  # 强板块效应：涨停≥8家
        elif sector_limit_up_count >= 5:
            score += 0.04  # 中等板块效应：涨停5-7家
        elif sector_limit_up_count >= 3:
            score += 0.02  # 弱板块效应：涨停3-4家
        else:
            score += 0.00  # 独狼板：不扣分，只是不加分

        # 热点板块加分
        if is_dual_resonance:
            score += 0.04  # 双共振：概念+行业同时热点
        elif is_hot_sector:
            score += 0.02  # 单一热点：概念或行业热点

        # 限制上限
        return min(score, 0.10)

    def detect_first_board_by_sectors(self,
                                      today_zt: pd.DataFrame,
                                      history_pools: Dict[str, pd.DataFrame],
                                      date_str: str,
                                      hot_sectors: List[Dict] = None,
                                      concept_hierarchy: Dict = None,
                                      all_hot_member_codes: set = None,
                                      stock_to_hot_sectors: Dict[str, list] = None,
                                      stock_to_ths_industry: Dict[str, str] = None,
                                      stock_to_ths_concept: Dict[str, str] = None,
                                      stock_to_ths_ts_code: Dict[str, str] = None) -> List[TradeSignal]:
        """
        基于二级行业的首板突破检测

        流程：
        1. 获取热点板块（从参数传入，包含概念+行业，含member_codes）
        2. 分析所有涨停股票中的首板（不限于热点板块）
        3. 结合技术指标进行过滤
        4. 属于热点板块的股票增加仓位权重
        5. 同时属于热点概念+热点行业的股票获得双共振置信度加成

        Args:
            today_zt: 今日涨停池
            history_pools: 历史涨停池（用于板块分析）
            date_str: 日期字符串YYYYMMDD
            hot_sectors: 预计算的热点板块列表（含member_codes，避免重复计算）
            concept_hierarchy: 概念连板梯队数据（用于复盘参考）
            all_hot_member_codes: 预计算的热点成分股集合（6位纯数字，用于O(1)判定）
            stock_to_hot_sectors: 预计算的股票→热点板块列表映射

        Returns:
            List[TradeSignal]: 符合条件的交易信号
        """
        signals = []

        if today_zt.empty:
            logger.warning("涨停池为空，无法检测首板突破")
            return signals

        # 1. 获取热点板块（同花顺概念/行业）
        if not hot_sectors:
            hot_sectors = self._get_hot_sectors(today_zt, history_pools, date_str)
        else:
            logger.info(f"[首板突破] 使用传入的热点板块数据: {len(hot_sectors)}个")

        # 构建热点板块代码集合（按概念/行业分开，用于双共振检测）
        hot_concept_codes = set()
        hot_industry_codes = set()
        hot_concept_names = set()
        hot_industry_names = set()

        for hs in hot_sectors:
            sector_type = hs.get('sector_type', '')
            member_codes = hs.get('member_codes', set())
            sector_name = hs.get('sector_name', '')

            if sector_type == '概念':
                hot_concept_codes.update(member_codes)
                hot_concept_names.add(sector_name)
            elif sector_type == '行业':
                hot_industry_codes.update(member_codes)
                hot_industry_names.add(sector_name)
            else:
                # 未知类型，同时加入两个集合
                hot_concept_codes.update(member_codes)
                hot_industry_codes.update(member_codes)

        # 全部热点代码（用于向后兼容的快速匹配）
        hot_sector_codes = hot_concept_codes | hot_industry_codes

        logger.info(f"[首板突破] 识别到 {len(hot_sectors)} 个热点板块")
        logger.info(f"[首板突破]   热点概念: {len(hot_concept_names)}个, 成分股{len(hot_concept_codes)}只")
        logger.info(f"[首板突破]   热点行业: {len(hot_industry_names)}个, 成分股{len(hot_industry_codes)}只")
        logger.info(f"[首板突破]   全部热点成分股: {len(hot_sector_codes)}只")

        # 2. 获取昨日涨停池（用于确认首板）
        yesterday_date = self._get_date_offset(date_str, -1)
        yesterday_zt = history_pools.get(yesterday_date, pd.DataFrame())

        # 3. THS行业/概念/板块代码映射
        #    若外部已传入（来自 pattern_recognition 集中查询，覆盖所有股票），直接使用
        #    否则回退旧逻辑：从 hot_sectors.member_codes 构建（仅覆盖热点板块成分股）
        if stock_to_ths_industry is not None and stock_to_ths_concept is not None:
            ths_industry_map = stock_to_ths_industry
            ths_concept_map = stock_to_ths_concept
            ths_ts_code_map = stock_to_ths_ts_code if stock_to_ths_ts_code is not None else {}
            logger.info(f"[首板突破] 使用外部传入的THS映射: 行业{len(ths_industry_map)}只, "
                       f"概念{len(ths_concept_map)}只")
        else:
            ths_industry_map = {}
            ths_concept_map = {}
            ths_ts_code_map = {}
            for hs in hot_sectors:
                sector_name = hs.get('sector_name', '')
                sector_type = hs.get('sector_type', '')
                ts_code = hs.get('ts_code', '')
                member_codes = hs.get('member_codes', set())
                if sector_type == '行业':
                    for code in member_codes:
                        if code not in ths_industry_map:
                            ths_industry_map[code] = sector_name
                            ths_ts_code_map[code] = ts_code
                elif sector_type == '概念':
                    for code in member_codes:
                        if code not in ths_concept_map:
                            ths_concept_map[code] = sector_name
                            ths_ts_code_map[code] = ts_code
            logger.info(f"[首板突破] 从hot_sectors构建THS映射: 行业{len(ths_industry_map)}只, "
                       f"概念{len(ths_concept_map)}只")

        # THS板块涨停家数统计（从hot_sectors统计，用于板块强度）
        ths_sector_limit_up_counts = {}
        for hs in hot_sectors:
            sector_name = hs.get('sector_name', '')
            member_codes = hs.get('member_codes', set())
            ths_limit_up_count = 0
            for _, zt_stock in today_zt.iterrows():
                zt_code = str(zt_stock.get('代码', '')).zfill(6)
                if zt_code in member_codes:
                    ths_limit_up_count += 1
            ths_sector_limit_up_counts[sector_name] = ths_limit_up_count

        # 将映射统一到 stock_to_ths_* 变量名，供后续代码使用
        stock_to_ths_industry = ths_industry_map
        stock_to_ths_concept = ths_concept_map
        stock_to_ths_ts_code = ths_ts_code_map

        logger.info(f"[首板突破] THS板块涨停统计: {len(ths_sector_limit_up_counts)}个板块")

        # 4. 分析所有涨停股票中的首板（不限于热点板块）
        total_analyzed = 0
        total_filtered = 0

        for _, stock in today_zt.iterrows():
            total_analyzed += 1

            stock_code = str(stock.get('代码', '')).zfill(6)

            # 获取THS行业/概念名称（优先THS，回退东财）
            ths_industry_name = stock_to_ths_industry.get(stock_code, '')
            ths_concept_name = stock_to_ths_concept.get(stock_code, '')
            # 优先使用THS行业名，其次THS概念名，最后回退东财行业
            ths_sector_name = ths_industry_name or ths_concept_name or stock.get('所属行业', '') or stock.get('L2_Industry', '')

            # 获取该股票所属THS板块的涨停家数（优先THS，回退东财行业列）
            sector_limit_up_count = 0
            if ths_industry_name and ths_industry_name in ths_sector_limit_up_counts:
                sector_limit_up_count = ths_sector_limit_up_counts[ths_industry_name]
            elif ths_concept_name and ths_concept_name in ths_sector_limit_up_counts:
                sector_limit_up_count = ths_sector_limit_up_counts[ths_concept_name]
            else:
                # THS映射未覆盖，回退用东财行业列统计
                dongcai_sector = stock.get('所属行业', '') or stock.get('L2_Industry', '')
                if dongcai_sector:
                    sector_limit_up_count = sum(
                        1 for _, zt in today_zt.iterrows()
                        if (zt.get('所属行业', '') or zt.get('L2_Industry', '')) == dongcai_sector
                    )

            # 判断该股票是否属于热点板块（优先使用预构建集合，O(1)判定）
            if all_hot_member_codes is not None:
                is_hot_sector = stock_code in all_hot_member_codes
                is_hot_concept = stock_code in hot_concept_codes
                is_hot_industry = stock_code in hot_industry_codes
                is_dual_resonance = is_hot_concept and is_hot_industry
            else:
                is_hot_concept = stock_code in hot_concept_codes
                is_hot_industry = stock_code in hot_industry_codes
                is_hot_sector = is_hot_concept or is_hot_industry
                is_dual_resonance = is_hot_concept and is_hot_industry

            # 找到匹配的热点板块信息
            matched_hot_concept = None
            matched_hot_industry = None

            if is_hot_concept:
                for hs in hot_sectors:
                    if hs.get('sector_type', '') == '概念' and stock_code in hs.get('member_codes', set()):
                        matched_hot_concept = hs
                        break

            if is_hot_industry:
                for hs in hot_sectors:
                    if hs.get('sector_type', '') == '行业' and stock_code in hs.get('member_codes', set()):
                        matched_hot_industry = hs
                        break

            # 优先使用行业热点信息，其次概念热点信息
            # 注意：is_hot_sector 来自外部预计算的 all_hot_member_codes，可能与本地
            # hot_sectors 明细不一致（个股命中热点集合，却在 hot_sectors 里找不到对应板块），
            # 此时 matched_* 均为 None。旧逻辑直接 primary_hs['sector_name'] 会触发
            # "'NoneType' object is not subscriptable"，导致整个首板检测异常退出、丢失全部信号。
            # 这里安全降级为非热点处理。
            primary_hs = matched_hot_industry or matched_hot_concept
            if is_hot_sector and primary_hs is None:
                logger.debug(f"[{stock_code}] 命中热点成分股集合但未匹配到具体板块明细，按非热点处理")
                is_hot_sector = False

            if is_hot_sector:
                sector_info = {
                    'sector_name': primary_hs['sector_name'],
                    'sector_type': primary_hs.get('sector_type', '概念'),
                    'ts_code': primary_hs.get('ts_code', ''),
                    'stats': {
                        '涨停家数': primary_hs['stats'].get('涨停家数', 0),
                        '连板家数': primary_hs['stats'].get('连板家数', 0),
                        'THS板块涨停数': sector_limit_up_count
                    },
                    'trend_stage': primary_hs['trend_stage'],
                    'action': primary_hs['action'],
                    'confidence': primary_hs['confidence']
                }
                if is_dual_resonance:
                    logger.debug(f"[{stock_code}] 双热点共振: 概念={matched_hot_concept['sector_name'] if matched_hot_concept else 'N/A'}, "
                                f"行业={matched_hot_industry['sector_name'] if matched_hot_industry else 'N/A'}")
                else:
                    logger.debug(f"[{stock_code}] 击中热点板块: {primary_hs['sector_name']}({primary_hs.get('sector_type', '')})")
            else:
                # 非热点股票：优先使用THS概念映射的名称和板块代码
                ths_concept_for_stock = stock_to_ths_concept.get(stock_code, '')
                ths_ts_code_for_stock = stock_to_ths_ts_code.get(stock_code, '')
                sector_info = {
                    'sector_name': ths_concept_for_stock or ths_sector_name or '未知行业',
                    'sector_type': 'THS概念' if ths_concept_for_stock else ('THS行业' if ths_industry_name else '未知'),
                    'ts_code': ths_ts_code_for_stock or '',
                    'stats': {
                        '涨停家数': sector_limit_up_count,
                        '连板家数': 0,
                        'THS板块涨停数': sector_limit_up_count
                    },
                    'trend_stage': '非热点',
                    'action': '',
                    'confidence': 0.0
                }

            # 确定匹配的板块信息（用于日志和信号）
            if is_dual_resonance:
                matched_sector_name = f"{matched_hot_concept['sector_name']}+{matched_hot_industry['sector_name']}"
                matched_sector_type = "概念+行业"
            elif is_hot_concept:
                matched_sector_name = matched_hot_concept['sector_name']
                matched_sector_type = "概念"
            elif is_hot_industry:
                matched_sector_name = matched_hot_industry['sector_name']
                matched_sector_type = "行业"
            else:
                matched_sector_name = ""
                matched_sector_type = ""

            # 分析首板（传入双共振标记和匹配板块信息）
            signal = self._analyze_first_board(
                stock, yesterday_zt, sector_info, date_str,
                is_hot_sector=is_hot_sector,
                is_dual_resonance=is_dual_resonance,
                matched_sector_name=matched_sector_name,
                matched_sector_type=matched_sector_type,
                concept_hierarchy=concept_hierarchy
            )
            if signal:
                signals.append(signal)
            else:
                total_filtered += 1

        # 按置信度排序
        signals.sort(key=lambda x: x.confidence, reverse=True)

        # 统计热点板块信号数量
        hot_count = sum(1 for s in signals if getattr(s, 'is_hot_sector', False))
        dual_count = sum(1 for s in signals if getattr(s, 'is_dual_resonance', False))
        logger.info(f"[首板突破] 检测完成: 共 {len(signals)} 个信号 "
                    f"(击中热点{hot_count}只, 双共振{dual_count}只, "
                    f"分析{total_analyzed}只, 过滤{total_filtered}只)")

        return signals

    def _get_hot_sectors(self, today_zt: pd.DataFrame,
                         history_pools: Dict[str, pd.DataFrame],
                         date_str: str) -> List[Dict]:
        """
        获取热点板块列表（基于同花顺板块数据）
        
        核心改进：
        1. 使用同花顺板块追踪器获取热点板块（概念+行业）
        2. 获取每个热点板块的成分股代码
        3. 通过股票代码匹配，而不是行业名称匹配
        
        Returns:
            List[Dict]: 热点板块信息列表，每个包含：
                - sector_name: 板块名称
                - sector_type: 板块类型（概念/行业）
                - ts_code: 板块代码（如885001.TI）
                - member_codes: 成分股代码集合（6位数字）
                - stats: 统计数据
                - trend_stage: 趋势阶段
        """
        hot_sectors = []

        try:
            from core.data.data_manager_main import DataManager
            from config.settings import TUSHARE_TOKEN, CACHE_DIR
            
            dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
            tracker = SectorRotationTracker(dm)
            
            # 使用传入的日期参数
            trade_date = date_str
            sector_df = tracker.analyze_sectors_persistence(trade_date, top_n=10)

            if sector_df.empty:
                logger.warning("[首板突破] 板块分析结果为空")
                return hot_sectors

            # 筛选热点板块（加速期、高潮期、启动期）
            for _, row in sector_df.iterrows():
                stage = row.get('所处阶段', '')
                if stage in ['加速期', '高潮期', '启动期', '主升浪']:
                    sector_name = row.get('板块名称', '')
                    ts_code = row.get('ts_code', '')
                    sector_type = row.get('板块类型', '概念')
                    
                    if not sector_name or not ts_code:
                        continue
                    
                    # 获取该板块的成分股
                    try:
                        members_df = tracker.get_sector_members(ts_code)
                        if not members_df.empty:
                            col = 'con_code' if 'con_code' in members_df.columns else ('code' if 'code' in members_df.columns else None)
                            if col:
                                member_codes = set()
                                for c in members_df[col].values:
                                    code_str = str(c).strip().upper()
                                    if '.' in code_str:
                                        parts = code_str.split('.')
                                        code_str = parts[0].zfill(6) + '.' + parts[1]
                                    else:
                                        code_str = code_str.zfill(6)
                                        code_str += '.SH' if (code_str.startswith('688') or code_str.startswith('6')) else '.SZ'
                                    member_codes.add(code_str)
                            else:
                                member_codes = set()
                                logger.warning(f"[首板突破] 板块{sector_name}成分股数据缺少代码列")
                        else:
                            member_codes = set()
                            logger.warning(f"[首板突破] 板块{sector_name}无成分股数据")
                    except Exception as e:
                        logger.warning(f"[首板突破] 获取板块{sector_name}成分股失败: {e}")
                        member_codes = set()
                    
                    hot_sectors.append({
                        'sector_name': sector_name,
                        'sector_type': sector_type,
                        'ts_code': ts_code,
                        'member_codes': member_codes,  # 关键：成分股代码集合
                        'stats': {
                            '涨停家数': row.get('涨停家数', 0),
                            '连板家数': row.get('连板家数', 0)
                        },
                        'trend_stage': stage,
                        'action': row.get('操作建议', ''),
                        'confidence': row.get('持续性评分', 50) / 100
                    })

            logger.info(f"[首板突破] 从同花顺板块分析中识别到 {len(hot_sectors)} 个热点板块")
            for hs in hot_sectors[:5]:
                logger.info(f"  - {hs['sector_name']}({hs['sector_type']}): {len(hs['member_codes'])}只成分股, 阶段:{hs['trend_stage']}")

        except Exception as e:
            logger.error(f"[首板突破] 获取热点板块失败: {e}")

        return hot_sectors

    def _get_sector_stocks(self, today_zt: pd.DataFrame, sector_name: str) -> pd.DataFrame:
        """
        获取指定行业的涨停股票
        """

        result = pd.DataFrame()
        if '所属行业' in today_zt.columns:
            result = today_zt[today_zt['所属行业'] == sector_name].copy()
        elif 'L2_Industry' in today_zt.columns:
            result = today_zt[today_zt['L2_Industry'] == sector_name].copy()
        else:
            logger.warning(f"    [_get_sector_stocks] 涨停池缺少行业列，可用列: {list(today_zt.columns)}")

        return result

    def _filter_layer_0_hard_exclusions(self, stock: pd.Series, sector_info: Dict, date_str: str) -> Optional[str]:
        """
        Layer 0: 硬性排除（零成本过滤 — 仅用涨停池自带字段，不拉取任何额外数据）

        优先级最高：这些是任何情况下都不应通过的条件，必须最先判断以避免浪费后续计算。

        排除条件（按判断成本从低到高）：
        1. 非涨停 / 非首板 — 连板数 != 1 或 涨跌幅 < 9.5%
        2. 一字板 — 无换手，次日大概率高开低走
        3. 尾盘板 — 14:30后涨停，偷袭板确定性低

        Returns:
            str: 排除原因，None表示通过Layer 0
        """
        code = str(stock.get('代码', '')).zfill(6)
        name = stock.get('名称', '')

        # 1. 首板确认：连板数必须为1
        board_height = stock.get('连板数', 0)
        if board_height != 1:
            return f"非首板(连板数={board_height})"

        # 2. 涨停确认：涨幅必须>=9.5%
        change = stock.get('涨跌幅', 0)
        if isinstance(change, str):
            change = float(change.replace('%', ''))
        if change < 9.5:
            return f"涨幅不足({change:.2f}%<9.5%)"

        # 3. 一字板排除：无开板+一字类型
        break_count = stock.get('开板次数', 0)
        limit_type = stock.get('涨停类型', '')
        if break_count == 0 and ('一字' in str(limit_type) or limit_type == '1'):
            return f"一字板(无换手)"

        # 4. 尾盘板排除：14:30后首次封板
        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        parsed = self._parse_time(limit_up_time)
        if parsed:
            hour, minute = parsed
            if hour > 14 or (hour == 14 and minute >= 30):
                return f"尾盘板({limit_up_time})"

        return None

    def _should_skip_stock(self, stock: pd.Series, sector_info: Dict, date_str: str) -> Optional[str]:
        """
        旧版兼容入口 — 内部转发到 Layer 0 硬性排除

        注意：独狼板检查已移至 Layer 4 评分阶段（板块效应作为加分项而非硬排除）；
              缩量板检查已移至 Layer 2 资金验证阶段。
        """
        return self._filter_layer_0_hard_exclusions(stock, sector_info, date_str)

    def _analyze_first_board(self, stock: pd.Series,
                            yesterday_zt: pd.DataFrame,
                            sector_info: Dict,
                            date_str: str,
                            is_hot_sector: bool = True,
                            is_dual_resonance: bool = False,
                            matched_sector_name: str = "",
                            matched_sector_type: str = "",
                            concept_hierarchy: Dict = None) -> Optional[TradeSignal]:
        """
        四层优先级过滤管线 — 分析单只股票是否为首板突破信号

        设计原则：按判断成本从低到高、按策略权重从核心到外围排列
        - 每一层不通过直接返回 None，避免后续无效计算
        - 板块效应从硬排除降级为 Layer 4 评分因子

        ┌─────────────────────────────────────────────────────────┐
        │ Layer 0: 硬性排除（零成本，仅用涨停池自带字段）          │
        │   ├── 非首板/非涨停                                     │
        │   ├── 一字板（无换手）                                  │
        │   └── 尾盘板（14:30后）                                 │
        ├─────────────────────────────────────────────────────────┤
        │ Layer 1: 技术结构突破（策略核心，需拉取日线数据）        │
        │   ├── 平台/前高/历史新高突破                             │
        │   ├── 筹码结构 → 确定动态阈值等级                        │
        │   └── MA5/MA10均线突破（确认低位启动）                   │
        ├─────────────────────────────────────────────────────────┤
        │ Layer 2: 资金验证（复用Layer 1日线数据）                 │
        │   ├── 量能检查（动态阈值）                               │
        │   ├── 缩量排除（量比<0.8）                               │
        │   └── 封单强度（动态阈值）                               │
        ├─────────────────────────────────────────────────────────┤
        │ Layer 3: 质量指标（硬性过滤）                            │
        │   ├── 涨停时间（动态阈值）                               │
        │   ├── 开板次数 ≤ 1                                      │
        │   ├── 流通市值 ≤ 100亿                                   │
        │   └── 5日涨幅 ≤ 15%（低位要求）                          │
        ├─────────────────────────────────────────────────────────┤
        │ Layer 4: 板块效应评分 + 信号生成（加分项，非硬排除）     │
        │   ├── 板块涨停家数 → 板块强度分                          │
        │   ├── 是否热点板块 → 热点分                              │
        │   ├── 概念+行业双共振 → 共振分                           │
        │   └── 综合置信度计算 + TradeSignal生成                   │
        └─────────────────────────────────────────────────────────┘
        """
        code = str(stock.get('代码', '')).zfill(6)
        name = stock.get('名称', '')
        sector_name = sector_info['sector_name']

        # ══════════════════════════════════════════════════════════
        # Layer 0: 硬性排除（零成本 — 仅用涨停池自带字段）
        # ══════════════════════════════════════════════════════════
        skip_reason = self._filter_layer_0_hard_exclusions(stock, sector_info, date_str)
        if skip_reason:
            logger.debug(f"[首板-L0] ✗ {name}({code}) 排除: {skip_reason}")
            return None

        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        break_count = stock.get('开板次数', 0)
        float_cap = float(stock.get('流通市值', 0)) / 100000000  # 亿
        if isinstance(stock.get('流通市值', 0), str):
            float_cap = float(str(stock.get('流通市值', '')).replace('亿', ''))

        logger.info(f"[首板-L0] ✓ {name}({code}) 通过硬性排除 "
                    f"(首板 {limit_up_time}封, 开板{break_count}次, 市值{float_cap:.1f}亿)")

        # ══════════════════════════════════════════════════════════
        # Layer 1: 技术结构突破（策略核心 — 拉取日线数据）
        # ══════════════════════════════════════════════════════════
        daily_data = self._get_daily_data(code, date_str)

        # 1a. 平台/前高突破
        is_breakout, platform_info = self._check_platform_breakout(code, date_str)
        if not is_breakout:
            logger.info(f"[首板-L1] ✗ {name}({code}) 过滤: 未突破平台/前高 "
                        f"({platform_info.get('reason', '')})")
            return None
        breakout_type = platform_info.get('breakout_type', '未知')

        # 1b. 筹码结构 → 确定动态阈值等级
        chip_ok, chip_info = self._check_chip_structure(code, date_str)
        if not chip_ok:
            logger.info(f"[首板-L1] ✗ {name}({code}) 过滤: 筹码结构不佳 "
                        f"(距前高{chip_info.get('distance_from_high', 'N/A')})")
            return None

        distance_str = chip_info.get('distance_from_high', '100%').replace('%', '')
        try:
            distance_from_high_pct = float(distance_str) / 100.0
        except Exception:
            distance_from_high_pct = 1.0

        thresholds = self._get_dynamic_thresholds(distance_from_high_pct, breakout_type)

        # 1c. MA5/MA10均线突破（确认低位启动 — 必须通过）
        ma_breakthrough = False
        if daily_data and all(k in daily_data for k in ['close', 'ma5', 'ma10']):
            if daily_data['close'] > daily_data['ma5'] and daily_data['close'] > daily_data['ma10']:
                ma_breakthrough = True

        if not ma_breakthrough:
            close_val = daily_data.get('close', 0) if daily_data else 0
            ma5_val = daily_data.get('ma5', 0) if daily_data else 0
            ma10_val = daily_data.get('ma10', 0) if daily_data else 0
            logger.info(f"[首板-L1] ✗ {name}({code}) 过滤: 未突破MA5/MA10均线 "
                        f"(close:{close_val:.2f}, ma5:{ma5_val:.2f}, ma10:{ma10_val:.2f})")
            return None

        logger.info(f"[首板-L1] ✓ {name}({code}) 通过技术结构 "
                    f"({breakout_type}, 距前高{distance_from_high_pct*100:.1f}%, "
                    f"MA突破, 动态阈值: 量>{thresholds['min_volume_ratio']} "
                    f"封单>{thresholds['min_seal_ratio']*100:.1f}%)")

        # ══════════════════════════════════════════════════════════
        # Layer 2: 资金验证（复用Layer 1日线数据）
        # ══════════════════════════════════════════════════════════
        volume_pass = True  # 量能是否优秀（≤3倍为佳）

        # 2a. 量能检查（动态阈值）
        if daily_data and 'volume_ratio' in daily_data:
            vol_ratio = daily_data['volume_ratio']
            min_vol_ratio = thresholds['min_volume_ratio']

            # 缩量板（量比<0.8）→ 排除
            if vol_ratio < 0.8:
                logger.info(f"[首板-L2] ✗ {name}({code}) 过滤: 缩量板(量比{vol_ratio:.2f}<0.8)")
                return None

            if vol_ratio < min_vol_ratio:
                logger.info(f"[首板-L2] ✗ {name}({code}) 过滤: 量能不足 "
                            f"(量比{vol_ratio:.2f}<{min_vol_ratio}, {thresholds['volume_desc']})")
                return None

            if vol_ratio > self.params['volume_abs_max']:
                logger.info(f"[首板-L2] ✗ {name}({code}) 过滤: 量能过大 "
                            f"(量比{vol_ratio:.2f}>{self.params['volume_abs_max']})")
                return None

            volume_pass = vol_ratio <= self.params['volume_max_ratio']
        else:
            logger.info(f"[首板-L2] ✗ {name}({code}) 过滤: 无日线数据，无法验证量能")
            return None

        # 2b. 封单强度（动态阈值）
        seal_amount = float(stock.get('封单额', 0) or stock.get('封板资金', 0) or stock.get('封单金额', 0))
        float_cap_raw = float(stock.get('流通市值', 0))
        free_float_cap = 0

        if hasattr(self.dm, 'get_stock_daily_basic'):
            try:
                daily_basic_data = self.dm.get_stock_daily_basic(code, date_str)
                if daily_basic_data:
                    free_share = daily_basic_data.get('free_share', 0)
                    close_price = daily_basic_data.get('close', 0)
                    free_float_cap = free_share * close_price * 10000
            except Exception as e:
                logger.debug(f"[{code}] 获取daily_basic失败: {e}")

        base_cap = free_float_cap if free_float_cap > 0 else float_cap_raw
        seal_ratio = seal_amount / base_cap if base_cap > 0 and seal_amount > 0 else 0

        if seal_ratio < thresholds['min_seal_ratio']:
            logger.info(f"[首板-L2] ✗ {name}({code}) 过滤: 封单强度不足 "
                        f"({seal_ratio*100:.2f}%<{thresholds['min_seal_ratio']*100:.1f}%)")
            return None

        logger.info(f"[首板-L2] ✓ {name}({code}) 通过资金验证 "
                    f"(量比{vol_ratio:.2f}≥{min_vol_ratio}, 封单{seal_ratio*100:.2f}%≥{thresholds['min_seal_ratio']*100:.1f}%, "
                    f"量能{'健康' if volume_pass else '偏大'})")

        # ══════════════════════════════════════════════════════════
        # Layer 3: 质量指标（硬性过滤）
        # ══════════════════════════════════════════════════════════
        layer3_fail = []

        # 3a. 涨停时间（动态阈值）
        if not self._is_valid_limit_time_dynamic(limit_up_time, thresholds['max_limit_up_time']):
            layer3_fail.append(f"涨停时间过晚({limit_up_time}>{thresholds['max_limit_up_time']})")

        # 3b. 开板次数 ≤ 1
        if break_count > self.params['max_break_count']:
            layer3_fail.append(f"开板过多({break_count}次)")

        # 3c. 流通市值 ≤ 100亿
        if float_cap > self.params['max_float_cap']:
            layer3_fail.append(f"市值过大({float_cap:.1f}亿)")

        # 3d. 5日涨幅 ≤ 15%（低位启动）
        if daily_data and 'rise_5d' in daily_data:
            if daily_data['rise_5d'] >= self.params['max_5d_rise']:
                layer3_fail.append(f"5日涨幅过高({daily_data['rise_5d']*100:.1f}%)")

        if layer3_fail:
            logger.info(f"[首板-L3] ✗ {name}({code}) 过滤: {'; '.join(layer3_fail)}")
            return None

        logger.info(f"[首板-L3] ✓ {name}({code}) 通过质量指标 "
                    f"(时间{limit_up_time}<{thresholds['max_limit_up_time']}, "
                    f"开板{break_count}次, 市值{float_cap:.1f}亿, "
                    f"5日涨幅{daily_data.get('rise_5d', 0)*100:.1f}%)")

        # ══════════════════════════════════════════════════════════
        # Layer 4: 板块效应评分 + 信号生成
        # ══════════════════════════════════════════════════════════
        sector_score = self._calculate_sector_effect_score(
            stock, sector_info, today_zt_for_sector=None,
            is_hot_sector=is_hot_sector, is_dual_resonance=is_dual_resonance
        )
        sector_limit_up_count = sector_info.get('stats', {}).get('涨停家数', 0)

        # 计算综合置信度
        confidence = 0.60  # 基础分（通过Layer 1/2/3已证明质量）

        # 技术结构分（最高+0.10）
        confidence += 0.05  # 突破平台/前高
        if breakout_type == '前高突破':
            confidence += 0.05  # 突破前高额外加成

        # 资金确认分（最高+0.10）
        confidence += min(seal_ratio * 2, 0.05)  # 封单强度
        if volume_pass and daily_data:
            excess_vol = daily_data.get('volume_ratio', 0) - thresholds['min_volume_ratio']
            confidence += min(max(excess_vol, 0) * 0.03, 0.05)  # 量能超预期加成

        # 质量分（最高+0.05）
        if break_count == 0:
            confidence += 0.03  # 零开板
        if limit_up_time and limit_up_time <= '09:40':
            confidence += 0.02  # 早盘秒封

        # 板块效应分（最高+0.10）
        confidence += sector_score

        confidence = min(confidence, 0.95)

        # 确定仓位权重
        if is_dual_resonance:
            position_size = "heavy"
        elif is_hot_sector:
            position_size = "medium"
        else:
            position_size = "light"

        # 获取概念梯队信息
        concept_info_list = []
        hierarchy_info = ""
        if concept_hierarchy:
            for concept_name, data in concept_hierarchy.items():
                stocks = data.get('stocks', [])
                stock_codes_in_concept = [str(s.get('code', '')).zfill(6) for s in stocks]
                if code in stock_codes_in_concept:
                    board_h = 0
                    for s in stocks:
                        if str(s.get('code', '')).zfill(6) == code:
                            board_h = s.get('board_height', 1)
                            break
                    concept_info_list.append({
                        'name': concept_name,
                        'board_height': board_h,
                        'max_height': data.get('max_board_height', 0),
                        'total_limit_up': data.get('total_limit_up', 0),
                    })

            concept_info_list.sort(key=lambda x: x['max_height'], reverse=True)
            top_concepts = concept_info_list[:3]
            if top_concepts:
                concept_parts = []
                for c in top_concepts:
                    if c['max_height'] > 0:
                        concept_parts.append(f"{c['name']}({c['max_height']}板)")
                    else:
                        concept_parts.append(c['name'])
                hierarchy_info = " | ".join(concept_parts)

        # 构建理由
        if is_dual_resonance:
            reason_parts = [f"首板突破+{sector_name}热点(双共振)"]
        elif is_hot_sector:
            reason_parts = [f"首板突破+{sector_name}热点"]
        else:
            reason_parts = [f"首板突破+{sector_name}"]

        if daily_data:
            if 'rise_5d' in daily_data:
                reason_parts.append(f"5日涨幅{daily_data['rise_5d']*100:.1f}%")
            reason_parts.append(f"量比{vol_ratio:.1f}")
        reason_parts.append(f"封单{seal_ratio*100:.1f}%")
        if ma_breakthrough:
            reason_parts.append("突破均线")
        if hierarchy_info:
            reason_parts.append(f"概念:{hierarchy_info}")

        # 关键指标
        key_metrics = {
            "涨停时间": limit_up_time,
            "封单额": f"{seal_amount/1e4:.0f}万",
            "封单强度": f"{seal_ratio*100:.1f}%",
            "所属行业": sector_name,
            "行业趋势": sector_info.get('trend_stage', ''),
            "距前高": chip_info.get('distance_from_high', 'N/A'),
            "量能说明": thresholds.get('volume_desc', '标准量能'),
            "板块效应": f"{sector_score*100:.0f}分(板块涨停{sector_limit_up_count}家)",
        }
        if concept_info_list:
            key_metrics["所属概念"] = ", ".join([c['name'] for c in concept_info_list[:3]])
            key_metrics["概念梯队"] = hierarchy_info or "无"
        if daily_data:
            if 'rise_5d' in daily_data:
                key_metrics["5日涨幅"] = f"{daily_data['rise_5d']*100:.1f}%"
            key_metrics["量比"] = f"{vol_ratio:.2f}"
            key_metrics["均线突破"] = "是"

        # 验证规则
        validation_rules = [
            "今日涨停(首板)",
            f"突破形态: {breakout_type}",
            f"筹码: 距前高{chip_info.get('distance_from_high', 'N/A')}",
            f"均线: MA5/MA10突破",
            f"涨停时间<{thresholds['max_limit_up_time']}",
            f"封单>{thresholds['min_seal_ratio']*100:.1f}%",
            f"量比>{thresholds['min_volume_ratio']}",
        ]
        if is_hot_sector:
            if matched_sector_type:
                validation_rules.append(f"热点{matched_sector_type}: {matched_sector_name}")
            else:
                validation_rules.append(f"热点板块: {sector_name}")
        else:
            validation_rules.append(f"行业: {sector_name}（非热点，板块效应{sector_score*100:.0f}分）")
        if daily_data and 'rise_5d' in daily_data:
            validation_rules.append(f"5日涨幅<{self.params['max_5d_rise']*100:.0f}%（低位）")

        # 买点策略
        if is_hot_sector and seal_ratio > 0.08 and break_count <= 1:
            buy_strategy = "主买点: 回封时扫板（热点+强封单）"
        elif is_hot_sector:
            buy_strategy = "次买点: 次日排板（热点板块中封单适中）"
        elif seal_ratio > 0.05:
            buy_strategy = "次买点: 次日竞价观察（非热点但封单尚可）"
        else:
            buy_strategy = "观察: 需板块效应确认后再介入"

        # 次日预期
        if is_hot_sector and sector_limit_up_count >= 5:
            next_day_expectation = "超预期: 一字板或T字板（板块发酵）"
        elif is_hot_sector:
            next_day_expectation = "正常: 高开3%-7%，竞价量能达昨日5%-10%"
        else:
            next_day_expectation = "低于预期: 低开或高开<3%（需竞价确认）"

        logger.info(f"[首板-L4] ✓ {name}({code}) 生成信号: "
                    f"置信度{confidence:.2f}, 仓位{position_size}, "
                    f"板块效应{sector_score*100:.0f}分, "
                    f"{'双共振' if is_dual_resonance else '热点' if is_hot_sector else '非热点'}")

        return TradeSignal(
            pattern_type=PatternType.HOTSPOT_FIRST_BOARD,
            stock_code=code,
            stock_name=name,
            trigger_time=limit_up_time,
            confidence=confidence,
            entry_price=stock.get('涨停价', stock.get('最新价', 0)),
            stop_loss=stock.get('涨停价', stock.get('最新价', 0)) * 0.93,
            take_profit=stock.get('涨停价', stock.get('最新价', 0)) * 1.10,
            position_size=position_size,
            reason="+".join(reason_parts),
            key_metrics=key_metrics,
            validation_rules=validation_rules,
            l2_industry=sector_name,
            buy_strategy=buy_strategy,
            next_day_expectation=next_day_expectation,
            is_dual_resonance=is_dual_resonance,
            is_hot_sector=is_hot_sector,
            matched_sector_name=matched_sector_name,
            matched_sector_type=matched_sector_type
        )

    def _get_daily_data(self, stock_code: str, date_str: str) -> Optional[Dict]:
        """
        获取股票的日线数据并计算相关指标

        Returns:
            Dict: 包含以下字段（如果可用）：
                - rise_5d: 近5日涨幅
                - volume_ratio: 量比
                - close: 收盘价
                - ma5: 5日均线
                - ma10: 10日均线
        """
        try:
            # 转换日期格式
            dt = datetime.strptime(date_str, "%Y%m%d")
            end_date = date_str
            start_date = (dt - timedelta(days=30)).strftime("%Y%m%d")  # 获取近30日数据用于计算均线

            # 尝试从data_manager获取日线数据
            if hasattr(self.dm, 'get_stock_daily'):
                # 添加后缀
                ts_code = self._add_suffix(stock_code)
                df = self.dm.get_stock_daily(ts_code, start_date, end_date)

                if not df.empty and len(df) >= 10:  # 至少需要10天数据计算MA10
                    # 按日期排序
                    df = df.sort_values('trade_date')

                    # 计算均线
                    df['ma5'] = df['close'].rolling(window=5).mean()
                    df['ma10'] = df['close'].rolling(window=10).mean()

                    # 获取最新数据
                    latest = df.iloc[-1]
                    prev_5d = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

                    # 计算5日涨幅
                    rise_5d = (latest['close'] - prev_5d['close']) / prev_5d['close'] if prev_5d['close'] > 0 else 0

                    # 计算量比 (当日成交量 / 前5日平均成交量)
                    if len(df) >= 6:
                        avg_volume_5d = df.iloc[-6:-1]['vol'].mean()
                        volume_ratio = latest['vol'] / avg_volume_5d if avg_volume_5d > 0 else 0
                    else:
                        volume_ratio = 0

                    result = {
                        'rise_5d': rise_5d,
                        'volume_ratio': volume_ratio,
                        'close': latest['close'],
                        'ma5': latest['ma5'],
                        'ma10': latest['ma10']
                    }
                    return result
        except Exception as e:
            logger.debug(f"获取日线数据失败 {stock_code}: {e}")

        return None

    def _add_suffix(self, stock_code: str) -> str:
        """为股票代码添加后缀，并补齐6位"""
        code = str(stock_code).strip()
        if '.' not in code:
            # 补齐6位
            code = code.zfill(6)
            # 根据代码规则判断交易所
            if code.startswith('6'):
                return f"{code}.SH"
            else:
                return f"{code}.SZ"
        return code

    def _check_platform_breakout(self, stock_code: str, date_str: str) -> Tuple[bool, Dict]:
        """
        检查平台突破形态（严格模式条件2）
        
        检测：
        1. 横盘整理 7-15天
        2. 突破平台或前高或历史新高
        
        Returns:
            (是否突破, 详细信息)
        """
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            # 获取近60天数据用于判断平台
            start_date = (dt - timedelta(days=60)).strftime("%Y%m%d")
            
            if hasattr(self.dm, 'get_stock_daily'):
                ts_code = self._add_suffix(stock_code)
                df = self.dm.get_stock_daily(ts_code, start_date, date_str)
                
                if df.empty or len(df) < 20:
                    return False, {"reason": "数据不足"}
                
                df = df.sort_values('trade_date').reset_index(drop=True)
                latest = df.iloc[-1]
                close_price = latest['close']
                
                # 获取近15天最高价和最低价
                recent_15d = df.iloc[-15:]
                recent_high = recent_15d['high'].max()
                recent_low = recent_15d['low'].min()
                
                # 获取近7-15天的数据（平台期）
                if len(df) >= 15:
                    platform_period = df.iloc[-15:-7]  # 7-15天前
                else:
                    platform_period = df.iloc[:-7] if len(df) > 7 else df
                
                if len(platform_period) < 5:
                    return False, {"reason": "平台期数据不足"}
                
                platform_high = platform_period['high'].max()
                platform_low = platform_period['low'].min()
                platform_range = (platform_high - platform_low) / platform_low if platform_low > 0 else 1
                
                # 判断横盘：平台期振幅<15%
                is_platform = platform_range < 0.15

                # 判断突破：当日收盘价突破平台高点
                is_breakout = close_price > platform_high * 0.98  # 允许2%误差

                # 判断前高突破
                prev_60d_high = df.iloc[:-1]['high'].max()  # 排除当日的60日高点
                is_break_prev_high = close_price > prev_60d_high * 0.98

                # 判断均线突破（5日、10日均线）
                df['ma5'] = df['close'].rolling(window=5).mean()
                df['ma10'] = df['close'].rolling(window=10).mean()
                prev_close = df.iloc[-2]['close'] if len(df) >= 2 else 0
                prev_ma5 = df.iloc[-2]['ma5'] if len(df) >= 2 and not pd.isna(df.iloc[-2]['ma5']) else 0
                prev_ma10 = df.iloc[-2]['ma10'] if len(df) >= 2 and not pd.isna(df.iloc[-2]['ma10']) else 0
                current_ma5 = df.iloc[-1]['ma5'] if not pd.isna(df.iloc[-1]['ma5']) else 0
                current_ma10 = df.iloc[-1]['ma10'] if not pd.isna(df.iloc[-1]['ma10']) else 0

                # 均线突破：当日突破5日或10日均线，且前一日在均线之下
                is_break_ma5 = (close_price > current_ma5 * 0.995) and (prev_close < prev_ma5 * 1.005)
                is_break_ma10 = (close_price > current_ma10 * 0.995) and (prev_close < prev_ma10 * 1.005)
                is_break_ma = is_break_ma5 or is_break_ma10

                # 综合判断
                result = {
                    "is_platform": is_platform,
                    "platform_range": f"{platform_range*100:.1f}%",
                    "is_breakout": is_breakout,
                    "is_break_prev_high": is_break_prev_high,
                    "is_break_ma": is_break_ma,
                    "is_break_ma5": is_break_ma5,
                    "is_break_ma10": is_break_ma10,
                    "platform_high": platform_high,
                    "close_price": close_price
                }

                # 突破条件：横盘+突破 或 突破前高 或 突破均线
                if (is_platform and is_breakout) or is_break_prev_high or is_break_ma:
                    if is_break_prev_high:
                        result["breakout_type"] = "前高突破"
                    elif is_platform and is_breakout:
                        result["breakout_type"] = "平台突破"
                    elif is_break_ma5:
                        result["breakout_type"] = "突破5日均线"
                    else:
                        result["breakout_type"] = "突破10日均线"
                    return True, result
                else:
                    result["reason"] = "未突破平台、前高或均线"
                    return False, result
                    
        except Exception as e:
            logger.debug(f"平台突破检测失败 {stock_code}: {e}")
        
        return False, {"reason": "检测异常"}

    def _check_chip_structure(self, stock_code: str, date_str: str) -> Tuple[bool, Dict]:
        """
        检查筹码结构（严格模式条件6）
        
        检测：距前高<20%（无巨量套牢盘）
        使用cyq_perf接口获取筹码数据
        
        Returns:
            (是否满足, 详细信息)
        """
        try:
            # 尝试使用cyq_perf接口获取筹码数据
            if hasattr(self.dm, 'get_chip_data'):
                chip_data = self.dm.get_chip_data(stock_code, date_str)
                if chip_data:
                    # 获取平均成本和胜率
                    avg_cost = chip_data.get('avg_cost', 0)
                    win_rate = chip_data.get('win_rate', 0)
                    
                    return True, {
                        "avg_cost": avg_cost,
                        "win_rate": win_rate,
                        "source": "cyq_perf"
                    }
            
            # 备选方案：使用日线数据估算
            dt = datetime.strptime(date_str, "%Y%m%d")
            start_date = (dt - timedelta(days=120)).strftime("%Y%m%d")
            
            if hasattr(self.dm, 'get_stock_daily'):
                ts_code = self._add_suffix(stock_code)
                df = self.dm.get_stock_daily(ts_code, start_date, date_str)
                
                if not df.empty and len(df) > 20:
                    df = df.sort_values('trade_date')
                    latest = df.iloc[-1]
                    close_price = latest['close']
                    
                    # 获取近60日高点
                    high_60d = df.iloc[-60:]['high'].max() if len(df) >= 60 else df['high'].max()
                    
                    # 计算距前高距离
                    distance_from_high = (high_60d - close_price) / high_60d if high_60d > 0 else 1
                    
                    # 距前高<20%视为无巨量套牢盘
                    no_trap = distance_from_high < self.params['max_distance_from_high']
                    
                    return no_trap, {
                        "distance_from_high": f"{distance_from_high*100:.1f}%",
                        "high_60d": high_60d,
                        "close_price": close_price,
                        "source": "daily_data"
                    }
                    
        except Exception as e:
            logger.debug(f"筹码结构检测失败 {stock_code}: {e}")
        
        # 默认通过
        return True, {"reason": "检测失败，默认通过"}

    def _parse_time(self, time_str: str) -> Optional[Tuple[int, int]]:
        """
        统一解析时间字符串为 (hour, minute)
        
        支持格式:
        - HHMMSS: 093606, 93606 (5位会被补0)
        - HHMM: 0936, 936 (3位会被补0)
        - HH:MM:SS: 09:36:06
        - HH:MM: 09:36
        
        Returns:
            (hour, minute) 或 None（解析失败）
        """
        if not time_str or time_str == '-':
            return None
        
        try:
            # 移除冒号并清理
            cleaned = str(time_str).strip().replace(':', '')
            
            # 处理不同长度
            if len(cleaned) == 6:  # HHMMSS, 如 093606
                hour = int(cleaned[:2])
                minute = int(cleaned[2:4])
                return (hour, minute)
            elif len(cleaned) == 5:  # HMMSS, 如 93606 -> 09:36:06
                hour = int(cleaned[0])
                minute = int(cleaned[1:3])
                return (hour, minute)
            elif len(cleaned) == 4:  # HHMM, 如 0936
                hour = int(cleaned[:2])
                minute = int(cleaned[2:4])
                return (hour, minute)
            elif len(cleaned) == 3:  # HMM, 如 936 -> 09:36
                hour = int(cleaned[0])
                minute = int(cleaned[1:3])
                return (hour, minute)
            else:
                logger.debug(f"无法解析的时间格式: {time_str} (长度{len(cleaned)})")
                return None
        except Exception as e:
            logger.debug(f"解析时间失败: {time_str}, {e}")
            return None

    def _is_valid_limit_time_dynamic(self, limit_up_time: str, max_time_str: str) -> bool:
        """
        动态检查涨停时间是否有效
        支持格式: HH:MM:SS, HH:MM, HHMMSS, HHMM

        Args:
            limit_up_time: 涨停时间
            max_time_str: 最晚涨停时间（如 "10:00", "10:30", "11:00"）
        """
        parsed = self._parse_time(limit_up_time)
        if not parsed:
            return False

        hour, minute = parsed
        max_hour = int(max_time_str[:2])
        max_minute = int(max_time_str[3:5])

        return hour < max_hour or (hour == max_hour and minute <= max_minute)

    def _get_date_offset(self, date_str: str, offset: int) -> str:
        """日期偏移计算"""
        dt = datetime.strptime(date_str, "%Y%m%d")
        target = dt + timedelta(days=offset)
        return target.strftime("%Y%m%d")