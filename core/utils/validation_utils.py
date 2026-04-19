"""
数据验证工具模块 - 提供股票相关的数据验证和检查函数
"""
from typing import Union, Optional
import pandas as pd


class ValidationUtils:
    """数据验证工具类 - 提供股票相关的数据验证和检查功能"""

    @staticmethod
    def is_limit_up(limit: str) -> bool:
        """
        判断是否涨停（使用 Tushare limit_list_d 接口的 limit 字段）

        Args:
            limit: Tushare limit 字段值（U=涨停, D=跌停, Z=炸板）

        Returns:
            是否涨停
        """
        return limit == 'U'

    @staticmethod
    def is_limit_down(limit: str) -> bool:
        """
        判断是否跌停（使用 Tushare limit_list_d 接口的 limit 字段）

        Args:
            limit: Tushare limit 字段值（U=涨停, D=跌停, Z=炸板）

        Returns:
            是否跌停
        """
        return limit == 'D'

    @staticmethod
    def is_broken_limit(limit: str) -> bool:
        """
        判断是否炸板（使用 Tushare limit_list_d 接口的 limit 字段）

        Args:
            limit: Tushare limit 字段值（U=涨停, D=跌停, Z=炸板）

        Returns:
            是否炸板
        """
        return limit == 'Z'

    @staticmethod
    def is_yizi_board(first_seal_time: str) -> bool:
        """
        判断是否为一字板（9:25前涨停）

        Args:
            first_seal_time: 首次封板时间

        Returns:
            是否为一字板
        """
        if not first_seal_time:
            return False

        from .time_utils import TimeUtils
        minutes = TimeUtils.time_to_minutes(str(first_seal_time))
        # 9:25 = 9*60+25 = 565分钟
        return minutes <= 565

    @staticmethod
    def is_miaoban(first_seal_time: str) -> bool:
        """
        判断是否为秒板（开盘后5分钟内涨停）

        Args:
            first_seal_time: 首次封板时间

        Returns:
            是否为秒板
        """
        if not first_seal_time:
            return False

        from .time_utils import TimeUtils
        minutes = TimeUtils.minutes_from_market_open(str(first_seal_time))
        return 0 <= minutes <= 5

    @staticmethod
    def is_late_board(last_seal_time: str, threshold: str = "14:30:00") -> bool:
        """
        判断是否为尾盘板

        Args:
            last_seal_time: 最后封板时间
            threshold: 尾盘时间阈值

        Returns:
            是否为尾盘板
        """
        if not last_seal_time:
            return False

        from .time_utils import TimeUtils
        return TimeUtils.compare_time(str(last_seal_time), threshold) > 0

    @staticmethod
    def is_broken_board(open_times: int) -> bool:
        """
        判断是否炸板

        Args:
            open_times: 炸板次数

        Returns:
            是否炸板
        """
        return open_times > 0

    @staticmethod
    def is_lanban(open_times: int, min_times: int = 2, max_times: int = 5) -> bool:
        """
        判断是否为烂板（炸板次数在一定范围内）

        Args:
            open_times: 炸板次数
            min_times: 最小炸板次数
            max_times: 最大炸板次数

        Returns:
            是否为烂板
        """
        return min_times <= open_times <= max_times

    @staticmethod
    def is_high_turnover(turnover: float, min_threshold: float = 15.0) -> bool:
        """
        判断是否高换手

        Args:
            turnover: 换手率（%）
            min_threshold: 最低阈值

        Returns:
            是否高换手
        """
        return turnover >= min_threshold

    @staticmethod
    def is_low_turnover(turnover: float, max_threshold: float = 5.0) -> bool:
        """
        判断是否低换手

        Args:
            turnover: 换手率（%）
            max_threshold: 最高阈值

        Returns:
            是否低换手
        """
        return turnover <= max_threshold

    @staticmethod
    def is_gap_up(gap_pct: float, min_gap: float = 0.02) -> bool:
        """
        判断是否高开

        Args:
            gap_pct: 跳空幅度（小数）
            min_gap: 最小高开幅度

        Returns:
            是否高开
        """
        return gap_pct >= min_gap

    @staticmethod
    def is_gap_down(gap_pct: float, min_gap: float = 0.02) -> bool:
        """
        判断是否低开

        Args:
            gap_pct: 跳空幅度（小数）
            min_gap: 最小低开幅度（绝对值）

        Returns:
            是否低开
        """
        return gap_pct <= -min_gap

    @staticmethod
    def is_strong_auction(gap_pct: float,
                          vol_ratio: float,
                          min_gap: float = 0.02,
                          min_vol_ratio: float = 0.08) -> bool:
        """
        判断是否强势竞价

        Args:
            gap_pct: 高开幅度
            vol_ratio: 竞价量比
            min_gap: 最小高开幅度
            min_vol_ratio: 最小量比

        Returns:
            是否强势竞价
        """
        return gap_pct >= min_gap and vol_ratio >= min_vol_ratio

    @staticmethod
    def is_sector_leader(board_height: int, max_height_in_sector: int) -> bool:
        """
        判断是否为板块龙头（板块内最高连板）

        Args:
            board_height: 当前连板高度
            max_height_in_sector: 板块内最高连板

        Returns:
            是否为龙头
        """
        return board_height >= max_height_in_sector

    @staticmethod
    def is_market_leader(board_height: int, max_height_in_market: int) -> bool:
        """
        判断是否为市场龙头（市场最高连板）

        Args:
            board_height: 当前连板高度
            max_height_in_market: 市场最高连板

        Returns:
            是否为市场龙头
        """
        return board_height >= max_height_in_market

    @staticmethod
    def classify_board_height(board_height: int) -> str:
        """
        分类连板高度

        Args:
            board_height: 连板高度

        Returns:
            分类：'low'(1-2板), 'mid'(3-4板), 'high'(5板+), 'excluded'(排除)
        """
        if 1 <= board_height <= 2:
            return 'low'
        elif 3 <= board_height <= 4:
            return 'mid'
        elif board_height >= 5:
            return 'high'
        return 'excluded'

    @staticmethod
    def is_valid_weak_quality(score: int, min_score: int = 60) -> bool:
        """
        验证弱的质量是否达标

        Args:
            score: 质量评分
            min_score: 最低分数

        Returns:
            是否达标
        """
        return score >= min_score

    @staticmethod
    def is_valid_turnover(turnover: float,
                          tier: str = 'low',
                          thresholds: dict = None) -> bool:
        """
        验证换手率是否达标

        Args:
            turnover: 换手率（%）
            tier: 层级（'low', 'high'）
            thresholds: 阈值配置

        Returns:
            是否达标
        """
        if thresholds is None:
            thresholds = {
                'low': {'min': 25, 'ideal': 40},
                'high': {'min': 35, 'ideal': 50}
            }

        tier_config = thresholds.get(tier, thresholds['low'])
        return turnover >= tier_config['min']

    @staticmethod
    def is_trade_date(date_str: str, trade_calendar: pd.DataFrame = None) -> bool:
        """
        判断是否为交易日

        Args:
            date_str: 日期字符串（YYYYMMDD）
            trade_calendar: 交易日历DataFrame

        Returns:
            是否为交易日
        """
        if trade_calendar is not None and not trade_calendar.empty:
            date_str = str(date_str)
            if 'cal_date' in trade_calendar.columns and 'is_open' in trade_calendar.columns:
                match = trade_calendar[trade_calendar['cal_date'].astype(str) == date_str]
                if not match.empty:
                    return match.iloc[0]['is_open'] == 1

        # 简单判断：非周末即为交易日
        from .date_utils import DateUtils
        return not DateUtils.is_weekend(date_str)

    @staticmethod
    def validate_price_data(price_data: dict,
                           required_fields: list = None) -> tuple:
        """
        验证价格数据完整性

        Args:
            price_data: 价格数据字典
            required_fields: 必需字段列表

        Returns:
            (是否有效, 错误信息)
        """
        if required_fields is None:
            required_fields = ['open', 'high', 'low', 'close', 'pre_close']

        if not price_data:
            return False, "价格数据为空"

        missing_fields = [f for f in required_fields if f not in price_data]
        if missing_fields:
            return False, f"缺少字段: {missing_fields}"

        # 检查价格有效性
        for field in required_fields:
            value = price_data.get(field, 0)
            if value <= 0:
                return False, f"{field}价格无效: {value}"

        return True, "数据有效"

    @staticmethod
    def is_data_fresh(data_time: str,
                      current_time: str = None,
                      max_delay_minutes: int = 5) -> bool:
        """
        判断数据是否新鲜

        Args:
            data_time: 数据时间
            current_time: 当前时间，默认使用系统时间
            max_delay_minutes: 最大延迟分钟数

        Returns:
            数据是否新鲜
        """
        from .time_utils import TimeUtils

        data_minutes = TimeUtils.time_to_minutes(str(data_time))

        if current_time:
            current_minutes = TimeUtils.time_to_minutes(str(current_time))
        else:
            from datetime import datetime
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute

        delay = abs(current_minutes - data_minutes)
        return delay <= max_delay_minutes


