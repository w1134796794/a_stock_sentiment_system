"""Fetch authoritative suspension evidence without replaying or changing accounts."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.trade_calendar import TradeCalendar
from config.settings import CACHE_DIR, TUSHARE_TOKEN
from core.data.data_manager_main import DataManager


def main() -> int:
    parser = argparse.ArgumentParser(description='补齐suspend_d停复牌日缓存，不运行模拟交易')
    parser.add_argument('--start', required=True, help='YYYYMMDD')
    parser.add_argument('--end', required=True, help='YYYYMMDD')
    parser.add_argument('--force-refresh', action='store_true')
    args = parser.parse_args()
    calendar = TradeCalendar()
    if not calendar.is_real:
        parser.error('真实交易日历不可用')
    dates = calendar.get_trade_dates(args.start, args.end)
    if not dates or args.start > args.end:
        parser.error('日期范围无交易日或起止顺序无效')
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
    failures = []
    for index, date in enumerate(dates, start=1):
        result = dm.get_daily_suspensions(date, force_refresh=args.force_refresh)
        print(f"{index}/{len(dates)} {date}: " +
              (f"ok, records={len(result['rows'])}" if result['ok'] else f"failed: {result['reason']}"))
        if not result['ok']:
            failures.append(date)
        time.sleep(0.4)
    print(f"complete: days={len(dates)}, failed={len(failures)}, cache={dm.stock_dir / 'suspend_d'}")
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
