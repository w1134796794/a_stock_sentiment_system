"""A/B equivalence, publication-time C gates and immutable experiment inputs."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from core.realtime.evidence_experiment import compare_day, compare_many, gate_at, validate_policy
from core.realtime.evidence_outcomes import attach_outcomes
from core.realtime.market_evidence import EvidenceResult, fingerprint, normalize_depth

DATE = "20261023"
AT = "2026-10-23T09:36:03+08:00"
POLICY = dict(schema_version=1, filters={"min_opening_amount_yuan": 1000}, max_age_seconds=15)


def inputs():
    return dict(
        request=dict(market_date=DATE, until="09:40:00"), plan=dict(plan_id="frozen"), events=[], evidence_events=[]
    )


def baseline():
    return dict(
        complete=True,
        failures=[],
        stocks=[dict(code="000001", name="test", transitions=[dict(signal_status="confirmed", evaluated_at=AT)])],
    )


def evidence(value, published=AT):
    value = value.envelope(plan_id="frozen")
    value["observation_id"] = fingerprint(value)
    marker = dict(
        code=value["code"],
        market_date=DATE,
        received_at=published,
        capture_mode="live",
        collection_plan_id="frozen",
        observation_id=value["observation_id"],
    )
    return [
        dict(event_id="a", kind=value["kind"], code=value["code"], received_at=value["received_at"], payload=value),
        dict(event_id="b", kind="evidence_publication", code=value["code"], received_at=published, payload=marker),
    ]


def opening(amount):
    return EvidenceResult(
        "opening_match",
        "000001",
        DATE,
        received_at="2026-10-23T09:25:00+08:00",
        rows=[dict(event_time="2026-10-23T09:25+08:00", amount_yuan=amount, event_kind="opening_match")],
    )


def test_missing_required_evidence_is_unknown_not_pass():
    report = compare_day(inputs(), POLICY, baseline=baseline())
    assert report["B"]["identical_to_A"]
    assert report["A"]["confirmed_count"] == report["B"]["confirmed_count"] == 1
    assert report["C"]["confirmed_count"] == 0 and report["C"]["unknown_count"] == 1
    assert not report["profitability_measured"] and not report["formal_eligible"]


@pytest.mark.parametrize("amount,status", [(1000, "passed"), (999, "rejected")])
def test_c_uses_only_available_formal_opening(amount, status):
    value = inputs()
    value["evidence_events"] = evidence(opening(amount))
    report = compare_day(value, POLICY, baseline=baseline())
    assert report["candidates"][0]["C_status"] == status
    assert report["A"]["confirmed_count"] == 1


def test_later_publication_cannot_improve_earlier_decision():
    value = inputs()
    value["evidence_events"] = evidence(opening(2000), published="2026-10-23T09:37:00+08:00")
    assert gate_at(value, "000001", AT, validate_policy(POLICY))["status"] == "unknown"


def test_snapshot_depth_requires_explicit_receipt_visibility():
    value = inputs()
    book = normalize_depth(NS(buy_levels=[NS(price=10, volume=10)] * 5, sell_levels=[NS(price=10.01, volume=10)] * 5))
    value["events"] = [
        dict(
            kind="snapshot",
            code="000001",
            received_at=AT,
            payload=dict(code="000001", date=DATE, received_at=AT, depth=book),
        )
    ]
    policy = dict(schema_version=1, filters={"max_spread_bps": 20})
    assert gate_at(value, "000001", AT, validate_policy(policy))["status"] == "unknown"
    policy["depth_visibility"] = "snapshot_receipt"
    result = gate_at(value, "000001", AT, validate_policy(policy))
    assert result["status"] == "passed" and not result["publication_required"]
    assert result["checks"]["max_spread_bps"]["visibility_basis"] == "snapshot_receipt_only"


@pytest.mark.parametrize(
    "policy",
    [
        {},
        dict(schema_version=1, filters={}),
        dict(schema_version=1, filters={"unknown": 1}),
        dict(schema_version=1, filters={"min_buy_amount_share": 1.1}),
        dict(schema_version=1, filters={"max_spread_bps": float("nan")}),
        dict(schema_version=1, filters={"max_spread_bps": True}),
        dict(schema_version=1, filters={"max_spread_bps": 1}, max_age_seconds=0),
    ],
)
def test_invalid_policy_is_rejected(policy):
    with pytest.raises(ValueError):
        validate_policy(policy)


def test_calibration_overlap_is_rejected():
    policy = {**POLICY, "calibration_end": DATE}
    with pytest.raises(ValueError):
        compare_many([inputs()], policy, baselines=[baseline()])


def test_ten_days_is_engineering_target_not_oos_or_profit_proof():
    data = []
    bases = []
    for day in range(10, 20):
        value = inputs()
        value["request"]["market_date"] = f"202610{day}"
        base = baseline()
        base["stocks"][0]["transitions"] = []
        data.append(value)
        bases.append(base)
    result = compare_many(data, POLICY, baselines=bases)
    assert not result["engineering_sample_target_met"]
    assert result["evidence_evaluable_days"] == 0
    assert not result["out_of_sample_verified"] and not result["profitability_measured"]


def test_outcome_matching_prices_cost_and_group_identity():
    value = inputs()
    value["evidence_events"] = evidence(opening(2000))
    report = compare_many([value], POLICY, baselines=[baseline()])
    report["frozen_inputs_hash"] = "hash"
    outcomes = dict(
        schema_version=1,
        frozen_inputs_hash="hash",
        policy_hash=report["policy_hash"],
        source_run_id="backtest",
        round_trip_cost_bps=10,
        rows=[
            dict(
                market_date=DATE,
                code="000001",
                group=group,
                confirmed_at=AT,
                filled=True,
                entry_at="2026-10-23T09:37:00+08:00",
                exit_at="2026-10-26T10:00:00+08:00",
                entry_price=10,
                exit_price=11,
            )
            for group in ("A", "C")
        ],
    )
    attach_outcomes(report, outcomes)
    assert report["profitability_measured"]
    assert report["imported_outcomes"]["groups"]["A"]["mean_filled_trade_net_return_pct"] == pytest.approx(9.9)
    assert report["imported_outcomes"]["groups"]["A"]["portfolio_max_drawdown"] is None
    bad = deepcopy(outcomes)
    bad["rows"][0]["entry_at"] = AT
    with pytest.raises(ValueError):
        attach_outcomes(report, bad)


def test_outcomes_cannot_be_reused_for_different_inputs():
    with pytest.raises(ValueError):
        attach_outcomes(dict(frozen_inputs_hash="a"), dict(schema_version=1, frozen_inputs_hash="b"))


def test_cli_saved_inputs_are_immutable_and_reused(tmp_path, monkeypatch, capsys):
    import core.realtime.evidence_experiment as experiment
    import core.realtime.snapshot_replay as replay
    import scripts.evaluate_eltdx_evidence as module

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY))
    frozen = inputs()
    monkeypatch.setattr(replay, "load_snapshot_inputs", lambda *a, **kw: frozen)
    monkeypatch.setattr(
        experiment,
        "compare_day",
        lambda inp, p: dict(
            market_date=DATE,
            baseline_complete=True,
            candidates=[],
            A={"confirmed_count": 0},
            B={"confirmed_count": 0},
            C={"confirmed_count": 0, "unknown_count": 0},
        ),
    )
    output = tmp_path / "experiment.json"
    monkeypatch.setattr(sys, "argv", ["evaluate", "--dates", DATE, "--policy", str(policy), "--output", str(output)])
    assert module.main() == 0
    first = json.loads(output.read_text())
    path = tmp_path / first["frozen_inputs"][0]["file"]
    assert path.exists() and first["frozen_inputs_hash"] == fingerprint([fingerprint(frozen)])
    monkeypatch.setattr(
        replay,
        "load_snapshot_inputs",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not refresh archives")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate", "--saved-inputs", str(output), "--policy", str(policy), "--output", str(tmp_path / "second.json")],
    )
    assert module.main() == 0
    path.write_text("{}")
    with pytest.raises(ValueError):
        module.main()
    capsys.readouterr()


def test_engineering_target_requires_evaluable_days_and_does_not_claim_profit():
    from core.realtime.evidence_experiment import summarize_days

    reports = [
        dict(
            market_date=f"202610{day}",
            baseline_complete=True,
            A={"confirmed_count": 1},
            C={"unknown_count": 0},
            candidates=[],
        )
        for day in range(10, 20)
    ]
    report = summarize_days(reports, POLICY)
    assert report["evidence_evaluable_days"] == 10 and report["engineering_sample_target_met"]
    assert not report["profitability_measured"] and not report["out_of_sample_verified"]
