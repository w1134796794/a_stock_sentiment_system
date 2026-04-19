"""
时间工具模块 - 提供统一的时间格式转换和计算函数
主要用于处理股票交易时间相关计算
"""
from typing import Optional, Tuple
import re


class TimeUtils:
    """时间工具类 - 提供时间格式转换和交易时间计算功能"""

    # 交易时间常量
    MARKET_OPEN_HOUR = 9
    MARKET_OPEN_MINUTE = 30
    MARKET_CLOSE_HOUR = 15
    MARKET_CLOSE_MINUTE = 0
    AUCTION_START_HOUR = 9
    AUCTION_START_MINUTE = 15
    AUCTION_END_HOUR = 9
    AUCTION_END_MINUTE = 25

    @staticmethod
    def time_to_minutes(time_str: str) -> int:
        """
        将时间字符串转换为从0点开始的分钟数

        支持格式:
        - HH:MM:SS (如 "09:30:00")
        - HH:MM (如 "09:30")
        - HHMMSS (如 "093000")
        - HHMM (如 "0930")
        - HMM (如 "930")

        Args:
            time_str: 时间字符串

        Returns:
            从0点开始的分钟数，解析失败返回0
        """
        if not time_str or time_str == '0':
            return 0

        time_str = str(time_str).strip()

        try:
            # 处理 HH:MM:SS 或 HH:MM 格式
            if ':' in time_str:
                parts = time_str.split(':')
                hour = int(parts[0])
                minute = int(parts[1])
                return hour * 60 + minute

            # 处理纯数字格式（去掉所有非数字字符）
            time_str = re.sub(r'\D', '', time_str)  # 只保留数字

            if len(time_str) == 6:
                # HHMMSS 格式
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                return hour * 60 + minute
            elif len(time_str) == 4:
                # HHMM 格式
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                return hour * 60 + minute
            elif len(time_str) == 3:
                # HMM 格式（如 930 -> 9:30）
                hour = int(time_str[0])
                minute = int(time_str[1:3])
                return hour * 60 + minute
            elif len(time_str) == 2 or len(time_str) == 1:
                # MM 或 M 格式（分钟数）
                return int(time_str)
            else:
                return 0
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def minutes_to_time(minutes: int, fmt: str = "HH:MM") -> str:
        """
        将分钟数转换为时间字符串

        Args:
            minutes: 从0点开始的分钟数
            fmt: 输出格式，"HH:MM" 或 "HHMM" 或 "HH:MM:SS"

        Returns:
            时间字符串
        """
        if minutes < 0:
            minutes = 0
        if minutes >= 24 * 60:
            minutes = 23 * 59

        hour = minutes // 60
        minute = minutes % 60

        if fmt == "HH:MM":
            return f"{hour:02d}:{minute:02d}"
        elif fmt == "HHMM":
            return f"{hour:02d}{minute:02d}"
        elif fmt == "HH:MM:SS":
            return f"{hour:02d}:{minute:02d}:00"
        else:
            return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def format_time(time_str: str, input_fmt: str = "auto", output_fmt: str = "HH:MM:SS") -> str:
        """
        格式化时间字符串

        Args:
            time_str: 原始时间字符串
            input_fmt: 输入格式，"auto"表示自动识别
            output_fmt: 输出格式

        Returns:
            格式化后的时间字符串
        """
        if not time_str:
            return ""

        # 自动识别并转换为分钟数
        minutes = TimeUtils.time_to_minutes(time_str)

        # 转换为目标格式
        if output_fmt == "HH:MM:SS":
            return TimeUtils.minutes_to_time(minutes, "HH:MM:SS")
        elif output_fmt == "HH:MM":
            return TimeUtils.minutes_to_time(minutes, "HH:MM")
        elif output_fmt == "HHMMSS":
            return TimeUtils.minutes_to_time(minutes, "HHMM") + "00"
        elif output_fmt == "HHMM":
            return TimeUtils.minutes_to_time(minutes, "HHMM")
        else:
            return TimeUtils.minutes_to_time(minutes, "HH:MM")

    @classmethod
    def minutes_from_market_open(cls, time_str: str) -> int:
        """
        计算从开盘时间(9:30)开始的分钟数

        Args:
            time_str: 时间字符串

        Returns:
            从9:30开始的分钟数（9:30之前返回负数）
        """
        minutes = cls.time_to_minutes(time_str)
        market_open = cls.MARKET_OPEN_HOUR * 60 + cls.MARKET_OPEN_MINUTE
        return minutes - market_open

    @classmethod
    def minutes_to_market_close(cls, time_str: str) -> int:
        """
        计算距离收盘时间(15:00)还有多少分钟

        Args:
            time_str: 时间字符串

        Returns:
            距离15:00的分钟数（15:00之后返回负数）
        """
        minutes = cls.time_to_minutes(time_str)
        market_close = cls.MARKET_CLOSE_HOUR * 60 + cls.MARKET_CLOSE_MINUTE
        return market_close - minutes

    @classmethod
    def is_in_trading_hours(cls, time_str: str) -> bool:
        """
        判断时间是否在交易时段内（9:30 - 15:00）

        Args:
            time_str: 时间字符串

        Returns:
            是否在交易时段内
        """
        minutes = cls.time_to_minutes(time_str)
        market_open = cls.MARKET_OPEN_HOUR * 60 + cls.MARKET_OPEN_MINUTE
        market_close = cls.MARKET_CLOSE_HOUR * 60 + cls.MARKET_CLOSE_MINUTE
        return market_open <= minutes <= market_close

    @classmethod
    def is_in_auction(cls, time_str: str) -> bool:
        """
        判断时间是否在集合竞价时段（9:15 - 9:25）

        Args:
            time_str: 时间字符串

        Returns:
            是否在集合竞价时段内
        """
        minutes = cls.time_to_minutes(time_str)
        auction_start = cls.AUCTION_START_HOUR * 60 + cls.AUCTION_START_MINUTE
        auction_end = cls.AUCTION_END_HOUR * 60 + cls.AUCTION_END_MINUTE
        return auction_start <= minutes <= auction_end

    @classmethod
    def is_morning_session(cls, time_str: str) -> bool:
        """
        判断时间是否在上午交易时段（9:30 - 11:30）

        Args:
            time_str: 时间字符串

        Returns:
            是否在上午交易时段内
        """
        minutes = cls.time_to_minutes(time_str)
        morning_start = 9 * 60 + 30
        morning_end = 11 * 60 + 30
        return morning_start <= minutes <= morning_end

    @classmethod
    def is_afternoon_session(cls, time_str: str) -> bool:
        """
        判断时间是否在下午交易时段（13:00 - 15:00）

        Args:
            time_str: 时间字符串

        Returns:
            是否在下午交易时段内
        """
        minutes = cls.time_to_minutes(time_str)
        afternoon_start = 13 * 60
        afternoon_end = 15 * 60
        return afternoon_start <= minutes <= afternoon_end

    @staticmethod
    def compare_time(time1: str, time2: str) -> int:
        """
        比较两个时间

        Args:
            time1: 时间1
            time2: 时间2

        Returns:
            -1: time1 < time2
             0: time1 = time2
             1: time1 > time2
        """
        m1 = TimeUtils.time_to_minutes(time1)
        m2 = TimeUtils.time_to_minutes(time2)

        if m1 < m2:
            return -1
        elif m1 > m2:
            return 1
        else:
            return 0

    @staticmethod
    def is_time_before(time1: str, time2: str) -> bool:
        """判断time1是否在time2之前"""
        return TimeUtils.compare_time(time1, time2) < 0

    @staticmethod
    def is_time_after(time1: str, time2: str) -> bool:
        """判断time1是否在time2之后"""
        return TimeUtils.compare_time(time1, time2) > 0

    @staticmethod
    def is_time_between(time_str: str, start_time: str, end_time: str) -> bool:
        """
        判断时间是否在指定范围内

        Args:
            time_str: 要判断的时间
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            是否在范围内
        """
        minutes = TimeUtils.time_to_minutes(time_str)
        start = TimeUtils.time_to_minutes(start_time)
        end = TimeUtils.time_to_minutes(end_time)
        return start <= minutes <= end

    @classmethod
    def get_time_period(cls, time_str: str) -> str:
        """
        获取时间所属的交易时段

        Args:
            time_str: 时间字符串

        Returns:
            时段名称：
            - 'pre_market': 盘前（9:15前）
            - 'auction': 集合竞价（9:15-9:25）
            - 'open': 开盘（9:25-9:30）
            - 'morning': 上午交易（9:30-11:30）
            - 'noon': 午休（11:30-13:00）
            - 'afternoon': 下午交易（13:00-15:00）
            - 'after_market': 盘后（15:00后）
        """
        minutes = cls.time_to_minutes(time_str)

        if minutes < 9 * 60 + 15:
            return 'pre_market'
        elif minutes <= 9 * 60 + 25:
            return 'auction'
        elif minutes < 9 * 60 + 30:
            return 'open'
        elif minutes <= 11 * 60 + 30:
            return 'morning'
        elif minutes < 13 * 60:
            return 'noon'
        elif minutes <= 15 * 60:
            return 'afternoon'
        else:
            return 'after_market'

    @staticmethod
    def parse_time_range(time_range_str: str) -> Tuple[int, int]:
        """
        解析时间范围字符串

        Args:
            time_range_str: 时间范围（如 "09:30-11:30" 或 "0930-1130"）

        Returns:
            (开始分钟数, 结束分钟数)
        """
        if '-' in time_range_str:
            parts = time_range_str.split('-')
            start = TimeUtils.time_to_minutes(parts[0].strip())
            end = TimeUtils.time_to_minutes(parts[1].strip())
            return (start, end)

        return (0, 0)


