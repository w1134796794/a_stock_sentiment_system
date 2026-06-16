"""Build Phase 4 slim analysis payload from Gold tables and screening output."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR
from core.screening.gold_analysis import build_gold_analysis_summary
from core.utils.date_utils import DateUtils


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Phase 4 slim analysis payload.")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD；缺省取最近交易日")
    parser.add_argument("--duckdb-path", default=str(FACTOR_DB_PATH))
    parser.add_argument("--screening-dir", default=str(WEB_DATA_DIR / "screening"))
    parser.add_argument("--output-dir", default=str(WEB_DATA_DIR / "screening"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    du = DateUtils()
    trade_date = args.date or du.get_nearest_trade_date(DateUtils.get_today_str())
    summary = build_gold_analysis_summary(
        trade_date,
        duckdb_path=Path(args.duckdb_path),
        screening_dir=Path(args.screening_dir),
    )
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"analysis_{trade_date}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary["output_path"] = str(out_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
