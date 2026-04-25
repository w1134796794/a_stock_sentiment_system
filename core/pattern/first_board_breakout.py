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
from core.analysis.sector_rotation_tracker import SectorRotationTracker

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
    def __init__(self, data_manager, sector_engine=None, mapper=None, mode="loose"):
        """
        data_manager: 数据管理器（DataManager）
        sector_engine: 板块热度引擎（可选）
        mapper: 行业映射器（可选）
        mode: 策略模式 - "strict"(严格模式) | "loose"(宽松模式)
        """
        self.dm = data_manager
        self.sector_engine = sector_engine
        self.mapper = mapper
        self.mode = mode

        # 基础参数（新旧逻辑共用）
        self.params = {
            "max_5d_rise": 0.15,           # 近5日涨幅<15%（低位要求）
            "min_volume_ratio": 1.8,       # 量比>1.8（资金突然介入）
            "max_limit_up_time": "14:30",  # 最晚14:30前涨停（拒绝偷袭板）
            "hot_sector_heat_threshold": 5,   # 板块3日涨停数>=5（确认是热点）
            "fast_limit_max_time": "0940",    # 早盘秒封最长时间（9:40）
            "max_float_cap": 100.0         # 流通市值<100亿（小盘偏好）
        }

        # 严格模式参数（新逻辑）
        self.strict_params = {
            "max_float_cap": 50.0,           # 流通市值<50亿（最佳）
            "max_float_cap_loose": 100.0,    # 宽松上限<100亿
            "max_limit_up_time": "10:30",    # 最晚10:30前涨停
            "max_break_count": 1,            # 开板次数≤1
            "min_sector_limit_up": 3,        # 板块涨停≥3家
            "volume_min_ratio": 1.5,         # 标准量能>150%
            "volume_min_ratio_relaxed": 1.2, # 套牢盘少时量能>120%（距前高<10%）
            "volume_min_ratio_break_high": 0.8,  # 突破前高时量能>80%（所有套牢盘已解放）
            "volume_max_ratio": 3.0,         # 量能<300%
            "volume_abs_max": 5.0,           # 绝对上限<5倍
            "platform_days_min": 7,          # 横盘≥7天
            "platform_days_max": 15,         # 横盘≤15天
            "max_distance_from_high": 0.20,  # 距前高<20%
            "relaxed_distance_threshold": 0.10,  # 距前高<10%视为套牢盘很少，可降低量能要求
            "skip_tail_board_time": "14:30", # 尾盘板时间（14:30后放弃）
            "min_seal_ratio": 0.05,          # 封单强度>5%
        }

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

        # 1. 获取热点二级行业
        if hot_sectors is None:
            # 如果没有传入热点板块，则自行计算
            hot_sectors = self._get_hot_sectors(today_zt, history_pools, date_str)
        else:
            logger.info(f"[首板突破] 使用传入的热点板块数据: {len(hot_sectors)}个")
        
        hot_sector_names = {s['sector_name'] for s in hot_sectors}
        logger.info(f"[首板突破] 识别到 {len(hot_sectors)} 个热点二级行业")
        logger.info(f"[首板突破] 热点行业列表: {list(hot_sector_names)[:10]}")  # 显示前10个
        
        # 调试：显示涨停池中的行业分布
        if '所属行业' in today_zt.columns:
            pool_sectors = today_zt['所属行业'].unique()
        elif 'L2_Industry' in today_zt.columns:
            pool_sectors = today_zt['L2_Industry'].unique()
        else:
            pool_sectors = []
        logger.info(f"[首板突破] 涨停池中的行业数: {len(pool_sectors)}个")
        logger.info(f"[首板突破] 涨停池行业样例: {list(pool_sectors)[:10]}")

        # 2. 获取昨日涨停池（用于确认首板）
        yesterday_date = self._get_date_offset(date_str, -1)
        yesterday_zt = history_pools.get(yesterday_date, pd.DataFrame())

        # 3. 统计每个行业的涨停数量（用于独狼板检测）
        sector_limit_up_counts = {}
        if '所属行业' in today_zt.columns:
            sector_limit_up_counts = today_zt['所属行业'].value_counts().to_dict()
        elif 'L2_Industry' in today_zt.columns:
            sector_limit_up_counts = today_zt['L2_Industry'].value_counts().to_dict()
        
        logger.info(f"[首板突破] 行业涨停统计: {len(sector_limit_up_counts)}个行业有涨停")
        
        # 4. 分析所有涨停股票中的首板（不限于热点板块）
        total_analyzed = 0
        total_filtered = 0

        for _, stock in today_zt.iterrows():
            total_analyzed += 1

            # 获取股票所属行业
            sector_name = stock.get('所属行业', '') or stock.get('L2_Industry', '')
            
            # 获取该行业的涨停数量
            sector_limit_up_count = sector_limit_up_counts.get(sector_name, 0)

            # 判断该股票是否属于热点板块
            if sector_name in hot_sector_names:
                # 属于热点板块，获取板块信息
                sector_info = next((s for s in hot_sectors if s['sector_name'] == sector_name), None)
                # 更新涨停数量
                if sector_info:
                    sector_info['stats']['涨停家数'] = sector_limit_up_count
                is_hot_sector = True
            else:
                # 不属于热点板块，创建板块信息（包含涨停数量）
                sector_info = {
                    'sector_name': sector_name or '未知行业',
                    'stats': {'涨停家数': sector_limit_up_count},
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
        hot_count = sum(1 for s in signals if s.l2_industry in hot_sector_names)
        logger.info(f"首板突破检测完成: 共 {len(signals)} 个信号 (热点板块{hot_count}只, 非热点{len(signals)-hot_count}只, 分析{total_analyzed}只, 过滤{total_filtered}只)")

        return signals

    def _get_hot_sectors(self, today_zt: pd.DataFrame,
                         history_pools: Dict[str, pd.DataFrame],
                         date_str: str) -> List[Dict]:
        """
        获取热点二级行业列表

        Returns:
            List[Dict]: 热点行业信息列表，每个包含：
                - sector_name: 行业名称
                - stats: 统计数据
                - trend_stage: 趋势阶段
        """
        hot_sectors = []

        # 使用sector_rotation_tracker分析板块热度
        try:
            from core.data.data_manager import DataManager
            from config.settings import TUSHARE_TOKEN, CACHE_DIR
            
            dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
            tracker = SectorRotationTracker(dm)
            
            # 获取最近交易日的板块持续性分析
            from datetime import datetime
            trade_date = datetime.now().strftime("%Y%m%d")
            sector_df = tracker.analyze_sectors_persistence(trade_date, top_n=10)

            if sector_df.empty:
                logger.warning("板块分析结果为空")
                return hot_sectors

            # 筛选热点行业（加速期、高潮期、萌芽期）
            for _, row in sector_df.iterrows():
                stage = row.get('所处阶段', '')
                if stage in ['加速期', '高潮期', '萌芽期']:
                    sector_name = row.get('板块名称', '')
                    if sector_name:
                        hot_sectors.append({
                            'sector_name': sector_name,
                            'stats': {'涨停家数': row.get('涨停家数', 0)},
                            'trend_stage': stage,
                            'action': row.get('操作建议', ''),
                            'confidence': row.get('持续性评分', 50) / 100
                        })

            logger.info(f"从板块分析中识别到 {len(hot_sectors)} 个热点行业")

        except Exception as e:
            logger.error(f"获取热点行业失败: {e}")

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
        if sector_limit_up_count < self.strict_params['min_sector_limit_up']:
            return f"独狼板(板块涨停{sector_limit_up_count}家<{self.strict_params['min_sector_limit_up']}家)"
        
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

        筛选条件：
        1. 今日涨停 + 昨日未涨停（首板确认）
        2. 流通市值 < 100亿（小盘偏好）
        3. 涨停时间 < 14:30（拒绝偷袭板）
        4. 封单强度 > 5%
        5. 近5日涨幅 < 15%（低位要求）- 需要日线数据
        6. 量比 > 1.8（资金突然介入）- 需要日线数据
        7. 当天日线穿过5日、10日线 - 需要日线数据

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
        # 确保代码是6位字符串
        code = str(code).zfill(6)
        name = stock.get('名称', '')
        sector_name = sector_info['sector_name']

        logger.debug(f"[{code}] 开始分析首板 - 名称:{name}, 行业:{sector_name}, 热点:{is_hot_sector}")

        # ===== 严格模式：先检查放弃信号 =====
        if self.mode == "strict":
            skip_reason = self._should_skip_stock(stock, sector_info, date_str)
            if skip_reason:
                logger.debug(f"[{code}] 放弃信号: {skip_reason}")
                return None

        # 条件1: 今日涨停（涨停池中的股票默认已涨停）
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
        
        # ===== 严格模式：6大核心条件 =====
        if self.mode == "strict":
            # 严格条件1: 题材强度（板块涨停≥3家）- 已在放弃信号中检查
            sector_stats = sector_info.get('stats', {})
            sector_limit_up_count = sector_stats.get('涨停家数', 0)
            logger.debug(f"[{code}] 严格条件1-题材强度: 板块涨停{sector_limit_up_count}家")
            
            # 严格条件2: 流通市值 < 50亿（最佳），<100亿可接受
            if float_cap > self.strict_params['max_float_cap_loose']:
                logger.debug(f"[{code}] 过滤: 流通市值过大 ({float_cap:.2f}亿 > {self.strict_params['max_float_cap_loose']}亿)")
                return None
            float_cap_pass = float_cap <= self.strict_params['max_float_cap']
            logger.debug(f"[{code}] 严格条件2-市值: {float_cap:.2f}亿 (理想<50亿:{float_cap_pass})")
            
            # 严格条件3: 涨停时间 < 10:30，开板次数≤1
            if not self._is_valid_limit_time_strict(limit_up_time):
                logger.debug(f"[{code}] 过滤: 涨停时间过晚 ({limit_up_time} > {self.strict_params['max_limit_up_time']})")
                return None
            if break_count > self.strict_params['max_break_count']:
                logger.debug(f"[{code}] 过滤: 开板次数过多 ({break_count} > {self.strict_params['max_break_count']})")
                return None
            logger.debug(f"[{code}] 严格条件3-分时质量: 涨停时间{limit_up_time}, 开板{break_count}次")
        else:
            # 宽松模式：使用旧逻辑
            # 条件3: 流通市值 < 100亿
            if float_cap > self.params['max_float_cap']:
                logger.debug(f"[{code}] 过滤: 流通市值过大 ({float_cap:.2f}亿 > {self.params['max_float_cap']}亿)")
                return None
            logger.debug(f"[{code}] 通过: 流通市值 {float_cap:.2f}亿")

            # 条件4: 涨停时间 < 14:30
            if not self._is_valid_limit_time(limit_up_time):
                logger.debug(f"[{code}] 过滤: 涨停时间过晚 ({limit_up_time} > {self.params['max_limit_up_time']})")
                return None
            logger.debug(f"[{code}] 通过: 涨停时间 {limit_up_time}")

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

        # 封单强度检查
        if self.mode == "strict":
            # 严格模式：封单强度>5%
            if seal_ratio < self.strict_params['min_seal_ratio']:
                logger.debug(f"[{code}] 过滤: 封单强度不足 ({seal_ratio*100:.2f}% < {self.strict_params['min_seal_ratio']*100:.0f}%)")
                return None
            logger.debug(f"[{code}] 严格条件4-封单强度: {seal_ratio*100:.2f}%")
        else:
            # 宽松模式：封单强度>2%
            if seal_ratio < 0.02:
                logger.debug(f"[{code}] 过滤: 封单强度不足 ({seal_ratio*100:.4f}% < 2%)")
                return None
            logger.debug(f"[{code}] 通过: 封单强度 {seal_ratio*100:.2f}%")

        # 获取日线数据进行进一步分析
        daily_data = self._get_daily_data(code, date_str)
        if daily_data:
            logger.debug(f"[{code}] 日线数据: 5日涨幅{daily_data.get('rise_5d', 0)*100:.2f}%, 量比{daily_data.get('volume_ratio', 0):.2f}")
        else:
            logger.debug(f"[{code}] 未获取到日线数据")

        # ===== 严格模式：先检查筹码结构（用于动态调整量能要求）=====
        platform_breakout_info = {}
        chip_structure_info = {}
        distance_from_high_pct = 1.0  # 默认距前高100%（很远）
        
        if self.mode == "strict":
            # 严格条件2: 技术形态（平台突破/前高突破）
            is_breakout, platform_info = self._check_platform_breakout(code, date_str)
            platform_breakout_info = platform_info
            if not is_breakout:
                logger.debug(f"[{code}] 过滤: 未突破平台或前高 ({platform_info.get('reason', '')})")
                return None
            logger.debug(f"[{code}] 严格条件6-技术形态: {platform_info.get('breakout_type', '突破')}")
            
            # 严格条件6: 筹码结构（距前高<20%）
            chip_ok, chip_info = self._check_chip_structure(code, date_str)
            chip_structure_info = chip_info
            if not chip_ok:
                logger.debug(f"[{code}] 过滤: 筹码结构不佳 ({chip_info.get('distance_from_high', 'N/A')}距前高)")
                return None
            logger.debug(f"[{code}] 严格条件6-筹码结构: {chip_info.get('distance_from_high', 'N/A')}距前高")
            
            # 解析距前高距离百分比
            distance_str = chip_info.get('distance_from_high', '100%').replace('%', '')
            try:
                distance_from_high_pct = float(distance_str) / 100.0
            except:
                distance_from_high_pct = 1.0

        # 量能检查（根据技术形态和距前高距离动态调整）
        volume_pass = False
        if daily_data and 'volume_ratio' in daily_data:
            vol_ratio = daily_data['volume_ratio']
            if self.mode == "strict":
                # 获取突破类型
                breakout_type = platform_breakout_info.get('breakout_type', '')
                
                # 根据突破类型和距前高距离动态调整量能要求
                # 1. 突破前高：所有套牢盘已解放，量能要求降至100%
                # 2. 距前高<10%：套牢盘很少，量能要求降至120%
                # 3. 距前高10%-20%：标准量能要求150%
                if breakout_type == "前高突破":
                    min_vol_ratio = self.strict_params['volume_min_ratio_break_high']  # 1.0
                    vol_reason = f"突破前高，所有套牢盘已解放，量能要求放宽至100%"
                elif distance_from_high_pct < self.strict_params['relaxed_distance_threshold']:
                    min_vol_ratio = self.strict_params['volume_min_ratio_relaxed']  # 1.2
                    vol_reason = f"距前高{distance_from_high_pct*100:.1f}%<10%，量能要求放宽至120%"
                else:
                    min_vol_ratio = self.strict_params['volume_min_ratio']  # 1.5
                    vol_reason = f"距前高{distance_from_high_pct*100:.1f}%>=10%，标准量能要求150%"
                
                if vol_ratio < min_vol_ratio:
                    logger.debug(f"[{code}] 过滤: 量能不足 ({vol_ratio:.2f} < {min_vol_ratio}), {vol_reason}")
                    return None
                if vol_ratio > self.strict_params['volume_abs_max']:
                    logger.debug(f"[{code}] 过滤: 量能过大 ({vol_ratio:.2f} > {self.strict_params['volume_abs_max']})")
                    return None
                volume_pass = vol_ratio <= self.strict_params['volume_max_ratio']
                logger.debug(f"[{code}] 严格条件5-量能: 量比{vol_ratio:.2f} (最低{min_vol_ratio*100:.0f}%), {vol_reason}, 理想<3倍:{volume_pass}")
            else:
                # 宽松模式：量比>1.8
                if vol_ratio < self.params['min_volume_ratio']:
                    logger.debug(f"[{code}] 过滤: 量比过低 ({vol_ratio:.2f} < {self.params['min_volume_ratio']})")
                    return None
                logger.debug(f"[{code}] 通过: 量比 {vol_ratio:.2f}")

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
            confidence += min((daily_data['volume_ratio'] - self.params['min_volume_ratio']) * 0.02, 0.10)  # 量比加成
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
            f"涨停时间<{self.params['max_limit_up_time']}（非偷袭）",
            "封单强度>5%",
        ]
        if is_hot_sector:
            validation_rules.append(f"属于热点行业: {sector_name}")
        else:
            validation_rules.append(f"行业: {sector_name}（非热点）")
        if daily_data and 'rise_5d' in daily_data:
            validation_rules.append(f"近5日涨幅<{self.params['max_5d_rise']*100:.0f}%（低位）")
        if daily_data and 'volume_ratio' in daily_data:
            validation_rules.append(f"量比>{self.params['min_volume_ratio']}（资金介入）")
        
        # 严格模式：添加额外验证规则
        if self.mode == "strict":
            if platform_breakout_info.get('breakout_type'):
                validation_rules.append(f"技术形态: {platform_breakout_info['breakout_type']}")
            if chip_structure_info.get('distance_from_high'):
                validation_rules.append(f"筹码结构: 距前高{chip_structure_info['distance_from_high']}")

        # 确定买点策略
        if self.mode == "strict":
            # 严格模式：根据板块强度和封单质量确定买点
            if is_hot_sector and seal_ratio > 0.08 and break_count <= 1:
                buy_strategy = "主买点: 回封时扫板（防止假突破）"
            else:
                buy_strategy = "次买点: 次日排板（板块龙头已确定）"
            
            # 次日预期
            if is_hot_sector and sector_limit_up_count >= 5:
                next_day_expectation = "超预期: 一字板或T字板（板块发酵）"
            elif is_hot_sector:
                next_day_expectation = "正常: 高开3%-7%，竞价量能达昨日5%-10%"
            else:
                next_day_expectation = "低于预期: 低开或高开<3%（考虑竞价卖出）"
        else:
            buy_strategy = "主买点: 回封时扫板"
            next_day_expectation = "正常预期"

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
                
                # 综合判断
                result = {
                    "is_platform": is_platform,
                    "platform_range": f"{platform_range*100:.1f}%",
                    "is_breakout": is_breakout,
                    "is_break_prev_high": is_break_prev_high,
                    "platform_high": platform_high,
                    "close_price": close_price
                }
                
                # 突破条件：横盘+突破 或 突破前高
                if (is_platform and is_breakout) or is_break_prev_high:
                    result["breakout_type"] = "平台突破" if (is_platform and is_breakout) else "前高突破"
                    return True, result
                else:
                    result["reason"] = "未突破平台或前高"
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
                    no_trap = distance_from_high < self.strict_params['max_distance_from_high']
                    
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

    def _is_valid_limit_time(self, limit_up_time: str) -> bool:
        """
        检查涨停时间是否有效（非尾盘偷袭）
        支持格式: HH:MM:SS, HH:MM, HHMMSS, HHMM
        """
        parsed = self._parse_time(limit_up_time)
        if not parsed:
            return False
        
        hour, minute = parsed
        max_hour = int(self.params['max_limit_up_time'][:2])
        max_minute = int(self.params['max_limit_up_time'][3:5])
        
        return hour < max_hour or (hour == max_hour and minute <= max_minute)

    def _is_valid_limit_time_strict(self, limit_up_time: str) -> bool:
        """
        严格模式：检查涨停时间是否有效（10:30前）
        支持格式: HH:MM:SS, HH:MM, HHMMSS, HHMM
        """
        parsed = self._parse_time(limit_up_time)
        if not parsed:
            return False
        
        hour, minute = parsed
        max_time = self.strict_params['max_limit_up_time']  # "10:30"
        max_hour = int(max_time[:2])
        max_minute = int(max_time[3:5])
        
        return hour < max_hour or (hour == max_hour and minute <= max_minute)

    def _get_date_offset(self, date_str: str, offset: int) -> str:
        """日期偏移计算"""
        dt = datetime.strptime(date_str, "%Y%m%d")
        target = dt + timedelta(days=offset)
        return target.strftime("%Y%m%d")