# 保持向后兼容的函数接口
time_to_minutes = TimeUtils.time_to_minutes
minutes_to_time = TimeUtils.minutes_to_time
format_time = TimeUtils.format_time
minutes_from_market_open = TimeUtils.minutes_from_market_open
minutes_to_market_close = TimeUtils.minutes_to_market_close
is_in_trading_hours = TimeUtils.is_in_trading_hours
is_in_auction = TimeUtils.is_in_auction
is_morning_session = TimeUtils.is_morning_session
is_afternoon_session = TimeUtils.is_afternoon_session
compare_time = TimeUtils.compare_time
is_time_before = TimeUtils.is_time_before
is_time_after = TimeUtils.is_time_after
is_time_between = TimeUtils.is_time_between
get_time_period = TimeUtils.get_time_period
parse_time_range = TimeUtils.parse_time_range


if __name__ == "__main__":
    # 测试
    test_times = ['093000', '09:30:00', '09:30', '930', '143000']

    print("时间转换测试:")
    for t in test_times:
        minutes = TimeUtils.time_to_minutes(t)
        print(f"  {t} -> {minutes}分钟 -> {TimeUtils.minutes_to_time(minutes)}")

    print("\n交易时间判断:")
    for t in ['09:15', '09:30', '10:00', '11:30', '13:00', '15:00']:
        print(f"  {t}: 交易时段={TimeUtils.is_in_trading_hours(t)}, 竞价时段={TimeUtils.is_in_auction(t)}, "
              f"距开盘={TimeUtils.minutes_from_market_open(t)}分钟, 时段={TimeUtils.get_time_period(t)}")
