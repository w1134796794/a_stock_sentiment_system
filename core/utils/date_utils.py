"""
日期工具模块 - 提供日期格式化和基于交易日历的交易日查询功能
交易日历文件路径由 config.settings.TRADE_CALENDAR_FILE 指定
"""
from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path
import pandas as pd

from config.settings import TRADE_CALENDAR_FILE


class DateUtils:
    """日期工具类 - 提供日期格式化和交易日查询功能"""

    _CALENDAR_PATH = Path(TRADE_CALENDAR_FILE)
    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if DateUtils._initialized:
            return
        self._prev_map: dict = {}
        self._next_map: dict = {}
        self._trade_dates: List[str] = []
        self._load_calendar()
        DateUtils._initialized = True

    def _load_calendar(self) -> None:
        """加载交易日历文件并构建快速映射"""
        if not self._CALENDAR_PATH.exists():
            raise FileNotFoundError(f"交易日历文件不存在: {self._CALENDAR_PATH}")

        df = pd.read_csv(self._CALENDAR_PATH)
        # 仅保留交易日
        open_df = df[df['is_open'] == 1].copy()
        # 统一日期格式为 YYYYMMDD
        open_df['cal_date'] = pd.to_datetime(open_df['cal_date']).dt.strftime('%Y%m%d')

        # 构建交易日列表（已排序）
        self._trade_dates = sorted(open_df['cal_date'].tolist())

        # 构建前向映射（根据列表推导，不使用 pretrade_date 因为可能有无效值）
        for i in range(1, len(self._trade_dates)):
            self._prev_map[self._trade_dates[i]] = self._trade_dates[i - 1]

        # 构建后向映射（根据列表推导）
        for i in range(len(self._trade_dates) - 1):
            self._next_map[self._trade_dates[i]] = self._trade_dates[i + 1]

    @staticmethod
    def parse_date(date_str: str, fmt: str = "%Y%m%d") -> Optional[datetime]:
        """解析日期字符串"""
        try:
            return datetime.strptime(str(date_str), fmt)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def format_date(date: datetime, fmt: str = "%Y%m%d") -> str:
        """格式化日期为字符串"""
        return date.strftime(fmt) if date else ""

    @staticmethod
    def get_today_str(fmt: str = "%Y%m%d") -> str:
        """获取今日日期字符串"""
        return datetime.now().strftime(fmt)

    @staticmethod
    def get_date_range(start_date: str, end_date: str) -> List[str]:
        """
        获取日期范围内的所有日期

        Args:
            start_date: 开始日期，格式YYYYMMDD
            end_date: 结束日期，格式YYYYMMDD

        Returns:
            日期字符串列表（包含首尾）
        """
        dates = []
        current = DateUtils.parse_date(start_date)
        end = DateUtils.parse_date(end_date)

        if not current or not end:
            return []

        while current <= end:
            dates.append(DateUtils.format_date(current))
            current += timedelta(days=1)

        return dates

    @staticmethod
    def is_weekend(date_str: str) -> bool:
        """
        判断是否为周末

        Args:
            date_str: 日期字符串，格式YYYYMMDD

        Returns:
            是否为周末
        """
        date = DateUtils.parse_date(date_str)
        if not date:
            return False
        return date.weekday() >= 5  # 周六=5, 周日=6

    def is_trade_date(self, date: str) -> bool:
        """判断是否为交易日"""
        return date in self._prev_map

    def get_nearest_trade_date(self, date: str) -> str:
        """
        获取最近的交易日（向前查找，即 ≤ date 的最近交易日）
        主要用于非交易日（如周末）获取前一交易日
        """
        if self.is_trade_date(date):
            return date
        # 二分查找最近的前一个交易日
        for i, td in enumerate(self._trade_dates):
            if td >= date:
                if i == 0:
                    return td  # date 早于最早交易日，返回最早交易日
                return self._trade_dates[i-1]
        # date 晚于最晚交易日，返回最晚交易日
        return self._trade_dates[-1]

    def get_prev_trade_date(self, date: str) -> str:
        """
        获取上一交易日
        - 若 date 是交易日：返回其前一交易日
        - 若 date 非交易日：先找到最近的前一个交易日，再返回该日的前一交易日
        """
        if not self.is_trade_date(date):
            # 非交易日：先找到最近的前一个交易日，再取其前一交易日
            date = self.get_nearest_trade_date(date)
        # 返回该交易日的前一交易日
        return self._prev_map.get(date, date)

    def get_next_trade_date(self, date: str) -> str:
        """
        获取下一交易日
        - 若 date 是交易日：返回其后一交易日
        - 若 date 非交易日：返回最近的后一个交易日
        """
        if not self.is_trade_date(date):
            # 非交易日：找到最近的后一个交易日
            for td in self._trade_dates:
                if td > date:
                    return td
            return date  # 晚于最晚交易日，返回原日期
        # 交易日：返回其后一交易日
        return self._next_map.get(date, date)

    def get_last_n_trade_dates(self, n: int, end_date: Optional[str] = None) -> List[str]:
        """
        获取最近 N 个交易日，包含 end_date（如果 end_date 是交易日）
        若 end_date 非交易日，则取其最近交易日作为结束点
        返回列表按日期倒序（最新在前）
        """
        if end_date is None:
            end_date = self.get_today_str()
        # 确保 end_date 是交易日
        if not self.is_trade_date(end_date):
            end_date = self.get_nearest_trade_date(end_date)

        # 在有序列表中找到 end_date 的位置，向前取 N 个
        try:
            idx = self._trade_dates.index(end_date)
        except ValueError:
            # 理论上不会发生，因为 end_date 已经是交易日
            return []
        start_idx = max(0, idx - n + 1)
        # 返回倒序列表
        return self._trade_dates[start_idx:idx+1][::-1]

    def get_n_trade_dates_before(self, n: int, date: Optional[str] = None) -> str:
        """
        获取N个交易日之前的日期（向前回溯N个交易日）

        Args:
            n: 回溯的交易日数量
            date: 起始日期，格式YYYYMMDD，默认为今日

        Returns:
            N个交易日之前的日期字符串
        """
        if date is None:
            date = self.get_today_str()

        # 确保起始日期是交易日
        if not self.is_trade_date(date):
            date = self.get_nearest_trade_date(date)

        # 在有序列表中找到起始日期的位置，向前回溯N个交易日
        try:
            idx = self._trade_dates.index(date)
        except ValueError:
            # 理论上不会发生，因为 date 已经是交易日
            return date

        target_idx = idx - n
        if target_idx < 0:
            # 如果N个交易日前超出最早交易日，返回最早交易日
            return self._trade_dates[0]

        return self._trade_dates[target_idx]


