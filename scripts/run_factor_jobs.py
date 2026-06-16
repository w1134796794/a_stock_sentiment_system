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
    parser.add_argument("--jobs", default="market,sector,stock", help="逗号分隔: market,sector,stock")
    parser.add_argument("--duckdb-path", default=str(FACTOR_DB_PATH))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    du = DateUtils()
    trade_date = args.date or du.get_nearest_trade_date(DateUtils.get_today_str())
    jobs = [x.strip() for x in str(args.jobs).split(",") if x.strip()]
    results = run_factor_jobs(
        trade_date,
        duckdb_path=Path(args.duckdb_path),
        jobs=jobs,
    )
    summary = {
        "trade_date": trade_date,
        "duckdb_path": args.duckdb_path,
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
