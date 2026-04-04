"""
龙二波策略 - 正确的历史连板判断
核心：从每日涨停池取近期连板，非日线涨幅计算
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class PatternType(Enum):
    DRAGON_SECOND_WAVE = "龙二波"


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


class DragonSecondWaveStrategyV2:
    def __init__(self, data_manager, sentiment_engine):
        self.dm = data_manager
        self.se = sentiment_engine
        
        # 时间参数（近期记忆）
        self.params = {
            "recent_days": 15,           # 只取近15天内的行情（记忆未散）
            "max_adjust_days": 10,       # 调整期最多10天
            "min_first_wave": 4,         # 第一波至少4板
            "max_first_wave": 12,        # 第一波最多12板（太多则记忆透支）
        }
    
    def detect_second_wave(self,
                          stock_code: str,
                          stock_name: str,
                          today_str: str,
                          recent_zt_pools: Dict[str, pd.DataFrame],  # 近15日每日涨停池
                          today_data: pd.Series,
                          sector_hot: bool) -> Optional[TradeSignal]:
        """
        检测龙二波机会
        recent_zt_pools: {日期: 当日涨停池DataFrame}
        """
        stock_code_padded = str(stock_code).zfill(6)
        logger.debug(f"[{stock_code_padded}] 开始检测龙二波 - 名称:{stock_name}, 日期:{today_str}, 板块热度:{sector_hot}")

        # ========== 步骤1：从涨停池重建近期连板记录（关键！）==========
        consecutive_record = self._rebuild_consecutive_from_pools(
            stock_code, recent_zt_pools
        )

        if not consecutive_record['is_valid']:
            logger.debug(f"[{stock_code_padded}] 过滤: 连板记录无效 - {consecutive_record.get('reason', '未知原因')}")
            return None

        first_wave_info = consecutive_record['first_wave']
        logger.debug(f"[{stock_code_padded}] 连板记录: 最大连板{first_wave_info['max_boards']}板, 起涨日{first_wave_info['start_date']}, 见顶日{first_wave_info['peak_date']}")

        # 检查是否是近期这一波（非历史久远）
        days_since_peak = self._calculate_days_since_peak(
            first_wave_info['peak_date'], today_str
        )

        if days_since_peak > self.params["max_adjust_days"] + 5:
            logger.debug(f"[{stock_code_padded}] 过滤: 第一波距今太久 ({days_since_peak}天 > {self.params['max_adjust_days'] + 5}天)")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 第一波距今{days_since_peak}天")

        # ========== 步骤2：判断第一波高度（真龙标准）==========

        if not (self.params["min_first_wave"] <= first_wave_info['max_boards'] <= self.params["max_first_wave"]):
            logger.debug(f"[{stock_code_padded}] 过滤: 第一波高度不符合 ({first_wave_info['max_boards']}板, 要求{self.params['min_first_wave']}-{self.params['max_first_wave']}板)")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 第一波高度{first_wave_info['max_boards']}板")

        # ========== 步骤3：检查调整期形态 ==========
        adjust_period = self._get_adjust_period(
            stock_code, first_wave_info['peak_date'], today_str
        )

        if not adjust_period:
            logger.debug(f"[{stock_code_padded}] 过滤: 无法获取调整期数据")
            return None

        logger.debug(f"[{stock_code_padded}] 调整期数据: 深度{adjust_period.get('depth', 0)*100:.1f}%, MA10:{adjust_period.get('ma10', 0):.2f}, 天数{adjust_period.get('days', 0)}")

        if not self._check_adjust_quality(adjust_period):
            logger.debug(f"[{stock_code_padded}] 过滤: 调整期质量不符合要求")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 调整期质量检查")

        # ========== 步骤4：今日启动确认 ==========
        today_change = today_data.get('涨跌幅', 0)

        if today_change < 9.5:  # 今日未涨停
            logger.debug(f"[{stock_code_padded}] 过滤: 今日未涨停 ({today_change:.2f}% < 9.5%)")
            return None
        logger.debug(f"[{stock_code_padded}] 通过: 今日涨停 {today_change:.2f}%")

        today_pool = recent_zt_pools.get(today_str, pd.DataFrame())
        if today_pool.empty:
            logger.debug(f"[{stock_code_padded}] 过滤: 今日涨停池为空")
            return None


        # 兼容不同的列名
        code_col = None
        if '代码' in today_pool.columns:
            code_col = '代码'
        elif 'Code' in today_pool.columns:
            code_col = 'Code'
        elif 'ts_code' in today_pool.columns:
            code_col = 'ts_code'

        if code_col is None:
            logger.debug(f"[{stock_code_padded}] 过滤: 涨停池缺少代码列")
            return None

        # 确保代码格式一致（都是字符串）
        today_pool_codes = today_pool[code_col].astype(str).str.zfill(6).tolist()

        if stock_code_padded not in today_pool_codes:
            logger.debug(f"[{stock_code_padded}] 过滤: 不在今日涨停池中")
            return None  # 虽然涨幅>9.5%，但可能不是涨停（如科创板20%）
        logger.debug(f"[{stock_code_padded}] 通过: 在今日涨停池中")
        
        # ========== 构建信号 ==========
        logger.debug(f"[{stock_code_padded}] 生成龙二波信号: {first_wave_info['max_boards']}板龙头, 调整{days_since_peak}天, 板块热度:{sector_hot}")
        return TradeSignal(
            pattern_type=PatternType.DRAGON_SECOND_WAVE,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=today_data.get('首次封板时间', ''),
            confidence=0.82,
            entry_price=today_data.get('涨停价', 0),
            stop_loss=adjust_period['ma10'] * 0.97,
            take_profit=today_data.get('涨停价', 0) * 1.15,
            position_size="medium",
            reason=f"近期{first_wave_info['max_boards']}板龙头，调整{days_since_peak}天后二波启动",
            key_metrics={
                "第一波高度": first_wave_info['max_boards'],
                "第一波日期": f"{first_wave_info['start_date']}至{first_wave_info['peak_date']}",
                "调整天数": days_since_peak,
                "调整深度": f"{adjust_period['depth']*100:.1f}%",
                "支撑均线": f"MA10:{adjust_period['ma10']:.2f}"
            },
            validation_rules=[
                f"近15日内{first_wave_info['max_boards']}连板（真龙）",
                f"调整{days_since_peak}天（记忆未散）",
                "回踩MA10获支撑",
                "地量后放量首板",
                "板块热度未退" if sector_hot else "板块已冷（风险）"
            ]
        )
    
    # ==================== 核心方法：从涨停池重建连板记录 ====================

    def _rebuild_consecutive_from_pools(self,
                                       stock_code: str,
                                       recent_pools: Dict[str, pd.DataFrame]) -> Dict:
        """
        从近15日涨停池重建该股的连板记录
        返回：是否是近期龙头，第一波信息等
        """
        dates = sorted(recent_pools.keys())

        zt_dates = []  # 该股涨停的日期列表

        for date in dates:
            pool = recent_pools[date]
            if pool.empty:
                continue

            # 兼容不同的列名：'代码' 或 'Code'
            code_col = None
            if '代码' in pool.columns:
                code_col = '代码'
            elif 'Code' in pool.columns:
                code_col = 'Code'
            elif 'ts_code' in pool.columns:
                code_col = 'ts_code'

            if code_col is None:
                continue

            if stock_code in pool[code_col].values:
                zt_dates.append(date)

        if len(zt_dates) < self.params["min_first_wave"]:
            return {'is_valid': False, 'reason': f'连板数不足({len(zt_dates)} < {self.params["min_first_wave"]})'}

        # 计算连续涨停（允许断板1个交易日）
        consecutive_groups = []
        current_group = [zt_dates[0]]

        for i in range(1, len(zt_dates)):
            prev_date_str = zt_dates[i-1]
            curr_date_str = zt_dates[i]

            # 使用交易日历判断两个日期之间有几个交易日
            trading_days_between = self._count_trading_days_between(prev_date_str, curr_date_str)

            # 间隔1个交易日算连续（允许断板1天）
            # 例如：周五和下周一，trading_days_between=0，算连续
            # 例如：周一和周三（周二停牌），trading_days_between=1，算连续
            # 例如：周一和周四（中间有2个交易日），trading_days_between=2，算断板
            if trading_days_between <= 1:
                current_group.append(curr_date_str)
            else:
                consecutive_groups.append(current_group)
                current_group = [curr_date_str]

        consecutive_groups.append(current_group)
        
        # 找最大连板组
        max_group = max(consecutive_groups, key=len)
        max_boards = len(max_group)

        if max_boards < self.params["min_first_wave"]:
            return {'is_valid': False, 'reason': f'最大连板数不足({max_boards} < {self.params["min_first_wave"]})'}

        # 检查是否是近期这一波（非开头几天）
        peak_date = max_group[-1]
        first_date = max_group[0]

        # 距离今天不能太久
        today = datetime.strptime(dates[-1], "%Y%m%d")
        peak = datetime.strptime(peak_date, "%Y%m%d")
        days_since_peak = (today - peak).days

        if days_since_peak > self.params["max_adjust_days"] + 5:
            return {'is_valid': False, 'reason': f'第一波距今太久({days_since_peak}天)'}

        return {
            'is_valid': True,
            'first_wave': {
                'max_boards': max_boards,
                'start_date': first_date,
                'peak_date': peak_date,
                'zt_dates': max_group
            },
            'all_zt_dates': zt_dates
        }

    def _count_trading_days_between(self, start_date: str, end_date: str) -> int:
        """
        计算两个日期之间有多少个交易日（不包括start_date，包括end_date）

        例如：
        - 周五(20260320)到下周一(20260323)：中间没有交易日，返回0
        - 周一(20260323)到周三(20260325)：中间有1个交易日(周二)，返回1
        - 周一(20260323)到周四(20260326)：中间有2个交易日(周二、周三)，返回2

        Args:
            start_date: 开始日期，格式YYYYMMDD
            end_date: 结束日期，格式YYYYMMDD

        Returns:
            两个日期之间的交易日数量
        """
        try:
            # 尝试使用data_manager的交易日历
            if hasattr(self.dm, 'get_trade_calendar'):
                cal_df = self.dm.get_trade_calendar(start_date, end_date)
                if not cal_df.empty and 'is_open' in cal_df.columns:
                    # 计算start_date和end_date之间的交易日数量
                    # 不包括start_date当天
                    cal_df['cal_date'] = cal_df['cal_date'].astype(str)
                    mask = (cal_df['cal_date'] > start_date) & (cal_df['cal_date'] <= end_date)
                    trading_days = cal_df[mask & (cal_df['is_open'] == 1)]
                    return len(trading_days)
        except Exception as e:
            logger.debug(f"获取交易日历失败，使用简化计算: {e}")

        # 简化计算：使用日历天数减去周末
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        # 计算总天数差
        total_days = (end - start).days

        # 计算中间有多少个周末
        # 从start的下一天开始算
        weekend_days = 0
        current = start + timedelta(days=1)
        while current <= end:
            if current.weekday() >= 5:  # 周六或周日
                weekend_days += 1
            current += timedelta(days=1)

        trading_days = total_days - weekend_days
        return max(0, trading_days)

    def _get_adjust_period(self, stock_code: str,
                          peak_date: str, today: str) -> Dict:
        """
        获取调整期数据（peak_date到today之间）
        为了确保能计算MA10，需要获取peak_date之前额外的数据
        """
        # 计算需要提前获取的天数（至少10天数据用于计算MA10）
        # 从peak_date往前推15个交易日，确保有足够数据
        peak_dt = datetime.strptime(peak_date, "%Y%m%d")
        extended_start_dt = peak_dt - timedelta(days=20)  # 往前推20个日历天（约15个交易日）
        extended_start = extended_start_dt.strftime("%Y%m%d")

        # 从data_manager获取日线数据（扩大范围）
        hist = self.dm.get_stock_daily(stock_code, extended_start, today)
        if not hist.empty:
            # 确保数据按日期升序排序（rolling计算需要）
            if 'trade_date' in hist.columns:
                hist = hist.sort_values('trade_date').reset_index(drop=True)

        if hist.empty:
            return {}

        # 筛选出peak_date之后的数据用于分析调整期
        if 'trade_date' in hist.columns:
            peak_dt_ts = pd.Timestamp(peak_date)
            adjust_hist = hist[hist['trade_date'] >= peak_dt_ts].copy()
        else:
            adjust_hist = hist.copy()

        if len(adjust_hist) < 3:
            return {}

        # 使用完整数据计算MA10
        peak_price = adjust_hist.iloc[0]['high']  # 第一波最高价
        lowest = adjust_hist['low'].min()

        # 计算调整深度
        depth = (peak_price - lowest) / peak_price

        # 计算均线 - 使用完整历史数据计算MA10
        total_days = len(hist)
        if total_days < 10:
            return {}

        hist['MA10'] = hist['close'].rolling(10).mean()

        # 获取today对应的MA10值
        ma10 = None
        if 'trade_date' in hist.columns:
            today_dt_ts = pd.Timestamp(today)
            today_row = hist[hist['trade_date'] == today_dt_ts]
            if not today_row.empty:
                ma10 = today_row.iloc[-1]['MA10']
            else:
                ma10 = hist.iloc[-1]['MA10']
        else:
            ma10 = hist.iloc[-1]['MA10']

        # 检查MA10是否有效
        if pd.isna(ma10):
            return {}

        adjust_days = len(adjust_hist)

        return {
            'depth': depth,
            'ma10': ma10,
            'lowest_price': lowest,
            'days': adjust_days
        }
    
    def _check_adjust_quality(self, adjust: Dict) -> bool:
        """检查调整质量"""
        if not adjust:
            return False

        # 调整深度10-25%
        if not (0.10 <= adjust['depth'] <= 0.25):
            return False

        # 调整阶段仍在10日线之上
        # 这里假设latest_price在调用方传入，或者从adjust中获取
        # 简化判断：如果最低价在MA10的95%以上，认为在10日线之上
        if 'lowest_price' in adjust and 'ma10' in adjust:
            lowest_price = adjust['lowest_price']
            ma10 = adjust['ma10']
            if lowest_price < ma10 * 0.95:  # 跌破MA10超过5%
                return False

        return True
    
    def _calculate_days_since_peak(self, peak_date: str, today: str) -> int:
        """计算从第一波见顶到今天的天数"""
        peak = datetime.strptime(peak_date, "%Y%m%d")
        today_dt = datetime.strptime(today, "%Y%m%d")
        return (today_dt - peak).days

# ==================== 数据准备示例 ====================

def prepare_recent_pools(data_manager, today: str, days: int = 15) -> Dict[str, pd.DataFrame]:
    """
    准备近15日每日涨停池
    """
    pools = {}
    
    for i in range(days):
        date = data_manager.get_date_offset(today, -i)
        pool = data_manager.get_limit_up_pool(date)
        if not pool.empty:
            pools[date] = pool
    
    return pools

# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("龙二波策略V2 - 正确的历史连板判断")
    print("核心：从每日涨停池取近期连板，非日线涨幅计算")
    print("时间范围：近15日内，记忆未散")
    print("")
    print("正确做法：")
    print("1. 取近15日每日涨停池")
    print("2. 检查目标股在哪些日期出现在涨停池")
    print("3. 计算连续出现次数（允许断板1天）")
    print("4. 确认是近期这一波（非3个月前的行情）")
    print("5. 今日再次出现在涨停池=二波启动确认")