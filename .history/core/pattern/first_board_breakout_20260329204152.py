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
from datetime import datetime, time
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
    def __init__(self, data_manager, sector_engine=None):
        """
        data_manager: 数据管理器（DataManager）
        sector_engine: 板块热度引擎（可选）
        """
        self.dm = data_manager
        self.sector_engine = sector_engine

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
        for i, sector in enumerate(hot_sectors[:5]):  # 只打印前5个
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
            sector_df = calculator.analyze_all_sectors_v2(today_zt, history_pools)

            if sector_df.empty:
                logger.warning("板块分析结果为空")
                return hot_sectors

            # 筛选热点行业（爆发期、加速期、确认期）
            for _, row in sector_df.iterrows():
                trend_stage = row.get('趋势阶段', '')
                if trend_stage in ['爆发期', '加速期', '确认期']:
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
        if '所属行业' in today_zt.columns:
            return today_zt[today_zt['所属行业'] == sector_name].copy()
        elif 'L2_Industry' in today_zt.columns:
            return today_zt[today_zt['L2_Industry'] == sector_name].copy()
        return pd.DataFrame()

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

        # 条件1: 今日涨停（涨停池中的股票默认已涨停）
        change = stock.get('涨跌幅', 0)
        if isinstance(change, str):
            change = float(change.replace('%', ''))
        if change < 9.5:
            return None

        # 条件2: 昨日未涨停（首板确认）
        if not yesterday_zt.empty and code in yesterday_zt['代码'].values:
            return None  # 昨日已涨停，不是首板

        # 条件3: 涨停时间 < 14:30（拒绝偷袭板）
        limit_up_time = str(stock.get('首次封板时间', '')).strip()
        if not self._is_valid_limit_time(limit_up_time):
            return None

        # 条件4: 封单强度 > 5%
        seal_amount = stock.get('封单额', 0)
        float_cap = stock.get('流通市值', 1) * 10000
        seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
        if seal_ratio < 0.05:
            return None

        # 获取日线数据进行进一步分析
        daily_data = self._get_daily_data(code, date_str)

        # 条件5: 近5日涨幅 < 15%（低位要求）
        if daily_data and 'rise_5d' in daily_data:
            if daily_data['rise_5d'] >= self.params['max_5d_rise']:
                return None

        # 条件6: 量比 > 3（资金突然介入）
        if daily_data and 'volume_ratio' in daily_data:
            if daily_data['volume_ratio'] < self.params['min_volume_ratio']:
                return None

        # 条件7: 当天日线穿过5日、10日线
        ma_breakthrough = False
        if daily_data and all(k in daily_data for k in ['close', 'ma5', 'ma10']):
            # 当天收盘价上穿5日线和10日线
            if daily_data['close'] > daily_data['ma5'] and daily_data['close'] > daily_data['ma10']:
                ma_breakthrough = True

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
        获取股票的日线数据

        Returns:
            Dict: 包含以下字段（如果可用）：
                - rise_5d: 近5日涨幅
                - volume_ratio: 量比
                - close: 收盘价
                - ma5: 5日均线
                - ma10: 10日均线
        """
        try:
            # 尝试从data_manager获取日线数据
            if hasattr(self.dm, 'get_daily_data'):
                df = self.dm.get_daily_data(stock_code, date_str)
                if not df.empty:
                    latest = df.iloc[-1]
                    return {
                        'rise_5d': latest.get('rise_5d', 0) / 100 if 'rise_5d' in latest else 0,
                        'volume_ratio': latest.get('volume_ratio', 0),
                        'close': latest.get('close', 0),
                        'ma5': latest.get('ma5', 0),
                        'ma10': latest.get('ma10', 0)
                    }
        except Exception as e:
            logger.debug(f"获取日线数据失败 {stock_code}: {e}")

        return None

    def _is_valid_limit_time(self, limit_up_time: str) -> bool:
        """
        检查涨停时间是否有效（非尾盘偷袭）
        """
        if not limit_up_time or limit_up_time == '-':
            return False

        try:
            # 处理不同格式
            time_str = str(limit_up_time).strip().replace(':', '')
            if len(time_str) >= 4:
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                max_hour = int(self.params['max_limit_up_time'][:2])
                max_minute = int(self.params['max_limit_up_time'][2:4])

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
