"""
回测用真实交易日历（B-1a）

原 ``BacktestEngine`` 用 ``weekday < 5`` 凑交易日——节假日全被当成交易日，导致：
- 把停盘的法定节假日纳入回测，产生虚假"无成交"日；
- 前一交易日 / 持仓天数算错。

本模块封装项目已有的 ``core.utils.date_utils.DateUtils``（它从
``data/trade_calendar.csv`` 加载真实 A 股交易日历），对回测侧提供干净接口；
当日历文件缺失时优雅降级回 ``weekday`` 规则，保证不抛异常。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import loguru

logger = loguru.logger


class TradeCalendar:
    """交易日历（优先真实日历，缺失则按工作日近似）。"""

    def __init__(self, date_utils=None):
        self._du = date_utils
        self._available = False
        if self._du is None:
            try:
                from core.utils.date_utils import DateUtils

                self._du = DateUtils()
                self._available = True
            except Exception as e:  # pragma: no cover - 日历缺失降级
                logger.warning(f"[TradeCalendar] 真实交易日历不可用，降级为工作日近似: {e}")
                self._du = None
                self._available = False
        else:
            self._available = True

    @property
    def is_real(self) -> bool:
        """是否使用真实交易日历（False = 工作日近似降级）。"""
        return self._available

    # ------------------------------------------------------------------
    def is_trade_date(self, date: str) -> bool:
        if self._available:
            try:
                return self._du.is_trade_date(date)
            except Exception:
                pass
        return not self._is_weekend(date)

    def get_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """返回 [start, end] 闭区间内的所有交易日（升序）。"""
        if end_date < start_date:
            return []
        if self._available:
            try:
                from core.utils.date_utils import DateUtils

                all_days = DateUtils.get_date_range(start_date, end_date)
                return [d for d in all_days if self._du.is_trade_date(d)]
            except Exception as e:  # pragma: no cover
                logger.warning(f"[TradeCalendar] 取交易日列表失败，降级工作日: {e}")
        # 降级：工作日
        return [d for d in self._date_range(start_date, end_date) if not self._is_weekend(d)]

    def prev(self, date: str) -> str:
        """前一交易日。"""
        if self._available:
            try:
                return self._du.get_prev_trade_date(date)
            except Exception:
                pass
        return self._weekday_shift(date, -1)

    def next(self, date: str) -> str:
        """下一交易日。"""
        if self._available:
            try:
                return self._du.get_next_trade_date(date)
            except Exception:
                pass
        return self._weekday_shift(date, +1)

    def offset(self, date: str, n: int) -> str:
        """从 date 偏移 n 个交易日（n>0 向后，n<0 向前，n=0 原样）。"""
        if n == 0:
            return date
        step = self.next if n > 0 else self.prev
        cur = date
        for _ in range(abs(n)):
            cur = step(cur)
        return cur

    def holding_days(self, entry_date: str, exit_date: str) -> int:
        """两个日期之间的交易日跨度（持仓天数）。"""
        if not entry_date or not exit_date or exit_date <= entry_date:
            return 0
        if self._available:
            try:
                return max(0, len(self.get_trade_dates(entry_date, exit_date)) - 1)
            except Exception:
                pass
        # 降级：自然日
        try:
            d0 = datetime.strptime(entry_date, "%Y%m%d")
            d1 = datetime.strptime(exit_date, "%Y%m%d")
            return (d1 - d0).days
        except Exception:
            return 0

    # ------------------------------------------------------------------
    @staticmethod
    def _is_weekend(date: str) -> bool:
        try:
            return datetime.strptime(date, "%Y%m%d").weekday() >= 5
        except Exception:
            return False

    @staticmethod
    def _date_range(start_date: str, end_date: str) -> List[str]:
        out = []
        try:
            cur = datetime.strptime(start_date, "%Y%m%d")
            end = datetime.strptime(end_date, "%Y%m%d")
        except Exception:
            return out
        while cur <= end:
            out.append(cur.strftime("%Y%m%d"))
            cur += timedelta(days=1)
        return out

    def _weekday_shift(self, date: str, direction: int) -> str:
        try:
            cur = datetime.strptime(date, "%Y%m%d")
        except Exception:
            return date
        cur += timedelta(days=direction)
        while cur.weekday() >= 5:
            cur += timedelta(days=direction)
        return cur.strftime("%Y%m%d")


__all__ = ["TradeCalendar"]
