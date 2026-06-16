"""Run Phase 1 ETL prefetch + silver persistence for one trade date.

Example:
    python scripts/run_etl_daily.py --date 20260616
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import CACHE_DIR, FACTOR_DB_PATH, TUSHARE_TOKEN, WEB_DATA_DIR
from core.data.data_manager_main import DataManager
from core.data.data_prep import DataPrep
from core.utils.date_utils import DateUtils


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase 1 ETL and write silver tables.")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD；缺省取最近交易日")
    parser.add_argument("--prev-date", default="", help="上一交易日 YYYYMMDD；缺省自动计算")
    parser.add_argument("--daily-lookback-calendar-days", type=int, default=120)
    parser.add_argument("--limit-up-history-days", type=int, default=16)
    parser.add_argument("--sector-history-days", type=int, default=10)
    parser.add_argument("--prefetch-auction", action="store_true", help="是否预取集合竞价")
    parser.add_argument("--skip-sectors", action="store_true", help="跳过板块预取")
    parser.add_argument("--skip-all-daily", action="store_true", help="跳过全市场日线预取")
    parser.add_argument("--warehouse-path", default=str(FACTOR_DB_PATH))
    parser.add_argument("--silver-dir", default=str(WEB_DATA_DIR / "warehouse" / "silver"))
    parser.add_argument("--quality-dir", default=str(WEB_DATA_DIR / "etl_quality"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    du = DateUtils()
    trade_date = args.date or du.get_nearest_trade_date(DateUtils.get_today_str())
    prev_date = args.prev_date or du.get_prev_trade_date(trade_date)

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    prep = DataPrep(dm)

    zt_pool = dm.get_limit_up_pool(trade_date) if hasattr(dm, "get_limit_up_pool") else None
    prev_zt_pool = dm.get_limit_up_pool(prev_date) if hasattr(dm, "get_limit_up_pool") else None

    ds = prep.build(
        trade_date,
        prev_date,
        zt_pool=zt_pool,
        prev_zt_pool=prev_zt_pool,
        daily_lookback_calendar_days=args.daily_lookback_calendar_days,
        limit_up_history_days=args.limit_up_history_days,
        prefetch_all_daily=not args.skip_all_daily,
        prefetch_auction=args.prefetch_auction,
        prefetch_sectors=not args.skip_sectors,
        sector_history_days=args.sector_history_days,
        persist_silver=True,
        warehouse_path=Path(args.warehouse_path),
        silver_dir=Path(args.silver_dir),
        quality_dir=Path(args.quality_dir),
    )

    summary = {
        "trade_date": trade_date,
        "prev_date": prev_date,
        "dataset": ds.summary(),
        "silver": ds.meta.get("silver_persist") or ds.meta.get("silver_persist_error"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
