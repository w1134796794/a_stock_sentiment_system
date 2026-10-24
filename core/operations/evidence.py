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


def attach_evidence(payload: dict[str, Any], *, frozen_bundle=None) -> dict[str, Any]:
    candidate_date = str(payload.get("candidate_date") or payload.get("trade_date") or "")
    market_date = str(payload.get("market_date") or "")
    fetched_at = _fetch_time(candidate_date)
    bundle_id = str(payload.get("evidence_bundle_id") or "")
    frozen = frozen_bundle
    if candidate_date and market_date:
        from core.operations.preopen_evidence import PreopenEvidenceStore
        frozen = frozen or PreopenEvidenceStore().load(candidate_date, market_date)
        if frozen:
            fetched_at = str(frozen["inputs"]["artifacts"]["fetch"].get("fetched_at") or "")
            bundle_id = frozen["bundle_id"]
    frozen_config = None
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
        denominator = int(sector.get("coverage_denominator") or min(member_count, 80))
        sector_coverage = observed_members / denominator if denominator > 0 else None
        snapshot_primary = row.get("trigger_source") == "eltdx_snapshot"
        minute_closed_at = f"{market_date} {confirm_time}" if confirm_time and not snapshot_primary else ""
        row["signal_id"] = signal_id
        row["signal_evidence"] = {
            "signal_id": signal_id,
            "candidate_date": candidate_date,
            "market_date": market_date,
            "code": code,
            "profile": profile,
            "strategy_id": row.get("execution_strategy_id") or row.get("strategy_id") or (payload.get("strategy") or {}).get("id") or "",
            "strategy_sources": row.get("strategy_sources") or "",
            "config_version": row.get("strategy_version") or (payload.get("strategy") or {}).get("version") or "",
            "weight_version": row.get("weight_version") or "",
            "factor_values": row.get("factor_values") or {},
            "screening_score": row.get("screening_score"),
            "screening_rank": row.get("screening_rank"),
            "action_group": row.get("action_group") or "",
            "source_data_fetched_at": fetched_at,
            "evidence_bundle_id": bundle_id,
            "quote_source_time": row.get("quote_source_time") or row.get("time") or "",
            "quote_received_at": row.get("received_at") or row.get("quote_time") or "",
            "minute_closed_at": minute_closed_at,
            "confirm_time": confirm_time,
            "trigger_source": row.get("trigger_source") or "minute_fallback",
            "entry_time": row.get("entry_time") or "",
            "entry_price": row.get("entry_price"),
            "entry_mode": mode,
            "confirmation_policy": row.get("confirmation_policy") or "legacy",
            "confirmation_reason": row.get("confirmation_reason") or "",
            "initial_confirm_time": row.get("initial_confirm_time") or "",
            "breakout_level": row.get("breakout_level"),
            "opening_evidence": row.get("opening_evidence") or {},
            "board_feedback": row.get("board_feedback") or {},
            "entry_parameters": row.get("entry_parameters") or {},
            "condition_checks": row.get("condition_checks") or {},
            "evaluation_timing": row.get("evaluation_timing") or {},
            "sample_interval_seconds": row.get("sample_interval_seconds"),
            "sector_observed_at": sector.get("observed_at") or "",
            "sector_coverage": round(sector_coverage, 4) if sector_coverage is not None else None,
            "sector_data_completeness": sector.get("data_completeness"),
            "status": row.get("confirm_status") or row.get("status") or "",
            "data_status": row.get("data_status") or "",
            "quote_stale_seconds": row.get("quote_stale_seconds"),
            "sector_detail": sector,
            "trigger_reason": row.get("raw_trigger_reason") or row.get("reason") or "",
            "veto_reason": row.get("entry_veto_reason") or row.get("notification_skip_reason") or row.get("health_gate_reason") or "",
            "paper_execution_time": "",
        }
        if confirm_time and row["signal_evidence"]["status"] in {"confirmed", "unfilled"}:
            from core.signals.case_features import build_signal_case

            if frozen_config is None:
                from dataclasses import asdict
                from backtest.backtest_engine import BacktestConfig
                from risk.risk_config import RiskConfig
                risk = RiskConfig.from_dict(frozen["inputs"]["risk_config"]) if frozen else RiskConfig.load()
                frozen_config = asdict(BacktestConfig.from_risk_config(risk))
            policy = str(frozen_config.get("exit_policy_mode") or "strategy")
            if policy == "oos_selected":
                from backtest.exit_policy import ExitPolicyRepository
                resolved_policy = ExitPolicyRepository().resolve(str(row.get("strategy_id") or "default"), market_date)
                policy = str(resolved_policy.get("policy") or "strategy")
            else:
                resolved_policy = {"policy": policy}
            version = stable_id("signal_execution_v1", row.get("strategy_version") or "",
                                row.get("strategy_execution") or {}, frozen_config, resolved_policy)
            case = build_signal_case({**row, "strategy_version": version},
                                     {**payload, "source_available_at": fetched_at, "pit_verified": True})
            case["strategy_config_version"] = row.get("strategy_version") or ""
            case["backtest_config"] = frozen_config
            case["resolved_exit_policy"] = resolved_policy
            snapshot = row.get("signal_feature_snapshot")
            if snapshot:
                case["features"] = snapshot["features"]
                case["quality"] = snapshot["quality"]
            row["case_id"] = case["case_id"]
            row["signal_evidence"]["signal_case"] = case
    return payload


def record_runtime_observations(ledger, payload, *, now=None):
    """Audit background observations without turning them into executable signals."""
    from core.realtime.trading_session import shanghai_now
    clock = shanghai_now(now)
    if str(payload.get("market_date") or "") != clock.strftime("%Y%m%d"):
        return 0
    records = []
    for row in payload.get("rows") or []:
        evidence = row.get("signal_evidence") or {}
        signal_id = row.get("signal_id")
        if not signal_id or not evidence.get("code"):
            continue
        observation = {key: evidence.get(key) for key in (
            "candidate_date", "market_date", "code", "profile", "signal_id", "status", "data_status",
            "entry_mode", "confirm_time", "trigger_source", "minute_closed_at", "entry_time", "entry_price", "opening_evidence", "board_feedback",
            "confirmation_policy", "confirmation_reason", "initial_confirm_time", "breakout_level", "entry_parameters",
            "trigger_reason", "veto_reason", "quote_received_at", "quote_source_time", "quote_stale_seconds",
            "sector_detail", "sector_coverage", "sector_data_completeness", "evidence_bundle_id", "config_version")}
        observation["condition_checks"] = evidence.get("condition_checks") or {}
        observation["evaluation_timing"] = evidence.get("evaluation_timing") or {}
        observation["sample_interval_seconds"] = evidence.get("sample_interval_seconds")
        observation["runtime_cycle"] = payload.get("runtime_cycle") or {}
        observation.update({"observed_at": clock.isoformat(timespec="milliseconds"),
                            "evidence_kind": "runtime_observation"})
        event_id = stable_id(signal_id, "runtime_observation", clock.strftime("%Y%m%d%H%M"),
                             evidence.get("status"), evidence.get("data_status"),
                             evidence.get("trigger_reason"), evidence.get("veto_reason"))
        records.append((event_id, observation))
    return ledger.record_observations(records)
