"""Read-only price cross-check against an explicitly unadjusted reference CSV."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import WEB_DATA_DIR
    from core.realtime.evidence_views import valid_code
    from core.realtime.market_evidence import fingerprint, number
    from core.realtime.research_jobs import audit_prices
    from core.realtime.research_store import ResearchStore
    from core.realtime.trading_session import shanghai_now

    parser = argparse.ArgumentParser(description="只读核对通达信不复权日线与本地CSV，不修复或覆盖源数据")
    parser.add_argument("--code", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-adjust", choices=["none"], required=True)
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    code = valid_code(args.code)
    store = ResearchStore(args.data_root)
    versions = store.read(code, "bars_raw", as_of=args.as_of)
    if not versions or versions[0]["quality_status"] != "ok" or versions[0]["period"] != "day":
        raise ValueError("No valid unadjusted daily research version")
    reference = []
    with args.reference.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            symbol = row.get("ts_code") or row.get("code")
            if not symbol or valid_code(symbol) != code:
                continue
            day = str(row.get("trade_date") or row.get("date") or "").replace("-", "")
            at = shanghai_now(datetime.strptime(day, "%Y%m%d")).isoformat()
            prices = {key: number(row.get(key)) for key in ("open", "high", "low", "close")}
            if any(value is None or value <= 0 for value in prices.values()):
                raise ValueError("Invalid reference price")
            reference.append(dict(date=day, event_time=at, **prices))
    baseline = dict(code=code, adjust="none", period="day", rows=reference)
    actions = store.read(code, "corporate_actions", as_of=args.as_of)
    report = audit_prices(versions[0], baseline, actions[0] if actions else None)
    report.update(
        eltdx_version_id=versions[0]["version_id"],
        reference_hash=fingerprint(baseline),
        reference_rows=len(reference),
        remote_fetch_performed=False,
    )
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["compared_count"] and not report["differences"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
