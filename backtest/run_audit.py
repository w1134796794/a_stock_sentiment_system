"""Deterministic audit artifacts for comparing backtest runs."""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _git_revision(base_dir: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(base_dir), text=True,
            stderr=subprocess.DEVNULL, timeout=3,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_entry_funnel(
    attempts: Iterable[Mapping[str, Any]], *, candidate_count: int = 0,
) -> list[Dict[str, Any]]:
    """Aggregate the candidate -> signal -> fill -> risk funnel by stable reason code."""
    rows = [dict(item) for item in attempts if isinstance(item, Mapping)]
    grouped: Counter[tuple[str, str, str, str, str]] = Counter()
    for row in rows:
        grouped[(
            str(row.get("strategy_id") or "default"),
            str(row.get("entry_mode") or ""),
            str(row.get("stage") or "entry_signal"),
            str(row.get("status") or "unknown"),
            str(row.get("reason_code") or row.get("status") or "unknown"),
        )] += 1
    output = [{
        "strategy_id": strategy_id,
        "entry_mode": entry_mode,
        "stage": stage,
        "status": status,
        "reason_code": reason_code,
        "count": count,
    } for (strategy_id, entry_mode, stage, status, reason_code), count in sorted(grouped.items())]
    output.insert(0, {
        "strategy_id": "all", "entry_mode": "", "stage": "candidate",
        "status": "loaded", "reason_code": "candidate_loaded", "count": int(candidate_count),
    })
    return output


def build_entry_opportunity_summary(
    attempts: Iterable[Mapping[str, Any]], *, candidate_count: int = 0,
    executed_buys: int = 0, closed_trades: int = 0,
) -> Dict[str, int]:
    """Separate market signals from account acceptance and completed round trips."""
    rows = [dict(item) for item in attempts if isinstance(item, Mapping)]

    def key(row: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("date") or ""),
            str(row.get("stock_code") or ""),
            str(row.get("strategy_id") or "default"),
        )

    fillable_keys = {
        key(row) for row in rows
        if str(row.get("stage") or "entry_signal") == "entry_signal"
        and str(row.get("status") or "") == "filled"
    }
    unfilled_keys = {
        key(row) for row in rows
        if str(row.get("stage") or "entry_signal") == "entry_signal"
        and str(row.get("status") or "") == "signal_unfilled"
    }
    later_gate_keys = {
        key(row) for row in rows
        if str(row.get("stage") or "") in {"sizing_gate", "portfolio_gate", "matching_gate"}
        and str(row.get("status") or "") == "rejected"
    }
    return {
        "candidate_count": int(candidate_count),
        "signal_count": len(fillable_keys | unfilled_keys),
        "fillable_signal_count": len(fillable_keys),
        "signal_unfilled_count": len(unfilled_keys),
        "account_rejected_after_signal_count": len(fillable_keys & later_gate_keys),
        "executed_buy_count": int(executed_buys),
        "closed_trade_count": int(closed_trades),
    }


def build_run_manifest(
    result: Mapping[str, Any], metadata: Mapping[str, Any] | None = None,
    *, base_dir: Path | None = None,
) -> Dict[str, Any]:
    """Build the reproducibility manifest persisted beside every backtest result."""
    root = Path(base_dir or Path(__file__).resolve().parents[1])
    meta = dict(metadata or {})
    config_snapshot = dict(result.get("backtest_config") or {})
    attempts = [dict(row) for row in (result.get("entry_attempts") or []) if isinstance(row, Mapping)]
    strategy_versions = sorted({
        (str(row.get("strategy_id") or "default"), str(row.get("strategy_version") or ""))
        for row in attempts
    })
    tracked_configs = [
        root / "config" / "risk_control.yaml",
        root / "config" / "strategy_combinations.yaml",
        root / "config" / "screening_profiles.yaml",
    ]
    config_files = {str(path.relative_to(root)): _file_hash(path) for path in tracked_configs}
    semantic_payload = {
        "backtest_config": config_snapshot,
        "config_files": config_files,
        "strategy_versions": strategy_versions,
        "entry_mode": result.get("entry_mode") or meta.get("entry_mode") or "",
        "start_date": meta.get("start_date") or "",
        "end_date": meta.get("end_date") or result.get("as_of_date") or "",
        "model_versions": meta.get("model_versions") or {},
        "data_cutoff": result.get("as_of_date") or meta.get("end_date") or "",
        "costs": {
            key: config_snapshot.get(key)
            for key in ("commission_rate", "stamp_duty_rate", "slippage")
        },
    }
    return {
        **semantic_payload,
        "code_revision": _git_revision(root),
        "configuration_hash": stable_hash(semantic_payload),
        "candidate_count": int(result.get("entry_candidate_count") or 0),
        "attempt_count": len(attempts),
    }


__all__ = [
    "build_entry_funnel", "build_entry_opportunity_summary", "build_run_manifest", "stable_hash",
]
