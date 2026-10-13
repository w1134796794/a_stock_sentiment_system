"""Retain every registered trial; rank fitted historical results without production release."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.replay_research import ReplayResearch, atomic_json
from scripts.summarize_return_refinement import control_reconciliation, state_at
from scripts.validate_confirmation_quality import sealed_source


def rank_trials(results, policy):
    eligible = [arm for arm, row in results.items() if row["complete"] and not row["blocked"]
        and not row["data_quality"].get("skipped_candidate_count")
        and row["summary"]["return_pct"] > 0
        and row["summary"]["closed_trades"] >= policy["minimum_closed_trades"]
        and row["summary"]["max_drawdown_pct"] <= policy["maximum_drawdown_pct"]]
    return max(eligible, key=lambda arm: (results[arm]["summary"]["return_pct"], arm)) if eligible else None


def summarize(source, output):
    prior = sealed_source(source.resolve())
    marker = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    if marker["source_contract_hash"] != prior["contract_hash"]:
        raise ValueError("Different source contract")
    service = ReplayResearch()
    manifest = service.verify(marker["experiment_id"])
    directory = service._directory(manifest["experiment_id"])
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if report["status"] != "complete" or manifest["spec"]["suite"] != "in_sample_tuning":
        raise ValueError("Requires completed in-sample tuning")
    states = {arm: state_at(directory, manifest, arm) for arm in manifest["arms"]}
    if not control_reconciliation(states["strategy_quality"], state_at(source, prior, "strategy_quality")):
        raise ValueError("Unchanged control no longer reproduces economic trades/NAV")
    result = {"experiment_id": manifest["experiment_id"], "source_experiment_id": prior["experiment_id"],
        "classification": manifest["classification"], "search_policy": manifest["search_policy"],
        "selected_arm": rank_trials(report["results"], manifest["search_policy"]),
        "selection_uses_full_history": True, "unseen_validation_required": True,
        "controls_reconciled": True, "production_changes": False, "release_allowed": False,
        "evidence_hash": manifest["evidence_hash"], "configuration_hash": manifest["configuration_hash"],
        "implementation_hash": manifest["implementation_hash"], "freeze_attempts": marker["attempts"],
        "trials": {}, "retrospective_folds": report["folds"]}
    for arm, row in report["results"].items():
        state = states[arm]
        previous = report["base_config"]["initial_capital"]
        months = {}
        for nav in state["daily_nav"]:
            month = months.setdefault(nav["date"][:6], {"initial": previous, "ending": previous})
            month["ending"] = previous = nav["total_value"]
        result["trials"][arm] = {"name": row["name"], "parameters": row["resolved_parameters"],
            "rule": manifest["arms"][arm], "summary": row["summary"], "data_quality": row["data_quality"],
            "actual_buys": sum(t["action"] == "BUY" for t in state["trade_history"]),
            "terminal_positions": len(state["current_positions"]), "strategy_regimes": row["metrics"],
            "validation_closed": row["validation_closed"], "evidence_status": row["evidence_status"],
            "monthly_return_pct": {key: (r["ending"] / r["initial"] - 1) * 100 for key, r in months.items()}}
    atomic_json(output / "all_trials.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.source, args.output)
    print(json.dumps({"experiment_id": result["experiment_id"], "selected_arm": result["selected_arm"],
        "results": {arm: row["summary"] for arm, row in result["trials"].items()}}, ensure_ascii=True, indent=2))
