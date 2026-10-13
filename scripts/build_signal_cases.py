"""Build/resume independent signal cases. Never alters a portfolio or sends notifications."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from backtest.trade_calendar import TradeCalendar
    from config.settings import CACHE_DIR, FACTOR_DB_PATH, HOLDING_DB_PATH, TUSHARE_TOKEN, WEB_DATA_DIR
    from core.data.data_manager_main import DataManager
    from core.signals.case_outcomes import CaseDataSource, SignalCaseBuilder
    from core.signals.case_repository import SignalCaseRepository

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="First market date, YYYYMMDD")
    parser.add_argument("--end", help="Last market date, YYYYMMDD")
    parser.add_argument("--db", type=Path, default=FACTOR_DB_PATH)
    parser.add_argument("--web-data-dir", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--fetch-missing", action="store_true", help="Allow remote fetch for missing required history")
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    repository = SignalCaseRepository(args.db)
    if args.coverage_only:
        report = repository.coverage()
    else:
        if not args.start or not args.end:
            parser.error("--start and --end are required unless --coverage-only")
        for value in (args.start, args.end):
            datetime.strptime(value, "%Y%m%d")
        if args.start > args.end or args.end > datetime.now().strftime("%Y%m%d"):
            parser.error("Invalid or future date range")
        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=args.fetch_missing)
        builder = SignalCaseBuilder(CaseDataSource(dm, fetch_missing=args.fetch_missing),
                                    repository=repository, calendar=TradeCalendar())
        repository.import_staged(HOLDING_DB_PATH, through_date=args.end)
        days = []
        for date in builder.calendar.get_trade_dates(args.start, args.end):
            try:
                result = builder.run_day(builder.calendar.prev(date), date, web_data_dir=args.web_data_dir)
                days.append({"market_date": date, "ok": True, "recorded": result["recorded"]})
            except (OSError, ValueError, RuntimeError) as exc:
                days.append({"market_date": date, "ok": False, "reason": str(exc)})
        report = {"days": days, **repository.coverage()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        pending = args.report.with_suffix(args.report.suffix + ".tmp")
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(args.report)
    if (any(not day["ok"] for day in report.get("days", []))
            or report.get("progress", {}).get("blocked")
            or report.get("progress", {}).get("initialization_blocked")
            or report.get("attempts", {}).get("blocked")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
