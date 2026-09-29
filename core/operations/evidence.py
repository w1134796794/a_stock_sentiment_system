"""Capture only facts observable at signal time; never backfill future prices."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config.settings import WEB_DATA_DIR
from core.operations.ledger import stable_id


def _fetch_time(candidate_date: str) -> str:
    path = Path(WEB_DATA_DIR) / "fetch_status" / f"fetch_{candidate_date}.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("fetched_at") or "")
    except (OSError, ValueError, TypeError):
        return ""


def attach_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    candidate_date = str(payload.get("candidate_date") or payload.get("trade_date") or "")
    market_date = str(payload.get("market_date") or "")
    fetched_at = _fetch_time(candidate_date)
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").split(".")[0].zfill(6)
        mode = str(row.get("entry_mode") or "")
        confirm_time = str(row.get("confirm_time") or "")
        profile = str(payload.get("profile") or "")
        signal_id = stable_id("signal", candidate_date, market_date, code, profile, mode, confirm_time)
        sector = row.get("sector_detail") or {}
        member_count = int(sector.get("member_count") or 0)
        observed_members = int(sector.get("observed_members") or 0)
        sector_coverage = observed_members / min(member_count, 80) if member_count > 0 else None
        minute_closed_at = f"{market_date} {confirm_time}" if confirm_time else ""
        row["signal_id"] = signal_id
        row["signal_evidence"] = {
            "signal_id": signal_id,
            "candidate_date": candidate_date,
            "market_date": market_date,
            "code": code,
            "profile": profile,
            "strategy_id": row.get("strategy_id") or (payload.get("strategy") or {}).get("id") or "",
            "strategy_sources": row.get("strategy_sources") or "",
            "config_version": row.get("strategy_version") or (payload.get("strategy") or {}).get("version") or "",
            "weight_version": row.get("weight_version") or "",
            "factor_values": row.get("factor_values") or {},
            "screening_score": row.get("screening_score"),
            "screening_rank": row.get("screening_rank"),
            "action_group": row.get("action_group") or "",
            "source_data_fetched_at": fetched_at,
            "quote_source_time": row.get("quote_source_time") or row.get("time") or "",
            "quote_received_at": row.get("received_at") or row.get("quote_time") or "",
            "minute_closed_at": minute_closed_at,
            "entry_time": row.get("entry_time") or "",
            "entry_price": row.get("entry_price"),
            "sector_observed_at": sector.get("observed_at") or "",
            "sector_coverage": round(sector_coverage, 4) if sector_coverage is not None else None,
            "sector_data_completeness": sector.get("data_completeness"),
            "status": row.get("confirm_status") or row.get("status") or "",
            "trigger_reason": row.get("reason") or "",
            "veto_reason": row.get("notification_skip_reason") or row.get("health_gate_reason") or "",
            "paper_execution_time": "",
        }
    return payload