# 向后兼容的别名
is_limit_up = ValidationUtils.is_limit_up
is_limit_down = ValidationUtils.is_limit_down
is_broken_limit = ValidationUtils.is_broken_limit
is_yizi_board = ValidationUtils.is_yizi_board
is_miaoban = ValidationUtils.is_miaoban
is_late_board = ValidationUtils.is_late_board
is_broken_board = ValidationUtils.is_broken_board
is_lanban = ValidationUtils.is_lanban
is_high_turnover = ValidationUtils.is_high_turnover
is_low_turnover = ValidationUtils.is_low_turnover
is_gap_up = ValidationUtils.is_gap_up
is_gap_down = ValidationUtils.is_gap_down
is_strong_auction = ValidationUtils.is_strong_auction
is_sector_leader = ValidationUtils.is_sector_leader
is_market_leader = ValidationUtils.is_market_leader
classify_board_height = ValidationUtils.classify_board_height
is_valid_weak_quality = ValidationUtils.is_valid_weak_quality
is_valid_turnover = ValidationUtils.is_valid_turnover
is_trade_date = ValidationUtils.is_trade_date
validate_price_data = ValidationUtils.validate_price_data
is_data_fresh = ValidationUtils.is_data_fresh


if __name__ == "__main__":
    # 测试
    print("验证工具测试:")
    print(f"  涨停判断(U): {ValidationUtils.is_limit_up('U')}")
    print(f"  跌停判断(D): {ValidationUtils.is_limit_down('D')}")
    print(f"  炸板判断(Z): {ValidationUtils.is_broken_limit('Z')}")
    print(f"  一字板判断: {ValidationUtils.is_yizi_board('09:24:00')}")
    print(f"  秒板判断: {ValidationUtils.is_miaoban('09:31:00')}")
    print(f"  尾盘板判断: {ValidationUtils.is_late_board('14:35:00')}")
    print(f"  烂板判断: {ValidationUtils.is_lanban(3)}")
    print(f"  高换手判断: {ValidationUtils.is_high_turnover(20)}")
    print(f"  连板分类(2板): {ValidationUtils.classify_board_height(2)}")
    print(f"  连板分类(5板): {ValidationUtils.classify_board_height(5)}")
