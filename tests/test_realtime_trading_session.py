from datetime import datetime

import pytest

from core.realtime.trading_session import SHANGHAI_TZ, realtime_session_status


class StubCalendar:
    def __init__(self, trade_dates):
        self.trade_dates = sorted(trade_dates)

    def is_trade_date(self, date):
        return date in self.trade_dates

    def next(self, date):
        return next(item for item in self.trade_dates if item > date)


CALENDAR = StubCalendar(["20260629", "20260630"])


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected", "reason"),
    [
        (9, 29, 59, False, "未开盘"),
        (9, 30, 0, True, "交易中"),
        (14, 59, 59, True, "交易中"),
        (15, 0, 0, True, "交易中"),
        (15, 0, 1, False, "已收盘"),
    ],
)
def test_realtime_refresh_window(hour, minute, second, expected, reason):
    status = realtime_session_status(
        datetime(2026, 6, 29, hour, minute, second, tzinfo=SHANGHAI_TZ),
        calendar=CALENDAR,
    )

    assert status["is_open"] is expected
    assert status["reason"] == reason


def test_before_open_uses_same_day_as_next_open():
    status = realtime_session_status(
        datetime(2026, 6, 29, 8, 0, tzinfo=SHANGHAI_TZ),
        calendar=CALENDAR,
    )

    assert status["next_open_at"].startswith("2026-06-29T09:30:00")


def test_closed_and_non_trade_day_use_next_trade_date():
    closed = realtime_session_status(
        datetime(2026, 6, 29, 16, 0, tzinfo=SHANGHAI_TZ),
        calendar=CALENDAR,
    )
    holiday = realtime_session_status(
        datetime(2026, 6, 28, 10, 0, tzinfo=SHANGHAI_TZ),
        calendar=CALENDAR,
    )

    assert closed["next_open_at"].startswith("2026-06-30T09:30:00")
    assert holiday["reason"] == "非交易日"
    assert holiday["next_open_at"].startswith("2026-06-29T09:30:00")
