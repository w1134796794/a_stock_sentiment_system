from __future__ import annotations

import copy
import json
import importlib
from dataclasses import asdict, replace

import pandas as pd
import pytest

from core.operations.ledger import TradingLedger
from core.signals.case_features import FEATURE_VERSION, event_time
from core.signals.case_features import clean
from core.signals.case_repository import SignalCaseRepository
from core.signals.similar_case_service import SimilarCaseService, SimilarityConfig, publish_case_index, load_case_index
from core.signals.similarity_validation import (SimilarityValidator, ValidationConfig, fit_calibrator,
                                              evaluate_frozen_model, validation_metrics)


def record(date, index=0, *, net=None, pit=True, available=None):
    stamp = event_time(date, "09:40:00")
    end = event_time(date, "15:00:00")
    features = {"market_score": 60 + index % 2 * 10, "profit_effect_score": 50, "emotion_phase": "active",
                "leader_score": 60 + index % 2 * 10, "board_height": 2, "limit_pct": 10,
                "amount_ratio_5d": 1.5 + index % 2 * .5, "vwap_distance": .01,
                "open_gap_pct": .02, "resonance_sectors": "chip", "confirm_close": 10}
    case = {"case_id": f"{date}_{index}", "signal_id": f"signal_{date}_{index}", "code": str(index + 1).zfill(6),
            "strategy_id": "weak_to_strong", "entry_mode": "weak_only", "feature_version": FEATURE_VERSION,
            "config_version": "cfg1", "confirm_at": stamp, "feature_cutoff_at": stamp, "market_date": date,
            "candidate_date": date, "pit_verified": pit, "features": features,
            "quality": {"feature_coverage": .9, "execution_precision": "minute_point"},
            "origin": "live" if pit else "reconstructed"}
    labels = {"net_return": {"state": "mature", "net_return": (.1 if index % 2 else -.05) if net is None else net,
                            "label_end_at": end, "available_at": available or event_time(date, "20:00:00")}}
    return {"case": case, "labels": labels, "versions": {"cost_version": "c1", "exit_rule_version": "e1", "outcome_version": "o1"},
            "progress_state": "complete"}


def sample(days=14, per_day=4):
    dates = pd.bdate_range("2026-01-01", periods=days).strftime("%Y%m%d").tolist()
    return dates, [record(day, i) for day in dates for i in range(per_day)]


def config():
    return SimilarityConfig(neighbors=100, min_samples=4, min_days=2, min_effective=3, bootstrap_draws=20)


def test_future_availability_versions_and_pending_never_enter_pool():
    dates, rows = sample()
    query = record("20260210")
    future = record("20260209", 90, available="2026-02-11T20:00:00")
    bad_version = record(dates[0], 91)
    bad_version["versions"]["cost_version"] = "other"
    pending = record(dates[0], 92)
    pending["labels"]["net_return"]["state"] = "pending"
    model = SimilarCaseService(rows + [future, bad_version, pending], config()).fit(as_of=query["case"]["confirm_at"])
    result = model.evaluate(query)
    assert result["status"] == "estimated"
    assert future["case"]["case_id"] not in result["sample_case_ids"]
    assert bad_version["case"]["case_id"] not in result["sample_case_ids"]
    assert pending["case"]["case_id"] not in result["sample_case_ids"]


def test_research_is_explicit_and_cannot_authorize_forward_model():
    _, rows = sample()
    for r in rows:
        r["case"]["pit_verified"] = False
        r["labels"]["net_return"]["available_at"] = "2026-10-01T20:00:00"
    query = record("20260210", pit=False)
    cutoff = query["case"]["confirm_at"]
    assert SimilarCaseService(rows, config()).fit(as_of=cutoff).evaluate(query)["probability"] is None
    result = SimilarCaseService(rows, config()).fit(as_of=cutoff, research=True).evaluate(query)
    assert result["classification"] == "research_event_time"
    assert result["probability"] is not None
    assert evaluate_frozen_model({"available_at": "2026-02-01T00:00:00", "research": True, "release_allowed": True},
                                 query, as_of=cutoff)["probability"] is None


def test_frozen_features_not_absolute_price_and_reproducible():
    _, rows = sample()
    query = record("20260210")
    model = SimilarCaseService(rows, config()).fit(as_of=query["case"]["confirm_at"])
    first = model.evaluate(query)
    query["case"]["features"]["confirm_close"] = 100000
    second = model.evaluate(query)
    assert first == second
    assert {m["success"] for m in first["matches"]} == {True, False}
    assert first["probability_interval"]["low"] < first["probability_interval"]["high"]


