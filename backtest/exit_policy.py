"""Versioned exit-policy variants with point-in-time OOS selection."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional

EXIT_POLICIES = {"strategy", "fixed_stop", "atr_stop", "structure_stop", "staged_trailing", "oos_selected"}


def normalize_exit_parameters(raw: Dict[str, Any], defaults: Dict[str, Any] | None = None) -> Dict[str, Any]:
    from core.screening.strategy_profiles import DEFAULT_EXIT

    result = {**DEFAULT_EXIT, **(defaults or {})}
    for key in DEFAULT_EXIT:
        value = float(raw.get(key, result[key]))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"退出参数无效: {key}")
        result[key] = max(1, int(value)) if key == "time_stop_days" else value
    return result


def trailing_distance(peak_profit: float, config: Dict[str, Any]) -> float:
    if peak_profit >= config["trailing_high_profit"]:
        return config["trailing_stop"]
    if peak_profit >= config["trailing_mid_profit"]:
        return config["trailing_mid_stop"]
    return config["trailing_early_stop"]


class ExitPolicyRepository:
    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            from config.settings import WEB_DATA_DIR

            path = Path(WEB_DATA_DIR) / "models" / "exit_policy_oos.json"
        self.path = Path(path)

    def resolve(self, strategy_id: str, trade_date: str) -> Dict[str, Any]:
        """Return the latest policy whose effective date is before the trade."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        rows = payload.get("policies") if isinstance(payload, dict) else []
        eligible = [
            row for row in (rows or [])
            if str(row.get("strategy_id") or "default") == str(strategy_id or "default")
            and str(row.get("effective_date") or "") < str(trade_date or "")
            and str(row.get("policy") or "") in EXIT_POLICIES - {"oos_selected"}
            and bool(row.get("oos_passed", False))
        ]
        return dict(max(eligible, key=lambda row: str(row.get("effective_date") or ""))) if eligible else {}


def resolve_exit_config(
    base: Dict[str, Any], *, policy: str, factor_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Project one audited exit variant onto the strategy's base settings."""
    cfg = dict(base or {})
    context = dict(factor_context or {})
    selected = str(policy or "strategy").lower()
    if selected in {"strategy", "staged_trailing"}:
        cfg["policy"] = "staged_trailing" if selected == "staged_trailing" else "strategy"
        return cfg
    if selected == "fixed_stop":
        cfg.update({"policy": selected, "trailing_stop": 0.0, "trailing_activation": 99.0})
        return cfg
    if selected == "atr_stop":
        atr_pct = _number(context.get("atr_14_pct") or context.get("stk_atr_14_pct"))
        if atr_pct > 1.0:
            atr_pct /= 100.0
        atr_pct = min(max(atr_pct, 0.02), 0.08)
        cfg.update({
            "policy": selected,
            "hard_stop_loss": min(max(1.25 * atr_pct, 0.03), 0.10),
            "trailing_early_stop": min(max(1.0 * atr_pct, 0.03), 0.08),
            "trailing_mid_stop": min(max(1.5 * atr_pct, 0.04), 0.12),
            "trailing_stop": min(max(2.0 * atr_pct, 0.06), 0.16),
        })
        return cfg
    if selected == "structure_stop":
        structure = _number(context.get("structure_stop_pct") or context.get("support_distance_pct"))
        if structure > 1.0:
            structure /= 100.0
        cfg.update({
            "policy": selected,
            "hard_stop_loss": min(max(structure or cfg.get("hard_stop_loss", 0.05), 0.025), 0.10),
        })
        return cfg
    cfg["policy"] = "strategy"
    return cfg


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["EXIT_POLICIES", "ExitPolicyRepository", "resolve_exit_config",
           "normalize_exit_parameters", "trailing_distance"]
