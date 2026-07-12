"""Auditable advanced stock factors derived from existing point-in-time data."""
from __future__ import annotations

from typing import Any, Dict, Mapping

import pandas as pd


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if pd.notna(number) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def seal_quality(pool: Mapping[str, Any], amount_yuan: Any) -> tuple[float, float]:
    """Score seal timing, reseal stability and sealed-order support."""
    first_time = str(pool.get("first_time") or "")
    last_time = str(pool.get("last_time") or first_time)
    if not first_time or first_time in {"0", "0.0", "nan", "None", "00:00:00"}:
        return 50.0, 0.0
    timing = 95.0 if first_time <= "09:35:00" else 82.0 if first_time <= "10:00:00" else 65.0 if first_time <= "11:30:00" else 35.0
    open_times = max(int(_number(pool.get("open_times"))), 0)
    stability = _clip(100.0 - open_times * 14.0)
    if last_time and first_time and last_time > first_time:
        stability = _clip(stability - min(open_times, 4) * 4.0)
    amount = max(_number(amount_yuan), 0.0)
    sealed_ratio = max(_number(pool.get("fd_amount")), 0.0) / amount if amount > 0 else 0.0
    support = _clip(sealed_ratio / 0.12 * 100.0)
    return round(timing * 0.40 + stability * 0.35 + support * 0.25, 4), sealed_ratio


def _time_minutes(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"0", "0.0", "nan", "None", "00:00:00"}:
        return None
    try:
        hour, minute, second = [int(part) for part in text.split(":")[:3]]
        return hour * 60.0 + minute + second / 60.0
    except (TypeError, ValueError):
        return None


def reseal_resilience(pool: Mapping[str, Any]) -> float:
    """Measure whether intraday divergence was absorbed and resealed promptly."""
    first = _time_minutes(pool.get("first_time"))
    last = _time_minutes(pool.get("last_time"))
    if first is None:
        return 50.0
    opens = max(int(_number(pool.get("open_times"))), 0)
    if opens <= 0:
        return 95.0
    delay = max((last or first) - first, 0.0)
    return round(_clip(90.0 - opens * 13.0 - min(delay / 180.0, 1.0) * 35.0), 4)


def late_seal_safety(pool: Mapping[str, Any], prior_first_times: list[Any]) -> tuple[float, float]:
    """Penalize a seal that is materially later than the stock's recent seals."""
    current = _time_minutes(pool.get("first_time"))
    history = [value for value in (_time_minutes(item) for item in prior_first_times) if value is not None]
    if current is None or not history:
        return 50.0, 0.0
    baseline = float(pd.Series(history).median())
    delay = current - baseline
    return round(_clip(50.0 - delay / 90.0 * 50.0), 4), round(delay, 4)


def sector_rotation_metrics(history: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """Estimate sector rotation age and acceleration from historical sector scores."""
    if history.empty:
        return {}
    data = history.copy()
    data["trade_date"] = data["trade_date"].astype(str)
    data["sector_code"] = data["sector_code"].astype(str).str.split(".").str[0]
    for column in ("momentum_score", "mainline_score"):
        data[column] = pd.to_numeric(data.get(column), errors="coerce").fillna(50.0)
    output: Dict[str, Dict[str, float]] = {}
    for code, rows in data.sort_values("trade_date").groupby("sector_code"):
        current = rows.iloc[-1]
        previous = rows.iloc[:-1].tail(3)
        previous_mainline = float(previous["mainline_score"].mean()) if not previous.empty else 50.0
        acceleration = float(current["mainline_score"]) - previous_mainline
        active = list((rows["mainline_score"] >= 60.0).astype(bool))
        age = 0
        for value in reversed(active):
            if not value:
                break
            age += 1
        age_score = {0: 45.0, 1: 100.0, 2: 90.0, 3: 78.0, 4: 62.0}.get(age, 42.0)
        acceleration_score = _clip(50.0 + acceleration * 3.0)
        score = (
            float(current["momentum_score"]) * 0.45
            + acceleration_score * 0.30
            + age_score * 0.25
        )
        output[str(code)] = {
            "score": round(_clip(score), 4),
            "age": float(age),
            "acceleration": round(acceleration, 4),
        }
    return output


def crowding_metrics(limit_history: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """Penalize repeated limit-up appearances that become increasingly crowded."""
    if limit_history.empty:
        return {}
    data = limit_history.copy()
    data["trade_date"] = data["trade_date"].astype(str)
    data["code"] = data["code"].astype(str).str.split(".").str[0].str.zfill(6)
    recent_dates = sorted(data["trade_date"].unique())[-5:]
    data = data[data["trade_date"].isin(recent_dates)]
    output: Dict[str, Dict[str, float]] = {}
    for code, rows in data.groupby("code"):
        appearances = int(rows["trade_date"].nunique())
        score = {1: 90.0, 2: 78.0, 3: 58.0, 4: 35.0, 5: 18.0}.get(appearances, 50.0)
        output[str(code)] = {"score": score, "appearances": float(appearances)}
    return output


def relative_sector_strength(today: pd.DataFrame) -> pd.DataFrame:
    """Compare each stock with the median move of its point-in-time primary sector."""
    result = pd.DataFrame(index=today.index)
    pct = pd.to_numeric(today.get("pct_chg"), errors="coerce").fillna(0.0)
    sectors = today.get("primary_sector_code", pd.Series("", index=today.index)).fillna("").astype(str)
    medians = pct.groupby(sectors).transform("median")
    group_size = sectors.groupby(sectors).transform("size")
    raw = (pct - medians).where((sectors != "") & (group_size >= 3), 0.0)
    score = raw.groupby(sectors).rank(method="average", pct=True).mul(100.0)
    score = score.where((sectors != "") & (group_size >= 3), 50.0).fillna(50.0)
    result["relative_strength_sector_raw"] = raw
    result["relative_strength_sector_score"] = score
    return result


__all__ = [
    "crowding_metrics", "late_seal_safety", "relative_sector_strength",
    "reseal_resilience", "seal_quality", "sector_rotation_metrics",
]
