"""Low frequency, versioned research capture; default dry run."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import ELTDX_HOST, ELTDX_TIMEOUT_SECONDS, WEB_DATA_DIR
    from core.data.providers.eltdx_provider import EltdxProvider
    from core.data.providers.eltdx_research import PERIODS, RESEARCH_KINDS
    from core.realtime.models import normalize_stock_code
    from core.realtime.research_jobs import run_research

    parser = argparse.ArgumentParser(description="版本化低频研究取数，默认预演，不写生产行情或Redis")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--codes")
    scope.add_argument("--collection-date", help="只使用该日已冻结名单，不扩大股票池")
    parser.add_argument(
        "--kinds", default="bars_raw,corporate_actions,money_flow,company_profile,announcements,finance_report,topics"
    )
    parser.add_argument("--period", choices=sorted(PERIODS), default="day")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if (
        datetime.strptime(args.start, "%Y%m%d") > datetime.strptime(args.end, "%Y%m%d")
        or not 1 <= args.max_pages <= 100
    ):
        parser.error("Invalid range or page budget")
    kinds = list(dict.fromkeys(args.kinds.split(",")))
    if not kinds or set(kinds) - RESEARCH_KINDS:
        parser.error("Unknown research kind")
    if args.collection_date:
        from core.realtime.collection_plan import digest

        datetime.strptime(args.collection_date, "%Y%m%d")
        plan = json.loads(
            (args.data_root / "collection_plans" / f"{args.collection_date}.json").read_text(encoding="utf-8")
        )
        if (
            plan.get("market_date") != args.collection_date
            or digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan["plan_id"]
        ):
            raise ValueError("Invalid frozen collection plan")
        codes = [row["code"] for row in plan["rows"]]
    else:
        codes = list(dict.fromkeys(normalize_stock_code(code.strip()) for code in args.codes.split(",")))
    if not codes or len(codes) > 100 or any(len(code) != 6 or not code.isdigit() for code in codes):
        parser.error("Expected 1-100 valid security codes")
    if not args.apply:
        print(
            json.dumps(
                dict(
                    dry_run=True,
                    codes=codes,
                    kinds=kinds,
                    tasks=len(codes) * len(kinds),
                    production_data_modified=False,
                )
            )
        )
        return 0
    report = run_research(
        args.data_root,
        EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None),
        codes=codes,
        kinds=kinds,
        start=args.start,
        end=args.end,
        period=args.period,
        max_pages=args.max_pages,
        refresh=args.refresh,
    )
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report["state"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
