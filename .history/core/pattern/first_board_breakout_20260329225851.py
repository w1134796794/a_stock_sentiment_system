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
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.sector_heat_v2 import SectorHeatCalculatorV2, TrendStage

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


class HotspotFirstBoardStrategy:
    def __init__(self, data_manager, sector_engine=None, mapper=None):
        """
        data_manager: 数据管理器（DataManager）
        sector_engine: 板块热度引擎（可选）
        mapper: 行业映射器（可选）
        """
        self.dm = data_manager
        self.sector_engine = sector_engine
        self.mapper = mapper

        # 首板突破专用参数
        self.params = {
            "max_5d_rise": 0.15,           # 近5日涨幅<15%（低位要求）
            "min_volume_ratio": 3.0,       # 量比>3（资金突然介入）
            "max_limit_up_time": "14:30",  # 最晚14:30前涨停（拒绝偷袭板）
            "hot_sector_heat_threshold": 5,   # 板块3日涨停数>=5（确认是热点）
            "fast_limit_max_time": "0940"     # 早盘秒封最长时间（9:40）
        }

    def detect_first_board_by_sectors(self,
                                      today_zt: pd.DataFrame,
                                      history_pools: Dict[str, pd.DataFrame],
                                      date_str: str) -> List[TradeSignal]:
        """
        基于二级行业的首板突破检测

        流程：
        1. 调用analyze_all_sectors_v2获取热点二级行业
        2. 筛选每个热点行业中的首板股票
        3. 结合技术指标进行过滤

        Args:
            today_zt: 今日涨停池
            history_pools: 历史涨停池（用于板块分析）
            date_str: 日期字符串YYYYMMDD

        Returns:
            List[TradeSignal]: 符合条件的交易信号
        """
        signals = []

        logger.debug(f"[首板突破] 开始检测，今日涨停池数量: {len(today_zt)}, 历史池数量: {len(history_pools)}, 日期: {date_str}")

        if today_zt.empty:
            logger.warning("涨停池为空，无法检测首板突破")
            return signals

        # 打印涨停池列名和数据样例
        logger.debug(f"[首板突破] 涨停池列名: {list(today_zt.columns)}")
        if not today_zt.empty:
            logger.debug(f"[首板突破] 涨停池首行数据: {today_zt.iloc[0].to_dict()}")

        # 1. 获取热点二级行业（使用sector_heat_v2分析）
        hot_sectors = self._get_hot_sectors(today_zt, history_pools, date_str)
        if not hot_sectors:
            logger.warning("未识别到热点二级行业")
            return signals

        logger.info(f"识别到 {len(hot_sectors)} 个热点二级行业，开始分析首板...")
        for i, sector in enumerate(hot_sectors):  # 打印所有热点行业
            logger.debug(f"  热点行业[{i+1}]: {sector['sector_name']}, 趋势: {sector['trend_stage']}")

        # 2. 获取昨日涨停池（用于确认首板）
        yesterday_date = self._get_date_offset(date_str, -1)
        yesterday_zt = history_pools.get(yesterday_date, pd.DataFrame())
        logger.debug(f"[首板突破] 昨日涨停池日期: {yesterday_date}, 数量: {len(yesterday_zt)}")

        # 3. 遍历每个热点行业，分析其中的首板
        total_analyzed = 0
        total_filtered = 0

        for sector_info in hot_sectors:
            sector_name = sector_info['sector_name']
            sector_stats = sector_info['stats']

            # 获取该行业的涨停股票
            sector_stocks = self._get_sector_stocks(today_zt, sector_name)
            if sector_stocks.empty:
                logger.debug(f"  [{sector_name}] 无涨停股票")
                continue

            logger.info(f"  [{sector_name}] 今日涨停 {len(sector_stocks)} 只，分析首板...")

            # 分析该行业中的首板
            sector_signals = 0
            for _, stock in sector_stocks.iterrows():
                total_analyzed += 1
                signal = self._analyze_first_board(
                    stock, yesterday_zt, sector_info, date_str
                )
                if signal:
                    signals.append(signal)
                    sector_signals += 1
                else:
                    total_filtered += 1

            logger.debug(f"  [{sector_name}] 符合条件: {sector_signals}/{len(sector_stocks)}")

        # 按置信度排序
        signals.sort(key=lambda x: x.confidence, reverse=True)
        logger.info(f"首板突破检测完成: 共 {len(signals)} 个信号 (分析{total_analyzed}只, 过滤{total_filtered}只)")

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

        # 使用sector_heat_v2分析板块热度
        try:
            calculator = SectorHeatCalculatorV2()
            sector_df = calculator.analyze_all_sectors_v2(today_zt, history_pools, self.mapper)

            if sector_df.empty:
                logger.warning("板块分析结果为空")
                return hot_sectors

            # 筛选热点行业（爆发期、加速期、确认期）
            for _, row in sector_df.iterrows():
                trend_stage = row.get('趋势阶段', '')
                if trend_stage in ['爆发期', '加速期', '确认期', '启动期']:
                    sector_name = row.get('二级行业', '')
                    if sector_name:
                        hot_sectors.append({
                            'sector_name': sector_name,
                            'stats': row.get('核心指标', {}),
                            'trend_stage': trend_stage,
                            'action': row.get('行动建议', ''),
                            'confidence': float(row.get('置信度', '0%').replace('%', '')) / 100
                        })

            logger.info(f"从板块分析中识别到 {len(hot_sectors)} 个热点行业")

        except Exception as e:
            logger.error(f"获取热点行业失败: {e}")

        return hot_sectors

    def _get_sector_stocks(self, today_zt: pd.DataFrame, sector_name: str) -> pd.DataFrame:
        """
        获取指定行业的涨停股票
        """
        logger.debug(f"    [_get_sector_stocks] 查找行业'{sector_name}'的股票")
        logger.debug(f"    [_get_sector_stocks] 涨停池列名: {list(today_zt.columns)}")

        result = pd.DataFrame()
        if '所属行业' in today_zt.columns:
            result = today_zt[today_zt['所属行业'] == sector_name].copy()
            logger.debug(f"    [_get_sector_stocks] 使用'所属行业'列，找到{len(result)}只")
        elif 'L2_Industry' in today_zt.columns:
            result = today_zt[today_zt['L2_Industry'] == sector_name].copy()
            logger.debug(f"    [_get_sector_stocks] 使用'L2_Industry'列，找到{len(result)}只")
        else:
            logger.warning(f"    [_get_sector_stocks] 涨停池缺少行业列，可用列: {list(today_zt.columns)}")

        return result

    def _analyze_first_board(self, stock: pd.Series,
                            yesterday_zt: pd.DataFrame,
                            sector_info: Dict,
                            date_str: str) -> Optional[TradeSignal]:
        """
        分析单只股票是否为首板突破

        筛选条件：
        1. 今日涨停 + 昨日未涨停（首板确认）
        2. 涨停时间 < 14:30（拒绝偷袭板）
        3. 封单强度 > 5%
        4. 近5日涨幅 < 15%（低位要求）- 需要日线数据
        5. 量比 > 3（资金突然介入）- 需要日线数据
        6. 当天日线穿过5日、10日线 - 需要日线数据

        Args:
            stock: 股票数据（来自涨停池）
            yesterday_zt: 昨日涨停池
            sector_info: 行业信息
            date_str: 日期字符串

        Returns:
            TradeSignal or None: 符合条件的交易信号
        """
        code = stock.get('代码', '')
        name = stock.get('名称', '')
        sector_name = sector_info['sector_name']

        logger.debug(f"    [分析{name}] 开始首板分析...")

        # 条件1: 今日涨停（涨停池中的股票默认已涨停）
        change = stock.get('涨跌幅', 0)
        if isinstance(change, str):
            change = float(change.replace('%', ''))
        if change < 9.5:
            logger.debug(f"    [分析{name}] 过滤: 涨幅{change:.2f}% < 9.5%")
            return None
        logger.debug(f"    [分析{name}] 涨幅{change:.2f}% 符合条件")

        # 条件2: 昨日未涨停（首板确认）
        if not yesterday_zt.empty and code in yesterday_zt['代码'].values:
            logger.debug(f"    [分析{name}] 过滤: 昨日已涨停，非首板")
            return None  # 昨日已涨停，不是首板
        logger.debug(f"    [分析{name}] 昨日未涨停，首板确认")

        # 条件3: 涨停时间 < 14:30（拒绝偷袭板）
        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        if not self._is_valid_limit_time(limit_up_time):
            logger.debug(f"    [分析{name}] 过滤: 涨停时间{limit_up_time}不符合要求(需<14:30)")
            return None
        logger.debug(f"    [分析{name}] 涨停时间{limit_up_time} 符合条件")

        # 条件4: 封单强度 > 5%
        # 尝试获取封单额（不同数据源可能使用不同列名）
        seal_amount = stock.get('封单额', 0) or stock.get('封板资金', 0)
        float_cap = stock.get('流通市值', 1)  # 流通市值单位是亿，转换为万
        seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
        logger.debug(f"    [分析{name}] 封单数据: 封单额={seal_amount}, 流通市值={stock.get('流通市值', 0)}亿, 强度={seal_ratio*100:.2f}%")
        if seal_ratio < 0.02:
            logger.debug(f"    [分析{name}] 过滤: 封单强度{seal_ratio*100:.2f}% < 2%")
            return None
        logger.debug(f"    [分析{name}] 封单强度{seal_ratio*100:.2f}% 符合条件")

        # 获取日线数据进行进一步分析
        daily_data = self._get_daily_data(code, date_str)
        logger.debug(f"    [分析{name}] 日线数据: {daily_data is not None}")

        # 条件5: 近5日涨幅 < 15%（低位要求）
        if daily_data and 'rise_5d' in daily_data:
            if daily_data['rise_5d'] >= self.params['max_5d_rise']:
                logger.debug(f"    [分析{name}] 过滤: 5日涨幅{daily_data['rise_5d']*100:.2f}% >= 15%")
                return None
            logger.debug(f"    [分析{name}] 5日涨幅{daily_data['rise_5d']*100:.2f}% 符合条件")
        elif daily_data:
            logger.debug(f"    [分析{name}] 无5日涨幅数据，跳过此条件")

        # 条件6: 量比 > 3（资金突然介入）
        if daily_data and 'volume_ratio' in daily_data:
            if daily_data['volume_ratio'] < self.params['min_volume_ratio']:
                logger.debug(f"    [分析{name}] 过滤: 量比{daily_data['volume_ratio']:.2f} < 3")
                return None
            logger.debug(f"    [分析{name}] 量比{daily_data['volume_ratio']:.2f} 符合条件")
        elif daily_data:
            logger.debug(f"    [分析{name}] 无量比数据，跳过此条件")

        # 条件7: 当天日线穿过5日、10日线
        ma_breakthrough = False
        if daily_data and all(k in daily_data for k in ['close', 'ma5', 'ma10']):
            # 当天收盘价上穿5日线和10日线
            if daily_data['close'] > daily_data['ma5'] and daily_data['close'] > daily_data['ma10']:
                ma_breakthrough = True
                logger.debug(f"    [分析{name}] 均线突破: 收盘价{daily_data['close']:.2f} > MA5({daily_data['ma5']:.2f}) & MA10({daily_data['ma10']:.2f})")
            else:
                logger.debug(f"    [分析{name}] 未突破均线: 收盘价{daily_data['close']:.2f}, MA5({daily_data['ma5']:.2f}), MA10({daily_data['ma10']:.2f})")
        else:
            logger.debug(f"    [分析{name}] 无均线数据")

        # 计算置信度
        confidence = 0.70  # 基础置信度
        confidence += min(seal_ratio * 2, 0.15)  # 封单强度加成
        if daily_data and 'volume_ratio' in daily_data:
            confidence += min((daily_data['volume_ratio'] - 3) * 0.02, 0.10)  # 量比加成
        if ma_breakthrough:
            confidence += 0.05  # 均线突破加成
        confidence = min(confidence, 0.95)

        # 构建理由
        reason_parts = [f"首板突破+{sector_name}热点"]
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
            f"属于热点行业: {sector_name}"
        ]
        if daily_data and 'rise_5d' in daily_data:
            validation_rules.append(f"近5日涨幅<{self.params['max_5d_rise']*100:.0f}%（低位）")
        if daily_data and 'volume_ratio' in daily_data:
            validation_rules.append(f"量比>{self.params['min_volume_ratio']}（资金介入）")

        logger.debug(f"    [分析{name}] 通过所有条件，生成信号 (置信度{confidence:.2f})")

        return TradeSignal(
            pattern_type=PatternType.HOTSPOT_FIRST_BOARD,
            stock_code=code,
            stock_name=name,
            trigger_time=limit_up_time,
            confidence=confidence,
            entry_price=stock.get('涨停价', stock.get('最新价', 0)),
            stop_loss=stock.get('涨停价', stock.get('最新价', 0)) * 0.93,
            take_profit=stock.get('涨停价', stock.get('最新价', 0)) * 1.10,
            position_size="light",
            reason="+".join(reason_parts),
            key_metrics=key_metrics,
            validation_rules=validation_rules,
            l2_industry=sector_name
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
                    logger.debug(f"    [_get_daily_data] {stock_code} 数据获取成功: 5日涨幅={rise_5d*100:.2f}%, 量比={volume_ratio:.2f}, MA5={latest['ma5']:.2f}, MA10={latest['ma10']:.2f}")
                    return result
                else:
                    logger.debug(f"    [_get_daily_data] {stock_code} 数据不足: {len(df)}条")
            else:
                logger.debug(f"    [_get_daily_data] data_manager没有get_stock_daily方法")
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

    def _is_valid_limit_time(self, limit_up_time: str) -> bool:
        """
        检查涨停时间是否有效（非尾盘偷袭）
        支持格式: HH:MM:SS, HH:MM, HHMMSS, HHMM
        """
        if not limit_up_time or limit_up_time == '-':
            return False

        try:
            # 处理不同格式
            time_str = str(limit_up_time).strip().replace(':', '')

            # 统一转换为 HHMM 格式（取前4位）
            if len(time_str) == 6:  # HHMMSS 格式
                time_str = time_str[:4]  # 取 HHMM
            elif len(time_str) == 5:  # HMMSS 格式 (如 93916)
                time_str = '0' + time_str[:3]  # 补0变成 09:39
            elif len(time_str) < 4:  # 太短，无法解析
                logger.debug(f"时间格式太短: {limit_up_time}")
                return False

            hour = int(time_str[:2])
            minute = int(time_str[2:4])
            max_hour = int(self.params['max_limit_up_time'][:2])
            max_minute = int(self.params['max_limit_up_time'][3:5])  # 注意: 14:30 的冒号位置

            logger.debug(f"解析时间: {limit_up_time} -> {hour:02d}:{minute:02d}, 限制: {max_hour:02d}:{max_minute:02d}")

            if hour < max_hour or (hour == max_hour and minute <= max_minute):
                return True
        except Exception as e:
            logger.debug(f"解析涨停时间失败: {limit_up_time}, {e}")

        return False

    def _get_date_offset(self, date_str: str, offset: int) -> str:
        """日期偏移计算"""
        dt = datetime.strptime(date_str, "%Y%m%d")
        target = dt + timedelta(days=offset)
        return target.strftime("%Y%m%d")
