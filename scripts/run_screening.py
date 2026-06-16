"""Run Phase 3 screening from Phase 2 gold factor tables.

Example:
    .venv\\Scripts\\python.exe scripts\\run_screening.py --date 20260616 --profile default
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR
from core.screening.screening_engine import run_screening
from core.utils.date_utils import DateUtils


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase 3 configurable screening.")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD；缺省取最近交易日")
    parser.add_argument("--profile", default="default", help="screening profile 名称")
    parser.add_argument("--duckdb-path", default=str(FACTOR_DB_PATH))
    parser.add_argument("--output-dir", default=str(WEB_DATA_DIR / "screening"))
    parser.add_argument("--no-persist", action="store_true", help="只打印结果，不写 webdata/screening")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    du = DateUtils()
    trade_date = args.date or du.get_nearest_trade_date(DateUtils.get_today_str())
    result = run_screening(
        trade_date,
        profile=args.profile,
        duckdb_path=Path(args.duckdb_path),
        output_dir=Path(args.output_dir),
        persist=not args.no_persist,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
