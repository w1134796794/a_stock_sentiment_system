"""Shared sector synchrony rules for live quotes and diagnostic minutes."""
from __future__ import annotations

import math
from collections.abc import Mapping

SECTOR_SYNC_VERSION = "ths_red_or_open_rising_3m_v2"
TREND_WINDOW_MINUTES = 3
MIN_COVERAGE = 0.6
MIN_OBSERVATIONS = 2


def finite_number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def assess_sector_sync(*, complete, breadth, average_change, index_positive,
                       opening_changes: Mapping, earlier_changes: Mapping, expected_members: int):
    common = sorted(set(opening_changes) & set(earlier_changes))
    pairs = [(finite_number(opening_changes[code]), finite_number(earlier_changes[code])) for code in common]
    pairs = [(now, before) for now, before in pairs if now is not None and before is not None]
    coverage = len(pairs) / expected_members if expected_members > 0 else 0.0
    current = sum(now for now, _ in pairs) / len(pairs) if pairs else None
    earlier = sum(before for _, before in pairs) / len(pairs) if pairs else None
    delta = current - earlier if pairs else None
    ready = len(pairs) >= MIN_OBSERVATIONS and coverage >= MIN_COVERAGE
    complete = bool(complete and finite_number(breadth) is not None
                    and finite_number(average_change) is not None and index_positive is not None)
    red = complete and breadth >= 0.55 and average_change >= 0 and index_positive
    rising = complete and ready and current > 0 and delta > 0
    path = "red_breadth" if red else ("open_rising_3m" if rising else "")
    state = bool(red or rising) if complete else None
    reason = ("板块红盘广度与指数同步" if red else "板块相对开盘走强，最近3分钟净抬升") if path else (
        "板块同步不足" if complete else "板块实时证据覆盖不足")
    return state, {
        "sector_sync_policy_version": SECTOR_SYNC_VERSION,
        "confirmation_path": path, "legacy_breadth_passed": bool(red),
        "opening_trend_passed": bool(rising), "opening_trend_available": ready,
        "open_gain_pct": current, "three_minute_change_pct": delta,
        "trend_observed_members": len(pairs), "trend_coverage": round(coverage, 4),
        "trend_window_minutes": TREND_WINDOW_MINUTES, "reason": reason,
    }
