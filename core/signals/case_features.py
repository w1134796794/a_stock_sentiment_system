"""Compact point-in-time features. Unknown observations stay unknown."""
from __future__ import annotations

import math
from numbers import Integral
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from backtest.minute_entry import normalize_minute_bars
from core.operations.ledger import stable_id

FEATURE_VERSION = "signal_case_v1"


def clean(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.lower() not in {"nan", "none", "nat"} else None
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return str(value)


def event_time(date: str, value: Any = "15:00:00") -> str:
    text = str(value or "").replace("T", " ")
    if not text:
        return ""
    if len(text) <= 8:
        text = f"{date} {text}"
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert("Asia/Shanghai").tz_localize(None)
    return parsed.isoformat()


def build_signal_case(row: Mapping[str, Any], context: Mapping[str, Any], *,
                      minutes: pd.DataFrame | None = None, origin: str = "live") -> dict[str, Any]:
    row = clean(row)
    context = clean(context)
    date = str(context.get("market_date") or row.get("market_date") or row.get("date") or "")
    candidate = str(context.get("candidate_date") or row.get("candidate_date") or "")
    code = str(row.get("code") or row.get("stock_code") or "").split(".")[0].zfill(6)
    mode = str(row.get("entry_mode") or "")
    config = str(row.get("strategy_version") or context.get("config_version") or "")
    confirm = event_time(date, row.get("confirm_time"))
    minute_confirm = confirm
    # The shared engine labels a completed minute by its start. Its facts become
    # observable at the following boundary, not at that minute's open.
    minute_closed = (pd.Timestamp(confirm) + pd.Timedelta(minutes=1)).isoformat() if confirm else ""
    received = event_time(date, row.get("received_at") or row.get("quote_time"))
    observed = event_time(date, (row.get("sector_detail") or {}).get("observed_at"))
    if origin == "live" and confirm:
        confirm = max(minute_closed, received, observed)
    elif confirm:
        confirm = minute_closed
    factors = dict(row.get("factor_values") or row.get("metrics") or {})
    features = {"factors": factors, "factor_scores": row.get("metrics") or {},
                "daily_context": row.get("context") or {}}
    fields = (
        "market_score", "market_regime", "emotion_phase", "profit_effect_score",
        "promotion_trend_score", "crowding_score", "leader_roles", "role_labels",
        "lifecycle_state", "continuity_score", "leader_score", "sector_status_score",
        "market_status_score", "capital_recognition_score", "safety_score", "board_height",
        "limit_pct", "limit_progress", "resonance_sectors", "screening_score",
        "open_gap_pct", "amount_pace", "hold_minutes", "false_break_count",
        "pullback_quality", "active_buy_ratio", "sector_confirmed",
    )
    for field in fields:
        features[field] = row.get(field, context.get(field))
    daily = row.get("context") or {}
    market = context.get("market_state") or {}
    aliases = {
        "market_score": ("mkt_market_score", "market_score"),
        "profit_effect_score": ("mkt_profit_effect_score", "profit_effect_score"),
        "promotion_trend_score": ("mkt_promotion_trend_score", "promotion_trend_score"),
        "crowding_score": ("stk_crowding_decay_5d", "crowding_penalty_score"),
        "board_height": ("board_height",), "limit_pct": ("limit_pct",),
        "limit_progress": ("limit_progress",),
        "liquidity_score": ("stk_liquidity_percentile", "liquidity_score"),
        "amount_ratio_5d": ("amount_ratio_5d", "amount_ratio"),
        "new_high_ratio": ("new_high_ratio",),
    }
    for field, names in aliases.items():
        if features.get(field) is None:
            features[field] = next((source[name] for name in names for source in (daily, factors, market)
                                    if source.get(name) is not None), None)
    sector = row.get("sector_detail") or {}
    observed = event_time(date, sector.get("observed_at"))
    sector_after = bool(observed and confirm and observed > confirm)
    features["sector_breadth"] = None if sector_after else sector.get("breadth")
    features["sector_change_pct"] = None if sector_after else sector.get("index_change_pct", sector.get("change_pct"))
    # Never use the current quote (which may be hours after first confirmation).
    prefix = pd.DataFrame()
    if minutes is not None and confirm:
        prefix = normalize_minute_bars(minutes)
        if not prefix.empty:
            prefix = prefix[prefix["time"] <= minute_confirm.split("T")[-1]]
    for field in ("confirm_close", "vwap_distance", "opening_high_distance", "cum_amount", "vwap_source"):
        features[field] = None
    if not prefix.empty:
        last = prefix.iloc[-1]
        close, vwap = float(last["close"]), float(last["vwap"])
        opening = prefix[prefix["time"] <= "09:34:00"]
        high = float(opening["high"].max()) if len(opening) >= 5 else None
        features.update(confirm_close=close, vwap_distance=close / vwap - 1 if vwap > 0 else None,
                        opening_high_distance=close / high - 1 if high and high > 0 else None,
                        cum_amount=clean(last.get("cum_amount")), vwap_source=last.get("vwap_source"))
    missing = [key for key, value in features.items() if value is None]
    source_available = event_time(date, context.get("source_available_at"))
    pit_verified = bool(context.get("pit_verified") and source_available and confirm and source_available <= confirm)
    # One objective stock/day/mode/version event, irrespective of strategy overlap.
    case_id = stable_id("signal_case", date, code, mode, config, FEATURE_VERSION, origin)
    return {
        "case_id": case_id, "signal_id": row.get("signal_id") or "", "code": code,
        "name": row.get("name") or row.get("stock_name") or "", "candidate_date": candidate,
        "market_date": date, "confirm_at": confirm, "feature_cutoff_at": confirm,
        "confirm_minute_at": minute_confirm, "minute_closed_at": minute_closed,
        "observed_at": event_time(date, row.get("quote_source_time") or row.get("time")),
        "received_at": received,
        "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
        "sector_observed_at": observed, "source_available_at": source_available,
        "origin": origin, "pit_verified": pit_verified, "feature_version": FEATURE_VERSION,
        "config_version": config, "data_version": context.get("data_version") or stable_id("case_inputs", factors, daily, source_available),
        "strategy_id": row.get("strategy_id") or context.get("strategy_id") or "",
        "strategy_sources": row.get("strategy_sources") or [], "entry_mode": mode,
        "status": row.get("signal_status") or row.get("confirm_status") or row.get("status") or "observe",
        "trigger_reason": row.get("reason") or "", "veto_reason": row.get("health_gate_reason") or "",
        "features": clean(features), "quality": {
            "missing_fields": missing, "feature_coverage": (len(features) - len(missing)) / len(features),
            "sector_observed_after_cutoff": sector_after,
            "sector_data_completeness": sector.get("data_completeness"),
            "data_status": row.get("data_status"),
            "execution_precision": "minute_point" if not prefix.empty and "source" in prefix and prefix["source"].eq("eltdx").any() else "minute_ohlc",
            "timestamp_semantics": "minute_start_plus_one_minute",
        },
        "entry_time": row.get("entry_time") or "", "entry_price": row.get("entry_price"),
        "strategy_execution": row.get("strategy_execution") or {},
    }
