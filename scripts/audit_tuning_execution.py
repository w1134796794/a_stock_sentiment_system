"""Audit actual fills, not merely hypothetical successful confirmation proposals."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.minute_entry import normalize_minute_bars
from core.operations.replay_research import FrozenEvidence, ReplayResearch, atomic_json
from scripts.summarize_return_refinement import state_at


def check_buy(trade, attempt, frame, *, sustain_minutes, slippage):
    by_time = {str(row.time): i for i, row in frame.iterrows()}
    initial = by_time[attempt["initial_confirm_time"]]
    final = by_time[attempt["confirm_time"]]
    fill = by_time[trade["entry_time"]]
    level = float(attempt["breakout_level"])
    assert attempt["confirmation_policy"] == "sustain"
    assert final - initial == sustain_minutes and fill == final + 1
    assert float(frame.iloc[initial].close) > level
    assert float(frame.iloc[initial].close) >= float(frame.iloc[initial].vwap)
    for index in range(initial + 1, final + 1):
        row, prior = frame.iloc[index], frame.iloc[index - 1]
        assert (datetime.strptime(row.time, "%H:%M:%S") - datetime.strptime(prior.time, "%H:%M:%S")).total_seconds() == 60
        assert row.volume > 0 and row.close >= level and row.close >= row.vwap
    assert attempt["sector_confirmed"] is True
    assert attempt["sector_evidence"]["at_time"] == attempt["confirm_time"]
    sector = attempt["sector_evidence"]
    assert sector["observed_count"] >= sector["minimum_observations"]
    assert sector["breadth"] >= sector["positive_ratio_threshold"]
    assert trade["shares"] > 0 and trade["shares"] % 100 == 0
    assert math.isclose(trade["entry_price"], float(frame.iloc[fill].open) * (1 + slippage), abs_tol=1e-8)
    assert frame.iloc[fill].volume > 0


def audit(output):
    marker = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    service = ReplayResearch()
    manifest = service.verify(marker["experiment_id"])
    directory = service._directory(manifest["experiment_id"])
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    if manifest["spec"]["suite"] != "in_sample_tuning" or report["status"] != "complete":
        raise ValueError("Requires completed tuning")
    evidence = FrozenEvidence(directory)
    violations, buys, sells = [], 0, 0
    for arm in manifest["arms"]:
        state = state_at(directory, manifest, arm)
        for trade in state["trade_history"]:
            if str(trade["action"]).startswith("SELL"):
                sells += 1
                if trade["entry_date"] >= trade["date"]:
                    violations.append({"arm": arm, "date": trade["date"], "code": trade["stock_code"], "reason": "T+1 violation"})
                continue
            if trade["action"] != "BUY":
                continue
            buys += 1
            try:
                attempts = [a for a in state["entry_attempts"] if a.get("status") == "filled"
                    and (a["date"], a["stock_code"], a["entry_time"]) == (trade["date"], trade["stock_code"], trade["entry_time"])]
                if len(attempts) != 1:
                    raise ValueError("Actual buy does not match one filled proposal")
                if arm != "strategy_quality" and trade["strategy_id"] in {"limit_pullback", "limit_reversal"}:
                    if attempts[0]["entry_mode"] != trade["strategy_id"]:
                        raise ValueError("Structural mode does not match gated strategy")
                frame = normalize_minute_bars(evidence.get_stock_tick(trade["stock_code"], trade["date"]))
                check_buy(trade, attempts[0], frame,
                    sustain_minutes=manifest["resolved_arm_parameters"][arm]["sustain_minutes"],
                    slippage=manifest["base_config"]["slippage"])
            except (AssertionError, KeyError, ValueError, IndexError) as error:
                violations.append({"arm": arm, "date": trade["date"], "code": trade["stock_code"], "reason": repr(error)})
    result = {"experiment_id": manifest["experiment_id"], "actual_buys_checked": buys,
        "actual_sells_checked": sells, "violations": violations,
        "checks": "completed breakout, registered sustain window, synchronous sector evidence, next-minute open plus slippage, lots, T+1"}
    atomic_json(output / "execution_audit.json", result)
    if violations:
        raise ValueError(f"Execution violations: {violations}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(audit(parser.parse_args().output), ensure_ascii=True))