def test_hundred_same_day_cases_do_not_replace_independent_days():
    rows = [record("20260101", i) for i in range(100)]
    query = record("20260210")
    result = SimilarCaseService(rows, config()).fit(as_of=query["case"]["confirm_at"]).evaluate(query)
    assert result["probability"] is None
    assert result["reliability"]["trading_days"] == 1


def test_distance_fallback_and_all_wins_have_no_infinite_advantage():
    _, rows = sample()
    for row in rows:
        row["labels"]["net_return"]["net_return"] = .1
    query = record("20260210")
    query["case"]["features"]["market_score"] = -10000
    query["case"]["features"]["leader_score"] = -10000
    result = SimilarCaseService(rows, config()).fit(as_of=query["case"]["confirm_at"]).evaluate(query)
    assert result["fallback_level"] == "strategy_mode_phase"
    assert result["payoff_ratio"] is None and result["profit_factor"] is None
    assert result["probability_interval"]["low"] < 1
    assert result["matches"][0]["group_reference_only"]


def test_future_secondary_labels_cannot_change_frozen_model():
    _, rows = sample()
    query = record("20260210")
    cutoff = query["case"]["confirm_at"]
    first = SimilarCaseService(rows, config()).fit(as_of=cutoff)
    changed = copy.deepcopy(rows)
    changed[0]["labels"]["return_d5"] = {"state": "mature", "value": 999,
        "label_end_at": "2026-02-20T15:00:00", "available_at": "2026-02-20T20:00:00"}
    second = SimilarCaseService(changed, config()).fit(as_of=cutoff)
    assert first.model_version == second.model_version
    assert first.evaluate(query) == second.evaluate(query)


def test_index_bounded_and_forecast_matches_immutable(tmp_path):
    dates, records = sample(4)
    repo = SignalCaseRepository(tmp_path / "cases.duckdb")
    for r in records:
        repo.record(r["case"])
        repo.save_progress(r["case"]["case_id"], "complete", dates[-1], r["versions"])
        repo.save_outcome(r["case"]["case_id"], "net_return", r["labels"]["net_return"])
    publication = publish_case_index(repo, tmp_path, as_of="2026-02-10T20:00:00", max_cases=7)
    assert publication["record_count"] == 7
    assert len(load_case_index(tmp_path / "matching_index.json")["records"]) == 7
    query = record("20260210")
    prediction = SimilarCaseService(records, config()).fit(as_of=query["case"]["confirm_at"]).evaluate(query)
    ledger = TradingLedger(tmp_path / "ledger.sqlite")
    saved = ledger.save_case_forecast(prediction)
    prediction["probability"] = 999
    prediction["matches"] = []
    assert ledger.save_case_forecast(prediction) == saved
    with ledger._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM signal_case_matches").fetchone()[0] == len(saved["matches"])


def test_calibration_uses_only_labels_before_boundary():
    dates, records = sample(12)
    by_id = {r["case"]["case_id"]: r for r in records}
    predictions = [{"case_id": r["case"]["case_id"], "probability": .8 if r["labels"]["net_return"]["net_return"] > 0 else .2}
                   for r in records]
    cfg = ValidationConfig(min_calibration_samples=8, min_calibration_days=2)
    calibration = fit_calibrator(predictions, by_id, as_of="2026-02-10T00:00:00", research=False, config=cfg)
    assert calibration["status"] == "fitted" and calibration["coefficient"] > 0
    assert fit_calibrator(predictions, by_id, as_of="2026-01-01T09:00:00", research=False, config=cfg)["status"] == "insufficient"


def test_rolling_windows_are_purged_and_test_cannot_select_or_calibrate(tmp_path):
    dates, records = sample(30)
    cfg = ValidationConfig(train_days=12, calibration_days=5, test_days=5, gap_days=1,
        min_calibration_samples=4, min_calibration_days=2, min_test_days=2)
    ledger = TradingLedger(tmp_path / "ledger.sqlite")
    validator = SimilarityValidator(records, config=cfg, candidates=[config(), replace(config(), neighbors=20)],
                                    ledger=ledger, frozen_at="2025-12-01T00:00:00")
    records[0]["labels"]["net_return"]["label_end_at"] = event_time(dates[15], "15:00:00")
    records[0]["labels"]["net_return"]["available_at"] = event_time(dates[15], "20:00:00")
    report, models, predictions = validator.run(dates, as_of="2026-03-01T00:00:00")
    fold = report["folds"][0]
    assert fold["purged_training_cases"] >= 1
    assert set(fold["training_case_ids"]).isdisjoint(fold["calibration"]["case_ids"])
    assert fold["train_dates"][-1] < fold["calibration_dates"][0] < fold["test_dates"][0]
    changed = copy.deepcopy(records)
    for r in changed:
        if r["case"]["market_date"] in fold["test_dates"]:
            r["labels"]["net_return"]["net_return"] *= -100
    other, other_models, other_predictions = SimilarityValidator(changed, config=cfg,
        candidates=validator.candidates, frozen_at=validator.frozen_at).run(dates, as_of="2026-03-01T00:00:00")
    assert other_models[0]["model_id"] == models[0]["model_id"]
    assert other["folds"][0]["selection"] == fold["selection"]
    assert [p["probability"] for p in predictions[:20]] == [p["probability"] for p in other_predictions[:20]]
    assert ledger.experiment(report["experiment_id"])["report"]["kind"] == "analog_validation"
    assert report["account_comparison"]["status"] == "not_run"


