"""Isolated post-close acquisition. Never calculate factors or run strategies."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def collect(date=None, *, force_refresh=False, skip_existing=True):
    from backtest.trade_calendar import TradeCalendar
    from config.settings import CACHE_DIR, TUSHARE_TOKEN
    from core.data.data_manager_main import DataManager
    from core.etl.daily_pipeline import ETLDailyPipeline
    from core.operations.collection_center import date_key
    from core.realtime.trading_session import shanghai_now
    calendar = TradeCalendar()
    if not date:
        now = shanghai_now()
        today = now.strftime("%Y%m%d")
        from core.etl.stage_status import post_close_data_ready
        date = today if calendar.is_trade_date(today) and post_close_data_ready(today) else calendar.prev(today)
    date = date_key(date)
    if date > shanghai_now().strftime("%Y%m%d"):
        raise ValueError("不能采集未来交易日的盘后行情")
    if not calendar.is_real or not calendar.is_trade_date(date):
        raise ValueError("盘后取数需要真实交易日历和交易日")
    result = ETLDailyPipeline(DataManager(TUSHARE_TOKEN, CACHE_DIR)).fetch_data(date, calendar.prev(date),
        force_refresh=force_refresh, skip_existing=skip_existing)
    if not result.ok:
        raise RuntimeError(result.silver_summary.get("message") or "盘后原始数据未就绪")
    return result


def main():
    parser = argparse.ArgumentParser(description="独立盘后行情采集")
    parser.add_argument("--date")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--no-skip-existing", action="store_true")
    args = parser.parse_args()
    collect(args.date, force_refresh=args.force_refresh, skip_existing=not args.no_skip_existing)


if __name__ == "__main__":
    main()