# 创建单例实例
_date_utils = DateUtils()

# 保持向后兼容的函数接口
parse_date = DateUtils.parse_date
format_date = DateUtils.format_date
get_today_str = DateUtils.get_today_str
get_date_range = DateUtils.get_date_range
is_weekend = DateUtils.is_weekend
is_trade_date = _date_utils.is_trade_date
get_nearest_trade_date = _date_utils.get_nearest_trade_date
get_prev_trade_date = _date_utils.get_prev_trade_date
get_next_trade_date = _date_utils.get_next_trade_date
get_last_n_trade_dates = _date_utils.get_last_n_trade_dates
get_n_trade_dates_before = _date_utils.get_n_trade_dates_before


# ==================== 简单测试 ====================
if __name__ == "__main__":
    utils = DateUtils()
    print(f"今日: {utils.get_today_str()}")
    test_date = "20260104"  # 示例周日
    print(f"{test_date} 是否为交易日: {utils.is_trade_date(test_date)}")
    print(f"{test_date} 最近交易日: {utils.get_nearest_trade_date(test_date)}")
    print(f"20260418 上一交易日: {utils.get_prev_trade_date('20260418')}")
    print(f"20260105 下一交易日: {utils.get_next_trade_date('20260105')}")
    print(f"最近5个交易日: {utils.get_last_n_trade_dates(5)}")
