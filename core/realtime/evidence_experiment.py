"""Offline A/B/C evidence ablation. Does not modify strategy, fills or notifications."""

from __future__ import annotations

from statistics import mean

from core.realtime.evidence_replay import replay_evidence
from core.realtime.market_evidence import fingerprint, number, timestamp
from core.realtime.snapshot_replay import cutoff, replay_snapshot_inputs

FILTERS = {"max_spread_bps", "min_bid_depth_hand", "min_buy_amount_share", "min_opening_amount_yuan"}


def validate_policy(policy):
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") != 1
        or set(policy) - {"schema_version", "filters", "max_age_seconds", "calibration_end", "depth_visibility"}
    ):
        raise ValueError("Unknown experiment policy schema/fields")
    filters = policy.get("filters")
    if not isinstance(filters, dict) or not filters or set(filters) - FILTERS:
        raise ValueError("Supply at least one supported experimental filter")
    for key, value in filters.items():
        if isinstance(value, bool) or number(value) is None or value < 0:
            raise ValueError("Filter values must be finite nonnegative numbers")
        if key == "min_buy_amount_share" and value > 1:
            raise ValueError("Buy share must be between 0 and 1")
    if policy.get("depth_visibility", "published_only") not in {"published_only", "snapshot_receipt"}:
        raise ValueError("Unknown depth visibility policy")
    age = policy.get("max_age_seconds", 15)
    if isinstance(age, bool) or number(age) is None or not 1 <= age <= 60:
        raise ValueError("Evidence freshness must be 1-60 seconds")
    if policy.get("calibration_end"):
        from datetime import datetime

        datetime.strptime(policy["calibration_end"], "%Y%m%d")
    return {**policy, "max_age_seconds": age}


def gate_at(inputs, code, at, policy):
    date = inputs["request"]["market_date"]
    report = replay_evidence(
        inputs.get("evidence_events", []),
        date=date,
        until=at,
        plan_id=inputs["plan"]["plan_id"],
        codes=[code],
        embedded_snapshots=inputs.get("events", []),
    )
    rows = {row["kind"]: row for row in report["evidence"] if row["code"] == code}
    checks = {}
    for key, threshold in policy["filters"].items():
        kind = (
            "opening_match"
            if key == "min_opening_amount_yuan"
            else "trade_batch"
            if key == "min_buy_amount_share"
            else "depth"
        )
        row = rows.get(kind)
        visibility = "published_observation"
        if row is None and kind == "depth" and policy.get("depth_visibility") == "snapshot_receipt":
            candidate = next((item for item in report["snapshot_depth"] if item["code"] == code), None)
            if candidate:
                row = dict(
                    quality_status=candidate["depth"]["quality_status"],
                    received_at=candidate["received_at"],
                    rows=[candidate["depth"]],
                )
                visibility = "snapshot_receipt_only"
        valid = bool(
            row
            and (
                row["quality_status"] == "ok"
                or (
                    kind == "trade_batch"
                    and row["quality_status"] == "empty"
                    and row.get("coverage", {}).get("sequence_verified")
                )
            )
        )
        if row and kind != "opening_match":
            valid = (
                valid
                and 0 <= (timestamp(at) - timestamp(row["received_at"])).total_seconds() <= policy["max_age_seconds"]
            )
        value = None
        reason = "missing_or_invalid_published_evidence"
        if valid:
            if kind == "depth":
                book = (row["rows"] or [{}])[0]
                if key == "min_bid_depth_hand":
                    value = number(book.get("bid_depth_hand"))
                else:
                    bids, asks = book.get("buy_levels") or [], book.get("sell_levels") or []
                    if bids and asks:
                        bid, ask = number(bids[0].get("price")), number(asks[0].get("price"))
                        if bid is not None and ask is not None and bid > 0 and ask >= bid:
                            value = (ask - bid) / ((ask + bid) / 2) * 10000
            elif kind == "opening_match":
                value = number((row["rows"] or [{}])[0].get("amount_yuan"))
            else:
                coverage = report["trade_coverage"].get(code, {})
                if coverage.get("coverage", {}).get("sequence_verified"):
                    value = number(coverage.get("window_summary", {}).get("buy_amount_share"))
            reason = "observed" if value is not None else "field_unknown"
        passed = (value <= threshold if key.startswith("max_") else value >= threshold) if value is not None else None
        checks[key] = dict(value=value, threshold=threshold, passed=passed, reason=reason, visibility_basis=visibility)
    status = (
        "rejected"
        if any(row["passed"] is False for row in checks.values())
        else ("unknown" if any(row["passed"] is None for row in checks.values()) else "passed")
    )
    return dict(
        status=status,
        checks=checks,
        publication_required=policy.get("depth_visibility", "published_only") == "published_only",
        limitations=["Snapshot-embedded depth without a publication marker is not promoted to published evidence"],
    )


