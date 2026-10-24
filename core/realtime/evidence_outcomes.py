"""Match optional execution-backtest outcomes to exact A/C confirmation boundaries."""

from __future__ import annotations

from statistics import mean

from core.realtime.market_evidence import number, timestamp


def attach_outcomes(report, outcomes):
    if outcomes.get("schema_version") != 1 or outcomes.get("frozen_inputs_hash") != report["frozen_inputs_hash"]:
        raise ValueError("Outcome inputs do not match frozen experiment inputs")
    if outcomes.get("policy_hash") != report["policy_hash"] or not outcomes.get("source_run_id"):
        raise ValueError("Outcome policy/run identity mismatch")
    cost = number(outcomes.get("round_trip_cost_bps"))
    if cost is None or cost < 0:
        raise ValueError("Explicit common round-trip cost is required")
    indexed = {}
    for row in outcomes.get("rows", []):
        key = (row["market_date"], row["code"], row["group"], row["confirmed_at"])
        if row["group"] not in {"A", "C"} or key in indexed:
            raise ValueError("Duplicate or invalid outcome identity")
        if not isinstance(row.get("filled"), bool):
            raise ValueError("Outcome fill status must be explicit")
        if row["filled"]:
            entry, exit = number(row.get("entry_price")), number(row.get("exit_price"))
            if entry is None or exit is None or entry <= 0 or exit <= 0:
                raise ValueError("Filled outcomes require valid execution prices")
            if not timestamp(row["confirmed_at"]) < timestamp(row["entry_at"]) <= timestamp(row["exit_at"]):
                raise ValueError("Outcome trade must follow its confirmation")
            row = {**row, "net_return_pct": (exit / entry - 1) * 100 - cost / 100}
        indexed[key] = row
    measured = {}
    expected = set()
    for group in ("A", "C"):
        matched, missing = [], 0
        for day in report["days"]:
            for stock in day["candidates"]:
                at = stock[f"{group}_confirmed_at"]
                if not at:
                    continue
                key = (day["market_date"], stock["code"], group, at)
                expected.add(key)
                row = indexed.get(key)
                if row is None:
                    missing += 1
                else:
                    matched.append(row)
        filled = [r for r in matched if r["filled"]]
        measured[group] = dict(
            matched=len(matched),
            missing=missing,
            filled=len(filled),
            fill_rate=len(filled) / len(matched) if matched and not missing else None,
            mean_filled_trade_net_return_pct=mean(r["net_return_pct"] for r in filled) if filled else None,
            win_rate=sum(r["net_return_pct"] > 0 for r in filled) / len(filled) if filled else None,
            portfolio_return=None,
            portfolio_max_drawdown=None,
        )
    if set(indexed) - expected:
        raise ValueError("Outcomes contain confirmations outside this experiment")
    measured["B"] = dict(measured["A"])
    report["imported_outcomes"] = dict(
        source_run_id=outcomes["source_run_id"],
        round_trip_cost_bps=cost,
        groups=measured,
        execution_source_verified=False,
        portfolio_metrics_measured=False,
        reason="Trade statistics from supplied backtest records; not proof of real fills or portfolio performance",
    )
    report["profitability_measured"] = bool(indexed) and all(measured[g]["missing"] == 0 for g in ("A", "C"))
    return report
