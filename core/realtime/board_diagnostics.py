"""Read-only, explicitly non-synchronous THS breadth versus TDX index audit."""
from __future__ import annotations

from datetime import datetime

from core.realtime.board_quote_store import positive
from core.realtime.trading_session import shanghai_now


def board_shadow_report(parent, plan, store, *, now=None, interval=10, member_stale_seconds=12):
    clock = shanghai_now(now)
    base = {"formal_signal": False, "notification_sent": False, "formal_eligible": False,
            "generated_at": clock.isoformat(), "mode": "tdx_board_shadow"}
    if not parent or not plan:
        return {**base, "status": "missing_collection_plan" if not parent else "missing_board_plan", "rows": []}
    if parent["plan_id"] != plan["collection_plan_id"] or parent["market_date"] != plan["market_date"]:
        raise ValueError("Board diagnostics require matching frozen collection plans")
    boards = store.read(plan, now=clock, expected_interval=interval)
    members = sorted({code for sector in parent["sectors"] for code in sector["members"]})
    quotes = store.backend.get_many_json([f"realtime:quotes:latest:{code}" for code in members])
    rows = []
    for mapping in plan["rows"]:
        sector = next(row for row in parent["sectors"] if row["name"] == mapping["ths_name"])
        observed, timestamps = [], []
        for code in sector["members"]:
            raw = quotes.get(f"realtime:quotes:latest:{code}") or {}
            try:
                stamp = shanghai_now(datetime.fromisoformat(raw["received_at"]))
            except (KeyError, ValueError, TypeError):
                continue
            last, prev = positive(raw.get("last_price")), positive(raw.get("pre_close"))
            if (raw.get("date") != parent["market_date"] or stamp.strftime("%Y%m%d") != parent["market_date"]
                    or raw.get("source") != "eltdx_batch" or raw.get("quality_ok") is False
                    or raw.get("is_stale") or not 0 <= (clock - stamp).total_seconds() <= member_stale_seconds
                    or not last or not prev):
                continue
            observed.append((last / prev - 1) * 100)
            timestamps.append(stamp.isoformat())
        rows.append({**mapping, "tdx_quotes": [{"code": candidate["code"], **boards[candidate["code"]]}
                                              for candidate in mapping["candidates"]],
                     "ths_breadth": {"member_count": len(sector["members"]), "observed_count": len(observed),
                         "coverage": len(observed) / len(sector["members"]) if sector["members"] else 0,
                         "advancing_ratio": sum(change > 0 for change in observed) / len(observed) if observed else None,
                         "average_change_pct": sum(observed) / len(observed) if observed else None,
                         "earliest_received_at": min(timestamps) if timestamps else "",
                         "latest_received_at": max(timestamps) if timestamps else ""},
                     "time_alignment": "independent_latest_observations_not_formal_resonance"})
    return {**base, "status": "shadow_only", "market_date": parent["market_date"], "rows": rows,
            "board_plan_id": plan["plan_id"], "warnings": plan["warnings"]}
