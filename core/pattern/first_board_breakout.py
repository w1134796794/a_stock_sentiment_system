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

        # 基础参数
        self.params = {
            "max_5d_rise": 0.15,           # 近5日涨幅<15%（低位要求）
            "max_float_cap": 100.0,        # 流通市值上限<100亿
            "hot_sector_heat_threshold": 5,   # 板块3日涨停数>=5（确认是热点）
            "fast_limit_max_time": "0940",    # 早盘秒封最长时间（9:40）
            "max_break_count": 1,          # 开板次数≤1
            "min_sector_limit_up": 2,      # 板块涨停≥2家（避免独狼板）
            "skip_tail_board_time": "14:30", # 尾盘板时间（14:30后放弃）
            "volume_max_ratio": 3.0,       # 量能上限<300%
            "volume_abs_max": 5.0,         # 绝对上限<5倍
            "platform_days_min": 7,        # 横盘≥7天
            "platform_days_max": 15,       # 横盘≤15天
            "max_distance_from_high": 0.25,  # 距前高<25%
        }

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

    def detect_first_board_by_sectors(self,
                                      today_zt: pd.DataFrame,
                                      history_pools: Dict[str, pd.DataFrame],
                                      date_str: str,
                                      hot_sectors: List[Dict] = None) -> List[TradeSignal]:
        """
        基于二级行业的首板突破检测

        流程：
        1. 获取热点二级行业（从参数传入或调用analyze_all_sectors_v2计算）
        2. 分析所有涨停股票中的首板（不限于热点板块）
        3. 结合技术指标进行过滤
        4. 属于热点板块的股票增加仓位权重

        Args:
            today_zt: 今日涨停池
            history_pools: 历史涨停池（用于板块分析）
            date_str: 日期字符串YYYYMMDD
            hot_sectors: 预计算的热点板块列表（避免重复计算）

        Returns:
            List[TradeSignal]: 符合条件的交易信号
        """
        signals = []

        if today_zt.empty:
            logger.warning("涨停池为空，无法检测首板突破")
            return signals

        # 1. 获取热点板块（同花顺概念/行业）
        if hot_sectors is None:
            # 如果没有传入热点板块，则自行计算
            hot_sectors = self._get_hot_sectors(today_zt, history_pools, date_str)
        else:
            logger.info(f"[首板突破] 使用传入的热点板块数据: {len(hot_sectors)}个")
        
        # 构建热点板块代码集合（用于快速匹配）
        hot_sector_codes = set()
        for hs in hot_sectors:
            hot_sector_codes.update(hs.get('member_codes', set()))
        
        logger.info(f"[首板突破] 识别到 {len(hot_sectors)} 个热点板块")
        logger.info(f"[首板突破] 热点板块包含 {len(hot_sector_codes)} 只成分股")
        
        # 2. 获取昨日涨停池（用于确认首板）
        yesterday_date = self._get_date_offset(date_str, -1)
        yesterday_zt = history_pools.get(yesterday_date, pd.DataFrame())

        # 3. 统计每个东财行业的涨停数量（用于独狼板检测）
        sector_limit_up_counts = {}
        if '所属行业' in today_zt.columns:
            sector_limit_up_counts = today_zt['所属行业'].value_counts().to_dict()
        elif 'L2_Industry' in today_zt.columns:
            sector_limit_up_counts = today_zt['L2_Industry'].value_counts().to_dict()
        
        logger.info(f"[首板突破] 东财行业涨停统计: {len(sector_limit_up_counts)}个行业有涨停")
        
        # 4. 分析所有涨停股票中的首板（不限于热点板块）
        total_analyzed = 0
        total_filtered = 0

        for _, stock in today_zt.iterrows():
            total_analyzed += 1

            # 获取股票代码（统一为6位数字）
            stock_code = str(stock.get('代码', '')).zfill(6)
            
            # 获取股票所属东财行业（用于独狼板检测）
            sector_name = stock.get('所属行业', '') or stock.get('L2_Industry', '')
            sector_limit_up_count = sector_limit_up_counts.get(sector_name, 0)

            # 判断该股票是否属于热点板块（通过代码匹配）
            matched_hot_sector = None
            if stock_code in hot_sector_codes:
                # 找到匹配的热点板块（取第一个匹配的）
                for hs in hot_sectors:
                    if stock_code in hs.get('member_codes', set()):
                        matched_hot_sector = hs
                        break
            
            if matched_hot_sector:
                # 属于热点板块，使用热点板块信息
                sector_info = {
                    'sector_name': matched_hot_sector['sector_name'],
                    'sector_type': matched_hot_sector.get('sector_type', '概念'),
                    'ts_code': matched_hot_sector.get('ts_code', ''),
                    'stats': {
                        '涨停家数': matched_hot_sector['stats'].get('涨停家数', 0),
                        '连板家数': matched_hot_sector['stats'].get('连板家数', 0),
                        '东财行业涨停数': sector_limit_up_count
                    },
                    'trend_stage': matched_hot_sector['trend_stage'],
                    'action': matched_hot_sector['action'],
                    'confidence': matched_hot_sector['confidence']
                }
                is_hot_sector = True
                logger.debug(f"[{stock_code}] 击中热点板块: {matched_hot_sector['sector_name']}")
            else:
                # 不属于热点板块，使用东财行业信息
                sector_info = {
                    'sector_name': sector_name or '未知行业',
                    'sector_type': '东财行业',
                    'ts_code': '',
                    'stats': {
                        '涨停家数': sector_limit_up_count,
                        '连板家数': 0,
                        '东财行业涨停数': sector_limit_up_count
                    },
                    'trend_stage': '非热点',
                    'action': '',
                    'confidence': 0.0
                }
                is_hot_sector = False

            # 分析首板
            signal = self._analyze_first_board(
                stock, yesterday_zt, sector_info, date_str, is_hot_sector
            )
            if signal:
                signals.append(signal)
            else:
                total_filtered += 1

        # 按置信度排序
        signals.sort(key=lambda x: x.confidence, reverse=True)
        
        # 统计热点板块信号数量（通过sector_type判断）
        hot_count = sum(1 for s in signals if s.l2_industry and s.l2_industry != '非热点')
        logger.info(f"[首板突破] 检测完成: 共 {len(signals)} 个信号 (击中热点{hot_count}只, 未击中{len(signals)-hot_count}只, 分析{total_analyzed}只, 过滤{total_filtered}只)")

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
            from core.data.data_manager import DataManager
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
                            # 提取成分股代码（统一为6位数字）
                            if 'con_code' in members_df.columns:
                                member_codes = set(
                                    str(c).split('.')[0].zfill(6) 
                                    for c in members_df['con_code'].values
                                )
                            elif 'code' in members_df.columns:
                                member_codes = set(
                                    str(c).split('.')[0].zfill(6) 
                                    for c in members_df['code'].values
                                )
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

    def _should_skip_stock(self, stock: pd.Series, sector_info: Dict, date_str: str) -> Optional[str]:
        """
        检查放弃信号（严格模式）
        
        放弃信号（满足任一即放弃）：
        1. 尾盘板（14:30后）
        2. 独狼板（无板块效应，板块涨停<3家）
        3. 一字板（无换手）
        4. 缩量板（<前5日均量）
        
        Returns:
            str: 放弃原因，None表示不放弃
        """
        code = str(stock.get('代码', '')).zfill(6)
        
        # 1. 检查尾盘板（14:30后涨停）
        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        parsed = self._parse_time(limit_up_time)
        if parsed:
            hour, minute = parsed
            # 14:30后涨停视为尾盘板
            if hour > 14 or (hour == 14 and minute >= 30):
                return f"尾盘板({limit_up_time})"
        
        # 2. 检查独狼板（板块效应不足）
        sector_stats = sector_info.get('stats', {})
        sector_limit_up_count = sector_stats.get('涨停家数', 0)
        if sector_limit_up_count < self.params['min_sector_limit_up']:
            return f"独狼板(板块涨停{sector_limit_up_count}家<{self.params['min_sector_limit_up']}家)"
        
        # 3. 检查一字板（无换手）
        # 通过开板次数和涨停类型判断
        break_count = stock.get('开板次数', 0)
        limit_type = stock.get('涨停类型', '')
        if break_count == 0 and ('一字' in str(limit_type) or limit_type == '1'):
            return "一字板(无换手)"
        
        # 4. 检查缩量板（获取日线数据对比）
        try:
            daily_data = self._get_daily_data(code, date_str)
            if daily_data and 'volume_ratio' in daily_data:
                # 量比<1表示缩量（低于前5日均量）
                if daily_data['volume_ratio'] < 1.0:
                    return f"缩量板(量比{daily_data['volume_ratio']:.2f}<1.0)"
        except Exception as e:
            logger.debug(f"[{code}] 检查缩量板失败: {e}")
        
        return None

    def _analyze_first_board(self, stock: pd.Series,
                            yesterday_zt: pd.DataFrame,
                            sector_info: Dict,
                            date_str: str,
                            is_hot_sector: bool = True) -> Optional[TradeSignal]:
        """
        分析单只股票是否为首板突破

        核心逻辑：根据套牢盘情况动态调整因子要求
        - 套牢盘多（距前高近）→ 量能、封单、涨停时间等要求更高
        - 套牢盘少（距前高远/突破前高）→ 相关要求降低

        Args:
            stock: 股票数据（来自涨停池）
            yesterday_zt: 昨日涨停池
            sector_info: 行业信息
            date_str: 日期字符串
            is_hot_sector: 是否属于热点板块（影响仓位权重）

        Returns:
            TradeSignal or None: 符合条件的交易信号
        """
        code = stock.get('代码', '')
        code = str(code).zfill(6)
        name = stock.get('名称', '')
        sector_name = sector_info['sector_name']

        logger.debug(f"[{code}] 开始分析首板 - 名称:{name}, 行业:{sector_name}, 热点:{is_hot_sector}")

        # ===== 步骤1: 基础过滤（必检项）=====
        skip_reason = self._should_skip_stock(stock, sector_info, date_str)
        if skip_reason:
            logger.debug(f"[{code}] 放弃信号: {skip_reason}")
            return None

        # 条件1: 今日涨停确认
        change = stock.get('涨跌幅', 0)
        if isinstance(change, str):
            change = float(change.replace('%', ''))
        if change < 9.5:
            logger.debug(f"[{code}] 过滤: 涨幅不足9.5% ({change:.2f}%)")
            return None
        logger.debug(f"[{code}] 通过: 今日涨停 ({change:.2f}%)")

        # 条件2: 首板确认（连板数=1）
        board_height = stock.get('连板数', 0)
        if board_height != 1:
            logger.debug(f"[{code}] 过滤: 非首板（连板数={board_height}）")
            return None
        logger.debug(f"[{code}] 通过: 首板确认（连板数=1）")

        # 获取基础数据
        float_cap = stock.get('流通市值', 0) / 100000000
        if isinstance(float_cap, str):
            float_cap = float(float_cap.replace('亿', ''))
        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        break_count = stock.get('开板次数', 0)

        # ===== 步骤2: 技术形态和筹码结构分析（用于动态阈值）=====
        # 检查平台/前高突破
        is_breakout, platform_info = self._check_platform_breakout(code, date_str)
        if not is_breakout:
            logger.debug(f"[{code}] 过滤: 未突破平台或前高 ({platform_info.get('reason', '')})")
            return None
        breakout_type = platform_info.get('breakout_type', '')
        logger.debug(f"[{code}] 技术形态: {breakout_type}")

        # 检查筹码结构（距前高距离）
        chip_ok, chip_info = self._check_chip_structure(code, date_str)
        if not chip_ok:
            logger.debug(f"[{code}] 过滤: 筹码结构不佳 ({chip_info.get('distance_from_high', 'N/A')}距前高)")
            return None

        # 解析距前高距离
        distance_str = chip_info.get('distance_from_high', '100%').replace('%', '')
        try:
            distance_from_high_pct = float(distance_str) / 100.0
        except:
            distance_from_high_pct = 1.0
        logger.debug(f"[{code}] 筹码结构: {chip_info.get('distance_from_high', 'N/A')}距前高")

        # 获取动态阈值（基于套牢盘情况）
        thresholds = self._get_dynamic_thresholds(distance_from_high_pct, breakout_type)
        logger.debug(f"[{code}] 动态阈值: 量能>{thresholds['min_volume_ratio']}, "
                    f"涨停时间<{thresholds['max_limit_up_time']}, "
                    f"封单>{thresholds['min_seal_ratio']*100:.1f}%")

        # ===== 步骤3: 应用动态阈值进行筛选 =====
        # 条件3: 流通市值 < 100亿
        if float_cap > self.params['max_float_cap']:
            logger.debug(f"[{code}] 过滤: 流通市值过大 ({float_cap:.2f}亿 > {self.params['max_float_cap']}亿)")
            return None
        logger.debug(f"[{code}] 通过: 流通市值 {float_cap:.2f}亿")

        # 条件4: 涨停时间检查（动态阈值）
        if not self._is_valid_limit_time_dynamic(limit_up_time, thresholds['max_limit_up_time']):
            logger.debug(f"[{code}] 过滤: 涨停时间过晚 ({limit_up_time} > {thresholds['max_limit_up_time']})")
            return None
        logger.debug(f"[{code}] 通过: 涨停时间 {limit_up_time} (要求<{thresholds['max_limit_up_time']})")

        # 条件5: 开板次数≤1
        if break_count > self.params['max_break_count']:
            logger.debug(f"[{code}] 过滤: 开板次数过多 ({break_count} > {self.params['max_break_count']})")
            return None
        logger.debug(f"[{code}] 通过: 开板次数 {break_count}次")

        # 条件4: 封单强度 > 2%
        # 尝试获取封单额（不同数据源可能使用不同列名）
        seal_amount = float(stock.get('封单额', 0) or stock.get('封板资金', 0) or stock.get('封单金额', 0))
        float_cap = float(stock.get('流通市值', 0))  # 流通市值单位是元

        # 从daily_basic接口获取自由流通股本计算自由流通市值
        free_float_cap = 0
        daily_basic_data = None
        if hasattr(self.dm, 'get_stock_daily_basic'):
            try:
                daily_basic_data = self.dm.get_stock_daily_basic(code, date_str)
                if daily_basic_data:
                    free_share = daily_basic_data.get('free_share', 0)  # 自由流通股本（万股）
                    close_price = daily_basic_data.get('close', 0)  # 收盘价
                    # 自由流通市值 = 自由流通股本 * 收盘价 * 10000（转换为元）
                    free_float_cap = free_share * close_price * 10000
                    logger.debug(f"[{code}] 从daily_basic获取: 自由流通股本={free_share:.2f}万股, "
                                f"收盘价={close_price:.2f}, 自由流通市值={free_float_cap/100000000:.2f}亿")
            except Exception as e:
                logger.debug(f"[{code}] 获取daily_basic数据失败: {e}")

        # 使用自由流通市值计算封单强度，如果没有则使用流通市值
        base_cap = free_float_cap if free_float_cap > 0 else float_cap

        # 统一单位计算封单强度（都转换为元）
        if base_cap > 0 and seal_amount > 0:
            seal_ratio = seal_amount / base_cap
        else:
            seal_ratio = 0

        # 打印详细的debug日志
        logger.debug(f"[{code}] 封单强度计算: 封单={seal_amount/10000:.0f}万, "
                    f"流通市值={float_cap/100000000:.2f}亿, "
                    f"自由流通市值={free_float_cap/100000000:.2f}亿, "
                    f"使用基数={base_cap/100000000:.2f}亿, "
                    f"封单强度={seal_ratio*100:.4f}%")

        # 条件6: 封单强度检查（使用动态阈值）
        if seal_ratio < thresholds['min_seal_ratio']:
            logger.debug(f"[{code}] 过滤: 封单强度不足 ({seal_ratio*100:.2f}% < {thresholds['min_seal_ratio']*100:.1f}%)")
            return None
        logger.debug(f"[{code}] 通过: 封单强度 {seal_ratio*100:.2f}% (要求>{thresholds['min_seal_ratio']*100:.1f}%)")

        # 获取日线数据进行进一步分析
        daily_data = self._get_daily_data(code, date_str)
        if daily_data:
            logger.debug(f"[{code}] 日线数据: 5日涨幅{daily_data.get('rise_5d', 0)*100:.2f}%, 量比{daily_data.get('volume_ratio', 0):.2f}")
        else:
            logger.debug(f"[{code}] 未获取到日线数据")

        # ===== 步骤4: 量能检查（使用动态阈值）=====
        volume_pass = False
        if daily_data and 'volume_ratio' in daily_data:
            vol_ratio = daily_data['volume_ratio']
            min_vol_ratio = thresholds['min_volume_ratio']

            if vol_ratio < min_vol_ratio:
                logger.debug(f"[{code}] 过滤: 量能不足 ({vol_ratio:.2f} < {min_vol_ratio}), {thresholds['volume_desc']}")
                return None
            if vol_ratio > self.params['volume_abs_max']:
                logger.debug(f"[{code}] 过滤: 量能过大 ({vol_ratio:.2f} > {self.params['volume_abs_max']})")
                return None
            volume_pass = vol_ratio <= self.params['volume_max_ratio']
            logger.debug(f"[{code}] 通过: 量比{vol_ratio:.2f} (最低{min_vol_ratio*100:.0f}%), {thresholds['volume_desc']}, 理想<3倍:{volume_pass}")

        # 涨幅检查
        if daily_data and 'rise_5d' in daily_data:
            if daily_data['rise_5d'] >= self.params['max_5d_rise']:
                logger.debug(f"[{code}] 过滤: 5日涨幅过高 ({daily_data['rise_5d']*100:.2f}% >= {self.params['max_5d_rise']*100:.0f}%)")
                return None
            logger.debug(f"[{code}] 通过: 5日涨幅 {daily_data['rise_5d']*100:.2f}%")

        # 条件7: 当天日线穿过5日、10日线
        ma_breakthrough = False
        if daily_data and all(k in daily_data for k in ['close', 'ma5', 'ma10']):
            # 当天收盘价上穿5日线和10日线
            if daily_data['close'] > daily_data['ma5'] and daily_data['close'] > daily_data['ma10']:
                ma_breakthrough = True
                logger.debug(f"[{code}] 通过: 突破MA5/MA10均线 (close:{daily_data['close']:.2f}, ma5:{daily_data['ma5']:.2f}, ma10:{daily_data['ma10']:.2f})")
            else:
                logger.debug(f"[{code}] 未突破均线 (close:{daily_data['close']:.2f}, ma5:{daily_data['ma5']:.2f}, ma10:{daily_data['ma10']:.2f})")

        # 计算置信度
        confidence = 0.70  # 基础置信度
        confidence += min(seal_ratio * 2, 0.15)  # 封单强度加成
        if daily_data and 'volume_ratio' in daily_data:
            # 使用动态阈值计算量比加成（超过最低要求越多，加成越高）
            vol_ratio = daily_data['volume_ratio']
            min_vol_ratio = thresholds['min_volume_ratio']
            confidence += min((vol_ratio - min_vol_ratio) * 0.05, 0.10)  # 量比加成
        if ma_breakthrough:
            confidence += 0.05  # 均线突破加成
        if is_hot_sector:
            confidence += 0.05  # 热点板块加成
        confidence = min(confidence, 0.95)

        # 确定仓位权重：热点板块 -> medium, 非热点 -> light
        position_size = "medium" if is_hot_sector else "light"

        logger.debug(f"[{code}] 生成信号: 置信度{confidence:.2f}, 仓位{position_size}, 热点{is_hot_sector}")

        # 构建理由
        if is_hot_sector:
            reason_parts = [f"首板突破+{sector_name}热点"]
        else:
            reason_parts = [f"首板突破+{sector_name}"]
        if daily_data:
            if 'rise_5d' in daily_data:
                reason_parts.append(f"5日涨幅{daily_data['rise_5d']*100:.1f}%")
            if 'volume_ratio' in daily_data:
                reason_parts.append(f"量比{daily_data['volume_ratio']:.1f}")
        reason_parts.append(f"封单强度{seal_ratio*100:.1f}%")
        if ma_breakthrough:
            reason_parts.append("突破均线")

        # 构建关键指标
        key_metrics = {
            "涨停时间": limit_up_time,
            "封单额": f"{seal_amount/1e4:.0f}万",
            "封单强度": f"{seal_ratio*100:.1f}%",
            "所属行业": sector_name,
            "行业趋势": sector_info['trend_stage']
        }
        if daily_data:
            if 'rise_5d' in daily_data:
                key_metrics["5日涨幅"] = f"{daily_data['rise_5d']*100:.1f}%"
            if 'volume_ratio' in daily_data:
                key_metrics["量比"] = f"{daily_data['volume_ratio']:.1f}"
            if ma_breakthrough:
                key_metrics["均线突破"] = "是"

        # 构建验证规则
        validation_rules = [
            "今日涨停",
            "昨日未涨停（首板）",
            f"涨停时间<{thresholds['max_limit_up_time']}（非偷袭）",
            f"封单强度>{thresholds['min_seal_ratio']*100:.1f}%",
            f"技术形态: {breakout_type}",
            f"筹码结构: 距前高{chip_info.get('distance_from_high', 'N/A')}"
        ]
        if is_hot_sector:
            validation_rules.append(f"属于热点行业: {sector_name}")
        else:
            validation_rules.append(f"行业: {sector_name}（非热点）")
        if daily_data and 'rise_5d' in daily_data:
            validation_rules.append(f"近5日涨幅<{self.params['max_5d_rise']*100:.0f}%（低位）")
        if daily_data and 'volume_ratio' in daily_data:
            validation_rules.append(f"量比>{thresholds['min_volume_ratio']}（资金介入）")

        # 确定买点策略（基于动态阈值和板块强度）
        if is_hot_sector and seal_ratio > 0.08 and break_count <= 1:
            buy_strategy = "主买点: 回封时扫板（防止假突破）"
        else:
            buy_strategy = "次买点: 次日排板（板块龙头已确定）"

        # 次日预期
        sector_limit_up_count = sector_info.get('stats', {}).get('涨停家数', 0)
        if is_hot_sector and sector_limit_up_count >= 5:
            next_day_expectation = "超预期: 一字板或T字板（板块发酵）"
        elif is_hot_sector:
            next_day_expectation = "正常: 高开3%-7%，竞价量能达昨日5%-10%"
        else:
            next_day_expectation = "低于预期: 低开或高开<3%（考虑竞价卖出）"

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
            next_day_expectation=next_day_expectation
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