def compare_day(inputs, policy, *, baseline=None):
    policy = validate_policy(policy)
    baseline = baseline or replay_snapshot_inputs(inputs)
    date = inputs["request"]["market_date"]
    end = cutoff(inputs["request"])
    candidates = []
    for stock in baseline.get("stocks", []):
        confirmations = sorted(
            [
                row
                for row in stock.get("transitions", [])
                if row.get("signal_status") == "confirmed" and timestamp(row["evaluated_at"]) <= end
            ],
            key=lambda row: timestamp(row["evaluated_at"]),
        )
        decisions = []
        for row in confirmations:
            at = row["evaluated_at"]
            if timestamp(at) > end:
                continue
            gate = gate_at(inputs, stock["code"], at, policy)
            decisions.append(dict(evaluated_at=at, **gate))
        first = confirmations[0]["evaluated_at"] if confirmations else None
        allowed = next((row["evaluated_at"] for row in decisions if row["status"] == "passed"), None)
        candidates.append(
            dict(
                code=stock["code"],
                name=stock.get("name", ""),
                A_confirmed_at=first,
                B_confirmed_at=first,
                C_confirmed_at=allowed,
                decisions=decisions,
                C_status="passed"
                if allowed
                else "unknown"
                if any(r["status"] == "unknown" for r in decisions)
                else "rejected"
                if decisions
                else "baseline_not_confirmed",
                confirmation_delay_seconds=(timestamp(allowed) - timestamp(first)).total_seconds()
                if first and allowed
                else None,
            )
        )
    original = {r["code"] for r in candidates if r["A_confirmed_at"]}
    retained = {r["code"] for r in candidates if r["C_confirmed_at"]}
    unknown = {r["code"] for r in candidates if r["C_status"] == "unknown"}
    return dict(
        market_date=date,
        until=inputs["request"]["until"],
        policy_hash=fingerprint(policy),
        input_hash=fingerprint(inputs),
        baseline_complete=baseline.get("complete", False),
        baseline_failures=baseline.get("failures", []),
        A=dict(confirmed_count=len(original)),
        B=dict(confirmed_count=len(original), identical_to_A=True),
        C=dict(
            confirmed_count=len(retained), rejected_count=len(original - retained - unknown), unknown_count=len(unknown)
        ),
        candidates=candidates,
        profitability_measured=False,
        formal_eligible=False,
        notification_sent=False,
        account_modified=False,
        remote_fetch_performed=False,
        limitations=[
            "C filters existing confirmation transitions; it does not rerun or enable a new live strategy",
            "Unknown required evidence is never counted as passing",
            "No fills, realized returns, drawdown or economic improvement are inferred from rule confirmations",
        ],
    )


def compare_many(inputs_list, policy, *, baselines=None):
    policy = validate_policy(policy)
    dates = [row["request"]["market_date"] for row in inputs_list]
    if not dates or len(set(dates)) != len(dates):
        raise ValueError("Expected one frozen input per evaluation day")
    if policy.get("calibration_end") and policy["calibration_end"] >= min(dates):
        raise ValueError("Calibration must precede every evaluation date")
    reports = [
        compare_day(value, policy, baseline=baselines[index] if baselines else None)
        for index, value in enumerate(inputs_list)
    ]
    return summarize_days(reports, policy)


def summarize_days(reports, policy):
    policy = validate_policy(policy)
    dates = [row["market_date"] for row in reports]
    if not dates or len(set(dates)) != len(dates):
        raise ValueError("Expected distinct evaluation dates")
    if policy.get("calibration_end") and policy["calibration_end"] >= min(dates):
        raise ValueError("Calibration must precede every evaluation date")
    complete = sum(row["baseline_complete"] for row in reports)
    evidence_days = sum(
        row["baseline_complete"] and row["A"]["confirmed_count"] > 0 and row["C"]["unknown_count"] == 0
        for row in reports
    )
    delays = [
        r["confirmation_delay_seconds"]
        for report in reports
        for r in report["candidates"]
        if r["confirmation_delay_seconds"] is not None
    ]
    return dict(
        schema_version=1,
        mode="offline_observational_ablation",
        policy=policy,
        policy_hash=fingerprint(policy),
        dates=sorted(dates),
        day_count=len(dates),
        complete_days=complete,
        engineering_sample_target=10,
        evidence_evaluable_days=evidence_days,
        engineering_sample_target_met=evidence_days >= 10,
        out_of_sample_verified=False,
        mean_confirmation_delay_seconds=mean(delays) if delays else None,
        days=reports,
        profitability_measured=False,
        economic_metrics=dict(
            net_return=None,
            max_drawdown=None,
            fill_rate=None,
            false_breakout_rate=None,
            reason="Requires matched execution/backtest outcomes under the same costs",
        ),
        formal_eligible=False,
        production_policy_changed=False,
    )
