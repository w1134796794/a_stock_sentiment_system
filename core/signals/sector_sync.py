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


def constituent_minute_prices(frames):
    """Index immutable replay evidence without filling absent observation times."""
    return {code: dict(zip(frame['time'].astype(str), frame['close']))
            for code, frame in frames.items() if frame is not None and not frame.empty}


def make_constituent_sync_checker(*, code, names, members, frames, previous, opening_prices,
                                  cache=None, minute_prices=None):
    """Use exact minute observations of verified constituents, never peer proxies."""
    from datetime import datetime, timedelta
    names = tuple(sorted(names))
    eligible = sorted({c for name in names for c in members.get(name, []) if c != code})
    detail = {"sector_names": list(names), "member_count": len(eligible), "coverage": 0.0,
              "data_completeness": 0.0, "sector_confirmation_version": SECTOR_SYNC_VERSION,
              "sector_confirmation_source": "historical_constituent_minutes", "assessed": False}
    cache = cache if cache is not None else {}
    minute_prices = constituent_minute_prices(frames) if minute_prices is None else minute_prices

    def changes(at_time, selected, basis):
        basis = basis() if callable(basis) else basis
        values = {}
        for member in selected:
            reference = finite_number(basis.get(member))
            prices = minute_prices.get(member, {})
            if reference is None or reference <= 0 or not prices:
                continue
            price = finite_number(prices.get(at_time))
            if price is not None and price > 0:
                values[member] = (price / reference - 1) * 100
        return values

    closes = {c: row.get("close") for c, row in previous.items()}
    def checker(at_time):
        minute = str(at_time)[:8]
        key = (code, names, minute)
        if key in cache:
            state, observed = cache[key]
            detail.update(observed)
            return state
        observed = changes(minute, eligible[:80], closes)
        coverage = len(observed) / min(len(eligible), 80) if eligible else 0
        indexes = []
        for name in names:
            group = [c for c in members.get(name, []) if c != code]
            values = changes(minute, group, closes)
            if group and len(values) / len(group) >= MIN_COVERAGE:
                indexes.append(sum(values.values()) / len(values))
        breadth = sum(v > 0 for v in observed.values()) / len(observed) if observed else None
        average = sum(observed.values()) / len(observed) if observed else None
        earlier = (datetime.strptime(minute, "%H:%M:%S") - timedelta(minutes=TREND_WINDOW_MINUTES)).strftime("%H:%M:%S")
        state, evidence = assess_sector_sync(
            complete=coverage >= MIN_COVERAGE and len(observed) >= MIN_OBSERVATIONS and bool(indexes),
            breadth=breadth, average_change=average, index_positive=sum(indexes) >= 0 if indexes else None,
            opening_changes=changes(minute, eligible, opening_prices),
            earlier_changes=changes(earlier, eligible, opening_prices), expected_members=len(eligible))
        detail.update({"coverage": round(coverage, 4), "observed_members": len(observed), "assessed": True,
                       "breadth": breadth, "average_change_pct": average,
                       "index_change_pct": sum(indexes) / len(indexes) if indexes else None,
                       "data_completeness": 1.0 if state is not None else 0.0, "observed_minute": minute,
                       "unresolved_sector_names": [n for n in names if not members.get(n)],
                       "missing_minute_codes": [c for c in eligible if not minute_prices.get(c)],
                       "missing_previous_close_codes": [c for c in eligible if not finite_number(closes.get(c))],
                       **evidence})
        cache[key] = state, dict(detail)
        return state
    return checker, detail
