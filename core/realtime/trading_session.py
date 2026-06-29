"""Realtime quote refresh window for the A-share market."""
from __future__ import annotations

from datetime import datetime, time as clock_time, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, Optional

from backtest.trade_calendar import TradeCalendar


SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MARKET_OPEN = clock_time(9, 30)
MARKET_CLOSE = clock_time(15, 0)


@lru_cache(maxsize=1)
def _trade_calendar() -> TradeCalendar:
    return TradeCalendar()


def _shanghai_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _at_market_open(date_text: str) -> datetime:
    value = datetime.strptime(date_text, "%Y%m%d")
    return value.replace(
        hour=MARKET_OPEN.hour,
        minute=MARKET_OPEN.minute,
        tzinfo=SHANGHAI_TZ,
    )


def realtime_session_status(
    now: Optional[datetime] = None,
    *,
    calendar: Optional[TradeCalendar] = None,
) -> Dict[str, Any]:
    """Return whether automatic realtime refresh is currently allowed."""
    current = _shanghai_now(now)
    cal = calendar or _trade_calendar()
    trade_date = current.strftime("%Y%m%d")
    is_trade_date = bool(cal.is_trade_date(trade_date))
    local_time = current.timetz().replace(tzinfo=None)
    is_open = is_trade_date and MARKET_OPEN <= local_time <= MARKET_CLOSE

    if not is_trade_date:
        reason = "非交易日"
    elif local_time < MARKET_OPEN:
        reason = "未开盘"
    elif local_time > MARKET_CLOSE:
        reason = "已收盘"
    else:
        reason = "交易中"

    market_open_at = _at_market_open(trade_date)
    market_close_at = market_open_at.replace(
        hour=MARKET_CLOSE.hour,
        minute=MARKET_CLOSE.minute,
    )
    if is_trade_date and local_time < MARKET_OPEN:
        next_open = market_open_at
    else:
        next_date = cal.next(trade_date)
        if not next_date or next_date <= trade_date:
            next_date = (current + timedelta(days=1)).strftime("%Y%m%d")
            while not cal.is_trade_date(next_date):
                next_date = (
                    datetime.strptime(next_date, "%Y%m%d") + timedelta(days=1)
                ).strftime("%Y%m%d")
        next_open = _at_market_open(next_date)

    return {
        "timezone": "Asia/Shanghai",
        "date": trade_date,
        "time": current.strftime("%H:%M:%S"),
        "is_trade_date": is_trade_date,
        "is_open": is_open,
        "reason": reason,
        "refresh_window": "交易日 09:30-15:00",
        "market_close_at": market_close_at.isoformat(),
        "next_open_at": next_open.isoformat(),
        "seconds_until_next_open": max(0, int((next_open - current).total_seconds())),
    }


__all__ = ["realtime_session_status"]
