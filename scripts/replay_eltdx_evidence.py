"""Read-only diagnostics for live evidence or explicitly isolated research data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from datetime import datetime

    from config.settings import WEB_DATA_DIR
    from core.realtime.collection_plan import digest
    from core.realtime.evidence_replay import load_evidence_events, replay_evidence
    from core.realtime.trading_session import shanghai_now

    parser = argparse.ArgumentParser(description="只读eltdx补充证据回放；不联网、不发通知、不修改账户")
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--until", default="15:01:00")
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--research-backfill", action="store_true")
    args = parser.parse_args()
    end = shanghai_now(datetime.strptime(args.market_date + args.until, "%Y%m%d%H:%M:%S"))
    plan_id, codes = "", None
    if not args.research_backfill:
        path = args.data_root / "collection_plans" / f"{args.market_date}.json"
        if not path.exists():
            print(json.dumps(dict(ok=False, reason="missing_collection_plan", remote_fetch_performed=False)))
            return 1
        plan = json.loads(path.read_text(encoding="utf-8"))
        body = {k: v for k, v in plan.items() if k != "plan_id"}
        if digest(body) != plan["plan_id"] or plan["market_date"] != args.market_date:
            raise ValueError("Invalid frozen plan")
        plan_id, codes = plan["plan_id"], [row["code"] for row in plan["rows"]]
    events, failures = load_evidence_events(args.data_root, args.market_date, research_backfill=args.research_backfill)
    report = replay_evidence(
        events,
        date=args.market_date,
        until=end.isoformat(),
        plan_id=plan_id,
        codes=codes,
        research_backfill=args.research_backfill,
    )
    report["failures"] = failures + report["failures"]
    report["ok"] = bool(events) and not report["failures"]
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
