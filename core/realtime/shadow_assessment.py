"""Read-only feasibility assessment, deliberately not a buy signal or alert."""
from __future__ import annotations

from datetime import datetime

from core.realtime.trading_session import shanghai_now


def assess_snapshot_sequence(rows, market_date, *, now=None):
    clock = shanghai_now(now)
    stamps, invalid = [], 0
    for row in rows:
        try:
            stamp = shanghai_now(datetime.fromisoformat(row["received_at"]))
            if (row.get("date") != market_date or stamp.strftime("%Y%m%d") != market_date
                    or row.get("source") != "eltdx_batch" or not -1 <= (clock - stamp).total_seconds() <= 180
                    or float(row.get("last_price") or 0) <= 0):
                invalid += 1
                continue
            stamps.append(stamp)
        except (ValueError, TypeError, KeyError):
            invalid += 1
    stamps = sorted(set(stamps))
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    age = (clock - stamps[-1]).total_seconds() if stamps else None
    return {"valid_samples": len(stamps), "invalid_samples": invalid,
        "latest_age_seconds": age, "max_gap_seconds": max(gaps) if gaps else None,
        "snapshot_continuity_ready": len(stamps) >= 2 and age <= 8 and max(gaps) <= 8,
        "formal_signal": False, "notification_sent": False,
        "seconds_board_confirmation_ready": False,
        "reason": "成分股尚无连续秒级历史，不能替代正式闭合分钟板块确认"}
