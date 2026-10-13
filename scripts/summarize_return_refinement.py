"""Audit a completed refinement against the previously frozen quality control."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.replay_research import ReplayResearch, atomic_json, digest
from scripts.validate_confirmation_quality import sealed_source


def state_at(directory, manifest, arm):
    value = json.loads((directory / "states" / f"{arm}.json").read_text(encoding="utf-8"))
    if value["contract_hash"] != manifest["contract_hash"] or value["state_hash"] != digest(value["state"]):
        raise ValueError(f"Invalid checkpoint: {arm}")
    return value["state"]


def control_reconciliation(current, prior):
    fields = ("date", "stock_code", "action", "entry_date", "entry_time", "exit_time", "entry_price",
              "exit_price", "shares", "position_size", "pnl", "pnl_pct")
    left, right = current["trade_history"], prior["trade_history"]
    if len(left) != len(right):
        return False
    for new, old in zip(left, right, strict=True):
        for key in fields:
            a, b = new.get(key), old.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-6):
                    return False
            elif a != b:
                return False
    return len(current["daily_nav"]) == len(prior["daily_nav"]) and all(
        new["date"] == old["date"] and math.isclose(new["total_value"], old["total_value"], rel_tol=1e-12, abs_tol=1e-6)
        for new, old in zip(current["daily_nav"], prior["daily_nav"], strict=True))


def summarize(source, output):
    source_manifest = sealed_source(source.resolve())
    marker = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    if marker["source_contract_hash"] != source_manifest["contract_hash"]:
        raise ValueError("Different source contract")
    service = ReplayResearch()
    manifest = service.verify(marker["experiment_id"])
    directory = service._directory(marker["experiment_id"])
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if report["status"] != "complete" or manifest["spec"]["suite"] != "return_refinement":
        raise ValueError("Requires a completed refinement")
    states = {arm: state_at(directory, manifest, arm) for arm in manifest["arms"]}
    controls = {arm: control_reconciliation(states[arm], state_at(source, source_manifest, arm))
                for arm in ("strategy_quality", "exit_failure")}
    if not all(controls.values()):
        raise ValueError("Unchanged controls no longer reproduce prior trades/NAV")
    details = {}
    for arm, state in states.items():
        sells = [t for t in state["trade_history"] if str(t["action"]).startswith("SELL")]
        exit_groups = {}
        for trade in sells:
            group = exit_groups.setdefault(trade["exit_reason"], {"closed": 0, "net_pnl": 0})
            group["closed"] += 1
            group["net_pnl"] += trade["pnl"]
        monthly = {}
        previous = report["base_config"]["initial_capital"]
        for nav in state["daily_nav"]:
            month = nav["date"][:6]
            row = monthly.setdefault(month, {"initial": previous, "ending": previous})
            row["ending"] = previous = nav["total_value"]
        details[arm] = {"summary": report["results"][arm]["summary"],
            "monthly_return_pct": {key: (r["ending"] / r["initial"] - 1) * 100 for key, r in monthly.items()},
            "actual_buys": sum(t["action"] == "BUY" for t in state["trade_history"]),
            "attempt_reasons": dict(Counter(a.get("reason_code", a.get("status", "")) for a in state["entry_attempts"])),
            "exit_reasons": exit_groups,
            "strategy_regimes": report["results"][arm]["metrics"],
            "validation_closed": report["results"][arm]["validation_closed"],
            "evidence_status": report["results"][arm]["evidence_status"]}
    result = {"experiment_id": manifest["experiment_id"], "classification": manifest["classification"],
        "control_reproduces_prior_economic_trades_and_nav": controls, "details": details,
        "paired_comparisons": report["paired_comparisons"], "folds": report["folds"],
        "production_changes": False, "release_allowed": False, "unseen_validation_required": True}
    atomic_json(output / "diagnosis.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.source, args.output)
    print(json.dumps({"experiment_id": result["experiment_id"],
        "results": {arm: row["summary"] for arm, row in result["details"].items()}}, ensure_ascii=True, indent=2))
