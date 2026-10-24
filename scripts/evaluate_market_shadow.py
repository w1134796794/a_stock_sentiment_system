"""Assess stored snapshot continuity without evaluation, providers or notifications."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from core.realtime.collection_plan import DailyCollectionPlan
    from core.realtime.snapshot_repository import RealtimeSnapshotRepository
    from core.realtime.shadow_assessment import assess_snapshot_sequence
    from core.realtime.trading_session import shanghai_now
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-date", default=shanghai_now().strftime("%Y%m%d"))
    args = parser.parse_args()
    plan = DailyCollectionPlan().load(args.market_date)
    repository = RealtimeSnapshotRepository()
    now = shanghai_now()
    report = {"classification": "shadow_feasibility_only", "market_date": args.market_date,
              "generated_at": now.isoformat(), "plan_available": bool(plan),
              "formal_rules_modified": False, "notification_sent": False, "rows": []}
    for row in (plan or {}).get("rows", []):
        report["rows"].append({"code": row["code"], **assess_snapshot_sequence(
            repository.read(args.market_date, row["code"]), args.market_date, now=now)})
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
