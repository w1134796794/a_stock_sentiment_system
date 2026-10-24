"""Explicit, resumable research backfill. Never writes live Redis/frozen plans."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import ELTDX_HOST, ELTDX_TIMEOUT_SECONDS, WEB_DATA_DIR
    from core.data.providers.eltdx_provider import EltdxProvider
    from core.realtime.evidence_store import EvidenceStore
    from core.realtime.market_journal import MarketJournal
    from core.realtime.models import normalize_stock_code

    parser = argparse.ArgumentParser(description="eltdx历史证据补数；默认预演，--apply执行，隔离于实时归档")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--kinds", default="auction_series,opening_match,trade_batch")
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="显式重抓已完成任务，保留原有版本")
    args = parser.parse_args()
    start, end = datetime.strptime(args.start, "%Y%m%d"), datetime.strptime(args.end, "%Y%m%d")
    kinds = list(dict.fromkeys(args.kinds.split(",")))
    codes = list(dict.fromkeys(normalize_stock_code(c.strip()) for c in args.codes.split(",")))
    if end < start or any(len(c) != 6 or not c.isdigit() for c in codes):
        parser.error("日期范围或股票代码无效")
    methods = {
        "auction_series": "get_auction_evidence",
        "opening_match": "get_opening_match_evidence",
        "trade_batch": "get_trade_evidence",
    }
    if set(kinds) - methods.keys() or not 1 <= args.max_pages <= 100:
        parser.error("不支持的接口类型或分页预算")
    tasks = [
        ((start + timedelta(days=i)).strftime("%Y%m%d"), code, kind)
        for i in range((end - start).days + 1)
        for code in codes
        for kind in kinds
    ]
    if not args.apply:
        print(
            json.dumps(
                dict(
                    dry_run=True,
                    task_count=len(tasks),
                    dates=[args.start, args.end],
                    codes=codes,
                    kinds=kinds,
                    live_data_modified=False,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    journal = MarketJournal(args.data_root / "market_archive" / "backfill")
    store = EvidenceStore(journal)
    provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None)
    results = []
    for date, code, kind in tasks:
        key = f"backfill-complete:{date}:{code}:{kind}"
        if not args.refresh and journal.evidence_cursor(key).get("complete"):
            results.append(dict(date=date, code=code, kind=kind, status="skipped_complete"))
            continue
        try:
            result = getattr(provider, methods[kind])(
                code, date, **({"max_pages": args.max_pages} if kind in {"trade_batch", "opening_match"} else {})
            )
            store.write(result, capture_mode="backfill")
            journal.archive_once()
            results.append(
                dict(
                    date=date,
                    code=code,
                    kind=kind,
                    status=result.status,
                    rows=len(result.rows),
                    reasons=result.reasons,
                    coverage=result.coverage,
                )
            )
        except Exception as exc:
            results.append(dict(date=date, code=code, kind=kind, status="storage_or_request_error", reason=str(exc)))
    ok = all(r["status"] in {"ok", "empty", "skipped_complete"} for r in results)
    print(
        json.dumps(
            dict(ok=ok, tasks=results, live_data_modified=False, research_only=True),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
