"""Inspect cached TDX boards or archived sampled minutes without market requests."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import ELTDX_BOARD_POLL_SECONDS, ELTDX_BOARD_STALE_SECONDS, REALTIME_QUOTE_STALE_SECONDS
    from core.realtime.board_diagnostics import board_shadow_report
    from core.realtime.board_plan import BoardCollectionPlan
    from core.realtime.board_quote_store import BoardQuoteStore, aggregate_board_minutes
    from core.realtime.collection_plan import DailyCollectionPlan
    from core.realtime.trading_session import shanghai_now
    parser = argparse.ArgumentParser(description="只读通达信板块影子诊断，不补取行情、不产生信号")
    parser.add_argument("--market-date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--archive", action="store_true", help="读取已归档及待归档快照，聚合采样分钟")
    args = parser.parse_args()
    store = BoardQuoteStore(stale_seconds=ELTDX_BOARD_STALE_SECONDS)
    if args.archive:
        result = {"mode": "archived_board_samples", "formal_eligible": False,
                  "minutes": aggregate_board_minutes(store.archived_points(args.market_date))}
    else:
        parent = DailyCollectionPlan().load(args.market_date)
        plan = BoardCollectionPlan().load(args.market_date, parent_id=parent["plan_id"] if parent else None)
        result = board_shadow_report(parent, plan, store, interval=ELTDX_BOARD_POLL_SECONDS,
                                     member_stale_seconds=REALTIME_QUOTE_STALE_SECONDS)
        result["collector_health"] = store.backend.get_json("realtime:tdx-board:health") or {}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
