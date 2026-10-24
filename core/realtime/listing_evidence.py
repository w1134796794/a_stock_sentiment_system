"""Dated listing facts, captured by preparation only; never fetched by strategy."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.infrastructure.atomic_json import atomic_read_json, atomic_write_json
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now


def listing_exclusions(inputs):
    evidence = inputs.get("listing_evidence") or {}
    date = str(evidence.get("market_date") or "")
    if date != inputs.get("listing_as_of_date", date):
        return {}
    try:
        captured = shanghai_now(datetime.fromisoformat(evidence["captured_at"]))
    except (KeyError, ValueError, TypeError):
        return {}
    if captured.strftime("%Y%m%d") != date or captured.strftime("%H:%M:%S") >= "09:25:00":
        return {}
    excluded = {}
    for row in evidence.get("rows") or []:
        code = normalize_stock_code(row.get("ts_code") or row.get("code"), add_suffix=False)
        listed = str(row.get("list_date") or "").replace("-", "")
        retired = str(row.get("delist_date") or "").replace("-", "")
        status = row.get("list_status")
        reason = ""
        if len(retired) == 8 and retired.isdigit() and retired <= date:
            reason = "delisted"
        elif len(listed) == 8 and listed.isdigit() and listed > date:
            reason = "not_yet_listed"
        elif status in {"G", "UN"}:
            reason = "not_yet_trading"
        elif status == "D" and not retired:
            reason = "delisted_status_at_capture"
        if code and reason:
            excluded[code] = {"code": code, "name": row.get("name") or "", "reason": reason}
    return excluded


def capture_listing_evidence(date, *, root=None, client=None, now=None):
    from config.settings import WEB_DATA_DIR, TUSHARE_TOKEN
    clock = shanghai_now(now)
    if clock.strftime("%Y%m%d") != date or clock.strftime("%H:%M:%S") >= "09:25:00":
        raise ValueError("Listing evidence may only be captured for today before 09:25")
    path = Path(root or WEB_DATA_DIR) / "listing_evidence" / f"{date}.json"
    if path.exists():
        existing = atomic_read_json(path)
        captured = shanghai_now(datetime.fromisoformat(existing["captured_at"]))
        if (existing.get("complete") and existing.get("market_date") == date
                and captured.strftime("%Y%m%d") == date and captured.strftime("%H:%M:%S") < "09:25:00"):
            return existing
    rows, failures = [], []
    if client is None and TUSHARE_TOKEN:
        import tushare
        client = tushare.pro_api(TUSHARE_TOKEN)
    for status in ("L", "D", "P", "G", "UN"):
        try:
            if client is None:
                raise RuntimeError("Listing source is not configured")
            frame = client.stock_basic(exchange="", list_status=status,
                fields="ts_code,name,list_status,list_date,delist_date")
            if frame is None or not {"ts_code", "list_status", "list_date", "delist_date"}.issubset(frame.columns):
                raise ValueError("Incomplete listing schema")
            import json
            rows.extend(json.loads(frame.to_json(orient="records", force_ascii=False)))
            if (status == "L" and frame.empty) or len(frame) >= 6000:
                failures.append(status)
        except Exception:
            failures.append(status)
    finished = shanghai_now(now)
    if finished.strftime("%Y%m%d") != date or finished.strftime("%H:%M:%S") >= "09:25:00":
        raise ValueError("Listing capture exceeded preopen cutoff")
    value = {"market_date": date, "captured_at": finished.isoformat(), "source": "tushare_stock_basic",
             "complete": not failures, "failed_statuses": failures, "rows": rows}
    atomic_write_json(path, value)
    return value
