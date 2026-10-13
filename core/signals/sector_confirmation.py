"""Shared point-in-time confirmation from distinct, non-ST sector peers."""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, Callable

import pandas as pd

from core.utils.price_limit import normalize_code
from core.utils.sector_names import normalize_sector_names
from core.utils.stock_eligibility import new_entry_exclusion

SECTOR_CONFIRMATION_VERSION = "sector_peers_prev_close_v1"
MIN_SECTOR_SAMPLES = 2
MIN_RISING_RATIO = 0.55


def sector_names_from_row(row: Mapping[str, Any]) -> set[str]:
    for key in ("resonance_sectors", "共振板块", "所属板块", "sector_names"):
        names = normalize_sector_names(row.get(key))
        if names:
            return set(names)
    return set()


def select_sector_peers(
    target_code: str, sectors: Any, rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    wanted = set(normalize_sector_names(sectors))
    target = normalize_code(target_code)
    peers: dict[str, dict[str, Any]] = {}
    excluded = set()
    for row in rows:
        code = normalize_code(row.get("code") or row.get("stock_code") or row.get("ts_code") or row.get("代码"))
        if not code or code == target:
            continue
        if new_entry_exclusion(row):
            excluded.add(code)
            continue
        if wanted.intersection(sector_names_from_row(row)):
            peers.setdefault(code, {**row, "code": code})
    return [row for code, row in peers.items() if code not in excluded]


def _positive_finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _time_text(value: Any) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text):
        return ""
    return text + ":00" if len(text) == 5 else text


def make_sector_sync_checker(
    target_code: str,
    sectors: Any,
    peer_rows: Iterable[Mapping[str, Any]],
    frames: Mapping[str, pd.DataFrame],
    *,
    previous_closes: Mapping[str, Any] | None = None,
    trade_date: str = "",
    evidence: dict[str, Any] | None = None,
) -> Callable[[str], bool | None]:
    """Consume normalized minute frames; missing evidence is never a negative vote."""
    peers = select_sector_peers(target_code, sectors, peer_rows)
    by_code = {}
    for code, frame in frames.items():
        if frame is None or frame.empty or not {"time", "close"}.issubset(frame.columns):
            continue
        times = frame["time"].map(_time_text)
        mask = times.ne("")
        if trade_date:
            date = str(trade_date).replace("-", "")
            for column in ("trade_date", "date"):
                if column in frame:
                    dates = frame[column].astype(str).str.replace("-", "", regex=False)
                    mask &= dates.eq(date)
            if "datetime" in frame:
                stamps = frame["datetime"]
                dates = stamps.astype(str).str[:10].str.replace("-", "", regex=False)
                mask &= stamps.isna() | dates.eq(date)
        by_code[normalize_code(code)] = frame.loc[mask].assign(_sector_time=times[mask]).sort_values("_sector_time")
    previous = {normalize_code(code): value for code, value in (previous_closes or {}).items()}
    detail = evidence if evidence is not None else {}
    source_failures = dict(detail.get("peer_failures") or {})
    detail.update({
        "sector_confirmation_version": SECTOR_CONFIRMATION_VERSION,
        "sector_confirmation_source": "peer_minutes",
        "reference_basis": "previous_close",
        "sector_names": sorted(normalize_sector_names(sectors)),
        "peer_codes": [row["code"] for row in peers],
        "minimum_observations": MIN_SECTOR_SAMPLES,
        "positive_ratio_threshold": MIN_RISING_RATIO,
        "observed_count": 0, "observed_members": 0, "positive_count": 0,
        "breadth": None, "data_completeness": 0.0,
    })

    def checker(at_time: str) -> bool | None:
        cutoff = _time_text(at_time)
        positive = observed = 0
        failures = {}
        for peer in peers:
            code = peer["code"]
            frame = by_code.get(code)
            if not cutoff or frame is None or frame.empty:
                failures[code] = source_failures.get(code) or "missing_minute_price"
                continue
            available = frame[frame["_sector_time"] <= cutoff]
            if available.empty:
                failures[code] = "missing_as_of_minute"
                continue
            latest = available.iloc[-1]
            simulated = str(latest.get("is_simulated", "")).lower() in {"1", "1.0", "true", "yes"}
            if simulated or "simulat" in str(latest.get("source", "")).lower():
                failures[code] = "simulated_minute_price"
                continue
            price = _positive_finite(latest.get("close"))
            pre_close = _positive_finite(previous.get(code)) or _positive_finite(latest.get("pre_close"))
            if not price or not pre_close:
                failures[code] = "missing_price_or_previous_close"
                continue
            observed += 1
            positive += int(price > pre_close)
        ratio = positive / observed if observed else None
        state = ratio >= MIN_RISING_RATIO if observed >= MIN_SECTOR_SAMPLES else None
        detail.update({
            "at_time": cutoff, "observed_count": observed, "observed_members": observed,
            "positive_count": positive, "breadth": ratio, "peer_failures": failures,
            "data_completeness": 0.5 if state is not None else 0.0,
            "reason_code": "insufficient_real_samples" if state is None else "peer_breadth_evaluated",
        })
        return state

    return checker
