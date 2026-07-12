"""Run Phase 2 factor jobs from silver tables in DuckDB.

Example:
    .venv\\Scripts\\python.exe scripts\\run_factor_jobs.py --date 20260616
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import FACTOR_DB_PATH
from core.factors.jobs.runner import run_factor_jobs
from core.utils.date_utils import DateUtils


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase 2 market/sector/stock factor jobs.")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD；缺省取最近交易日")
    parser.add_argument("--start", default="", help="批量开始交易日 YYYYMMDD")
    parser.add_argument("--end", default="", help="批量结束交易日 YYYYMMDD")
    parser.add_argument("--jobs", default="market,sector,stock", help="逗号分隔: market,sector,stock")
    parser.add_argument("--duckdb-path", default=str(FACTOR_DB_PATH))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    du = DateUtils()
    jobs = [x.strip() for x in str(args.jobs).split(",") if x.strip()]
    if args.start or args.end:
        if not args.start or not args.end or args.start > args.end:
            raise SystemExit("批量模式必须同时提供有效的 --start 和 --end")
        import duckdb

        with duckdb.connect(str(args.duckdb_path), read_only=True) as con:
            dates = [
                str(row[0]) for row in con.execute(
                    "SELECT DISTINCT CAST(trade_date AS VARCHAR) FROM stock_daily_silver "
                    "WHERE CAST(trade_date AS VARCHAR) BETWEEN ? AND ? ORDER BY 1",
                    [str(args.start), str(args.end)],
                ).fetchall()
            ]
    else:
        dates = [args.date or du.get_nearest_trade_date(DateUtils.get_today_str())]
    batches = []
    for trade_date in dates:
        results = run_factor_jobs(
            trade_date,
            duckdb_path=Path(args.duckdb_path),
            jobs=jobs,
        )
        batches.append({"trade_date": trade_date, "results": results})
    summary = {
        "trade_date": dates[0] if len(dates) == 1 else "",
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "date_count": len(dates),
        "duckdb_path": args.duckdb_path,
        "results": batches[0]["results"] if len(batches) == 1 else [],
        "batches": batches if len(batches) > 1 else [],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(
        item.get("ok") for batch in batches for item in batch["results"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