def test_historical_freeze_and_sparse_data_never_release():
    dates, records = sample(25)
    cfg = ValidationConfig(train_days=10, calibration_days=5, test_days=5, gap_days=1,
                           min_calibration_samples=4, min_calibration_days=2)
    report, models, _ = SimilarityValidator(records, config=cfg, candidates=[config()]).run(dates, as_of="2026-03-01T00:00:00")
    assert all(not m["release_allowed"] for m in models)
    assert all(f["classification"] == "pit_time_split_replay" for f in report["folds"])
    with pytest.raises(ValueError):
        SimilarityValidator(records, config=cfg).run(dates[:5], as_of="2026-03-01T00:00:00")


def test_forward_model_requires_publication_and_fails_expired():
    _, rows = sample()
    query = record("20260210")
    model = SimilarCaseService(rows, config()).fit(as_of="2026-02-01T00:00:00")
    artifact = {"model_id": "model1", "model_version": model.model_version, "scales": model.scales,
        "training_records": model.training, "config": asdict(config()), "fitted_at": model.fitted_at,
        "available_at": "2026-02-09T20:00:00", "logical_effective_at": "2026-02-01T00:00:00",
        "release_allowed": True, "research": False, "expiry_days": 30,
        "calibration": {"status": "fitted", "coefficient": 1., "intercept": 0.}}
    assert evaluate_frozen_model(artifact, query, as_of="2026-02-09T20:00:00")["probability"] is None
    artifact = clean(artifact)
    assert isinstance(artifact["config"]["neighbors"], int)
    assert evaluate_frozen_model(artifact, query, as_of=query["case"]["confirm_at"])["calibration_status"] == "fitted"
    assert evaluate_frozen_model(artifact, query, as_of="2026-05-01T10:00:00")["status"] == "expired"


def test_model_publication_is_immutable_and_shadow_not_live_authorization(tmp_path):
    _, rows = sample()
    query = record("20260210")
    model = SimilarCaseService(rows, config()).fit(as_of="2026-02-01T00:00:00")
    artifact = {"model_id": "model1", "model_version": model.model_version, "scales": model.scales,
        "training_records": model.training, "config": asdict(config()), "fitted_at": model.fitted_at,
        "available_at": "2026-02-09T20:00:00", "logical_effective_at": "2026-02-01T00:00:00",
        "release_allowed": False, "research": False, "expiry_days": 30, "calibration": {"status": "insufficient"}}
    ledger = TradingLedger(tmp_path / "ledger.sqlite")
    assert ledger.save_case_model(artifact)["available_at"] == artifact["available_at"]
    assert ledger.save_case_model({**artifact, "available_at": "2026-03-01T00:00:00"})["available_at"] == artifact["available_at"]
    assert evaluate_frozen_model(artifact, query, as_of=query["case"]["confirm_at"])["status"] == "not_ready"
    prediction = evaluate_frozen_model(artifact, query, as_of=query["case"]["confirm_at"], shadow=True)
    assert prediction["classification"] == "forward_shadow"
    assert not prediction["trading_gate_enabled"]


def test_analog_experiments_do_not_break_existing_strategy_dropdown(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.sqlite")
    ledger.save_experiment("rule", "2026-01-01", "v", "d", {"folds": []})
    ledger.save_experiment("analog", "2026-01-01", "v", "d", {"kind": "analog_validation", "folds": []})
    assert [r["experiment_id"] for r in ledger.list_experiments(kind="strategy")] == ["rule"]
    assert [r["experiment_id"] for r in ledger.list_experiments(kind="analog")] == ["analog"]


def test_analog_report_api_only_reads_frozen_report(tmp_path, monkeypatch):
    app_module = importlib.import_module("web.app")
    ledger = TradingLedger(tmp_path / "ledger.sqlite")
    report = {"kind": "analog_validation", "folds": [{"metrics": {"brier": .2}}]}
    ledger.save_experiment("analog", "2026-01-01", "v", "d", report)
    monkeypatch.setattr(app_module, "_trading_ledger", lambda: ledger)
    assert json.loads(app_module.api_operations_analog_validation("analog").body) == report
    assert app_module.api_operations_analog_validation("missing").status_code == 404
