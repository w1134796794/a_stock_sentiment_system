"""Purged train/calibration/test validation. Research replay is never PIT proof."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from core.operations.ledger import stable_id
from core.signals.case_features import clean, event_time
from core.signals.similar_case_service import SimilarCaseService, SimilarityConfig, block_weights, known_label, number

VALIDATION_VERSION = "analog_purged_sigmoid_v1"


@dataclass(frozen=True)
class ValidationConfig:
    train_days: int = 60
    calibration_days: int = 20
    test_days: int = 20
    gap_days: int = 5
    min_calibration_samples: int = 40
    min_calibration_days: int = 10
    min_test_days: int = 10
    min_test_coverage: float = .3
    max_brier_degradation: float = 0
    expiry_days: int = 30
    forecast_latency_seconds: int = 60

    def __post_init__(self):
        if (min(self.train_days, self.calibration_days, self.test_days) < 2 or self.gap_days < 0
                or min(self.min_calibration_samples, self.min_calibration_days, self.min_test_days) < 2
                or not 0 < self.min_test_coverage <= 1 or self.max_brier_degradation < 0 or self.expiry_days < 1
                or self.forecast_latency_seconds < 0):
            raise ValueError("Invalid rolling validation contract")


def logit(probabilities):
    values = np.clip(np.asarray(probabilities, dtype=float), .001, .999)
    return np.log(values / (1 - values))


def calibrate(probability, calibration):
    if probability is None or calibration.get("status") != "fitted":
        return probability
    score = float(logit([probability])[0]) * calibration["coefficient"] + calibration["intercept"]
    return float(1 / (1 + np.exp(-np.clip(score, -40, 40))))


def fit_calibrator(predictions, records_by_id, *, as_of, research, config):
    pairs = []
    for prediction in predictions:
        label = known_label(records_by_id[prediction["case_id"]], "net_return", as_of, research=research)
        if prediction.get("probability") is not None and label:
            pairs.append((prediction, label))
    days = {records_by_id[p["case_id"]]["case"]["market_date"] for p, _ in pairs}
    result = {"status": "insufficient", "method": "sigmoid_logit", "samples": len(pairs),
              "trading_days": len(days), "case_ids": sorted(p["case_id"] for p, _ in pairs), "fitted_as_of": as_of}
    labels = [float(label["net_return"]) > 0 for _, label in pairs]
    if len(pairs) < config.min_calibration_samples or len(days) < config.min_calibration_days or len(set(labels)) < 2:
        return result
    from sklearn.linear_model import LogisticRegression
    counts = {day: sum(records_by_id[p["case_id"]]["case"]["market_date"] == day for p, _ in pairs) for day in days}
    weights = [len(pairs) / len(days) / counts[records_by_id[p["case_id"]]["case"]["market_date"]] for p, _ in pairs]
    model = LogisticRegression(C=1, solver="lbfgs", random_state=0)
    model.fit(logit([p["probability"] for p, _ in pairs]).reshape(-1, 1), labels, sample_weight=weights)
    coefficient = float(model.coef_[0, 0])
    # A reversed mapping is not silently promoted to a live edge.
    return {**result, "status": "fitted" if coefficient > 0 else "degraded",
            "coefficient": coefficient, "intercept": float(model.intercept_[0])}


def validation_metrics(predictions, records_by_id, *, as_of, research):
    evaluated = []
    for p in predictions:
        label = known_label(records_by_id[p["case_id"]], "net_return", as_of, research=research)
        if label:
            evaluated.append((p, float(label["net_return"])))
    ready = [(p, value) for p, value in evaluated if p.get("probability") is not None]
    records = [records_by_id[p["case_id"]] for p, _ in ready]
    weights = block_weights(records, [1.] * len(records))
    total = float(weights.sum())
    def weighted(values):
        return float(np.average(values, weights=weights)) if total else None
    bins = []
    for low in np.arange(0, 1, .2):
        indices = [i for i, (p, _) in enumerate(ready) if low <= p["probability"] < low + .2
                   or (low > .79 and p["probability"] == 1)]
        if indices:
            w = weights[indices]
            bins.append({"low": float(low), "high": float(low + .2), "count": len(indices),
                         "predicted": float(np.average([ready[i][0]["probability"] for i in indices], weights=w)),
                         "observed": float(np.average([ready[i][1] > 0 for i in indices], weights=w)),
                         "weight": float(w.sum() / total)})
    baseline = [(i, p["baseline"]["probability"]) for i, (p, _) in enumerate(ready) if p.get("baseline")]
    common_brier = baseline_brier = None
    if baseline:
        indices = [i for i, _ in baseline]
        w = weights[indices]
        baseline_brier = float(np.average([(prob - (ready[i][1] > 0)) ** 2 for i, prob in baseline], weights=w))
        common_brier = float(np.average([(ready[i][0]["probability"] - (ready[i][1] > 0)) ** 2 for i, _ in baseline], weights=w))
    ranked = sorted(ready, key=lambda item: (item[0].get("expected_net_return") or 0, item[0]["case_id"]))
    ranking = []
    for i, part in enumerate(np.array_split(np.arange(len(ranked)), 3)):
        subset = [ranked[int(j)] for j in part]
        if subset:
            r = [records_by_id[p["case_id"]] for p, _ in subset]
            w = block_weights(r, [1.] * len(r))
            v = np.array([value for _, value in subset])
            gain = float(np.average(v[v > 0], weights=w[v > 0])) if (v > 0).any() else None
            loss = -float(np.average(v[v < 0], weights=w[v < 0])) if (v < 0).any() else None
            ranking.append({"group": i + 1, "count": len(subset), "mean_net_return": float(np.average(v, weights=w)),
                            "payoff_ratio": gain / loss if gain is not None and loss else None,
                            "p05_net_return": float(np.quantile(v, .05))})
    return {"target_count": len(predictions), "mature_outcome_count": len(evaluated), "scored_count": len(ready),
            "prediction_coverage": len([p for p in predictions if p.get("probability") is not None]) / len(predictions) if predictions else 0,
            "trading_days": len({r["case"]["market_date"] for r in records}),
            "brier": weighted([(p["probability"] - (value > 0)) ** 2 for p, value in ready]),
            "simple_baseline_brier": baseline_brier, "common_case_brier": common_brier,
            "baseline_common_count": len(baseline),
            "calibration_error": sum(row["weight"] * abs(row["predicted"] - row["observed"]) for row in bins) if bins else None,
            "reliability_bins": bins, "return_ranking": ranking,
            "fallback_rate": sum(p.get("fallback_level") != "similar_cases" for p, _ in ready) / len(ready) if ready else None,
            "existing_rules": {"basis": "all_standalone_confirmed_trades", "closed_count": len(evaluated),
                               "mean_net_return": float(np.mean([value for _, value in evaluated])) if evaluated else None,
                               "probability": None, "note": "Rules have no calibrated probability; trade returns are not account NAV."}}


class SimilarityValidator:
    def __init__(self, records, *, config=None, candidates=None, ledger=None, frozen_at=None):
        self.input_count = len(records)
        self.records = [r for r in records if all(r.get("versions", {}).get(k) for k in
                        ("cost_version", "exit_rule_version", "outcome_version"))]
        self.by_id = {r["case"]["case_id"]: r for r in self.records}
        if len(self.by_id) != len(self.records):
            raise ValueError("Duplicate case IDs in validation input")
        self.config = config or ValidationConfig()
        self.candidates = candidates or [SimilarityConfig(neighbors=20), SimilarityConfig(neighbors=40)]
        self.ledger = ledger
        self.frozen_at = event_time("", frozen_at or datetime.now().isoformat())
        files = [Path(__file__), Path(__file__).with_name("similar_case_service.py"), Path(__file__).with_name("case_features.py")]
        self.implementation_version = hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()
        self.contract_version = stable_id(VALIDATION_VERSION, self.implementation_version, asdict(self.config),
                                          [asdict(c) for c in self.candidates])

    def _select(self, records, dates, research):
        split = max(1, len(dates) * 2 // 3)
        cutoff = event_time(dates[split], "00:00:00")
        inner_train = [r for r in records if r["case"]["market_date"] < dates[split]]
        targets = [r for r in records if r["case"]["market_date"] >= dates[split]]
        end = event_time(dates[-1], "23:59:59")
        trials = []
        for candidate in self.candidates:
            model = SimilarCaseService(inner_train, candidate).fit(as_of=cutoff, research=research)
            predictions = [model.evaluate(r) for r in targets]
            metrics = validation_metrics(predictions, self.by_id, as_of=end, research=research)
            score = (metrics["brier"] + .25 * (1 - metrics["prediction_coverage"])) if metrics["brier"] is not None else None
            trials.append({"config": asdict(candidate), "train_only_score": score, "metrics": metrics})
        scores = [(trial["train_only_score"], i) for i, trial in enumerate(trials) if trial["train_only_score"] is not None]
        chosen = min(scores)[1] if scores else 0
        return self.candidates[chosen], {"trials": trials, "selected": chosen,
                                        "selection_status": "train_only" if scores else "predeclared_default_insufficient",
                                        "inner_cutoff": cutoff}

    def run(self, dates: list[str], *, as_of: str, research=False, account_comparison=None):
        if dates != sorted(set(dates)):
            raise ValueError("Use ordered unique trading days from the real calendar")
        as_of = event_time("", as_of)
        if not as_of or not self.frozen_at:
            raise ValueError("Valid report and contract timestamps required")
        cfg = self.config
        first = cfg.train_days + cfg.gap_days + cfg.calibration_days + cfg.gap_days
        folds, models, all_predictions = [], [], []
        for offset in range(first, len(dates), cfg.test_days):
            test = dates[offset:offset + cfg.test_days]
            if len(test) < cfg.test_days:
                break  # No partial window silently compared with full windows.
            cal_end = offset - cfg.gap_days
            cal = dates[cal_end - cfg.calibration_days:cal_end]
            train_end = cal_end - cfg.calibration_days - cfg.gap_days
            train = dates[train_end - cfg.train_days:train_end]
            cal_start, test_start = event_time(cal[0], "00:00:00"), event_time(test[0], "00:00:00")
            raw_train = [r for r in self.records if r["case"]["market_date"] in train]
            # All outcome horizons are guarded individually; the net label governs purging here.
            train_records = [r for r in raw_train if known_label(r, "net_return", cal_start, research=research)]
            chosen, selection = self._select(train_records, train, research)
            model = SimilarCaseService(train_records, chosen).fit(as_of=cal_start, research=research)
            cal_records = [r for r in self.records if r["case"]["market_date"] in cal]
            calibration_predictions = [model.evaluate(r) for r in cal_records]
            calibration = fit_calibrator(calibration_predictions, self.by_id, as_of=test_start,
                                         research=research, config=cfg)
            model_id = stable_id(VALIDATION_VERSION, model.model_version, calibration, self.contract_version)
            artifact = {"model_id": model_id, "contract_version": self.contract_version,
                "model_version": model.model_version, "config": asdict(chosen), "scales": model.scales,
                "training_records": model.training, "calibration": calibration, "research": research,
                "fitted_at": cal_start, "logical_effective_at": test_start,
                "available_at": datetime.now().isoformat(timespec="milliseconds"), "expiry_days": cfg.expiry_days,
                "release_allowed": False}
            if self.ledger:
                artifact = self.ledger.save_case_model(clean(artifact))
            targets = [r for r in self.records if r["case"]["market_date"] in test]
            predictions = []
            for target in targets:
                prediction = model.evaluate(target)
                prediction["raw_probability"] = prediction["probability"]
                prediction["probability"] = calibrate(prediction["probability"], calibration)
                prediction["calibration_status"] = calibration["status"]
                prediction["frozen_model_id"] = model_id
                prediction["forecast_id"] = stable_id("validated_analog_forecast", target["case"]["case_id"], model_id, prediction["as_of"])
                if calibration["status"] == "degraded":
                    prediction["probability"] = (prediction.get("baseline") or {}).get("probability")
                    prediction["fallback_level"] = "drift_group_baseline"
                if calibration["status"] == "fitted" and prediction.get("probability_interval"):
                    interval = prediction["probability_interval"]
                    prediction["probability_interval"] = {**interval, "low": calibrate(interval["low"], calibration),
                        "high": calibrate(interval["high"], calibration), "calibrator_uncertainty_included": False}
                prediction["classification"] = "research_event_time" if research else "pit_time_split_replay"
                prediction["prediction_generated_at"] = datetime.now().isoformat(timespec="milliseconds")
                if self.ledger:
                    existing = self.ledger.case_forecast(prediction["forecast_id"])
                    if existing:
                        prediction = existing
                predictions.append(prediction)
            metrics = validation_metrics(predictions, self.by_id, as_of=as_of, research=research)
            prospective = bool(targets) and not research and self.frozen_at < test_start and artifact["available_at"] < test_start
            prospective = prospective and all(r["case"].get("pit_verified") for r in targets) and all(
                p.get("prediction_generated_at", "9999") <= (datetime.fromisoformat(self.by_id[p["case_id"]]["case"]["confirm_at"])
                    + timedelta(seconds=cfg.forecast_latency_seconds)).isoformat()
                and p.get("prediction_generated_at", "9999") < self.by_id[p["case_id"]].get("labels", {}).get("net_return", {}).get("label_end_at", "")
                for p in predictions)
            passes = (metrics["trading_days"] >= cfg.min_test_days
                      and metrics["prediction_coverage"] >= cfg.min_test_coverage
                      and metrics["mature_outcome_count"] == metrics["target_count"]
                      and calibration["status"] == "fitted" and metrics["common_case_brier"] is not None
                      and metrics["common_case_brier"] <= metrics["simple_baseline_brier"] + cfg.max_brier_degradation)
            subgroups = {}
            for key in ("strategy_id", "emotion_phase"):
                values = {str((r["case"] if key == "strategy_id" else r["case"].get("features", {})).get(key) or "unknown") for r in targets}
                for value in sorted(values):
                    subset = [p for p in predictions if str((self.by_id[p["case_id"]]["case"] if key == "strategy_id" else
                        self.by_id[p["case_id"]]["case"].get("features", {})).get(key) or "unknown") == value]
                    subgroups[f"{key}:{value}"] = validation_metrics(subset, self.by_id, as_of=as_of, research=research)
            folds.append({"train_dates": train, "calibration_dates": cal, "test_dates": test,
                "purged_training_cases": len(raw_train) - len(train_records),
                "purged_calibration_cases": len(cal_records) - len(calibration["case_ids"]),
                "training_case_ids": [r["case"]["case_id"] for r in model.training],
                "model_id": model_id, "selection": selection, "calibration": calibration,
                "metrics": metrics, "subgroups": subgroups,
                "classification": "prospective_oos" if prospective else "research_event_time" if research else "pit_time_split_replay",
                "statistical_gate_passed": passes, "release_allowed": bool(passes and prospective)})
            models.append({**artifact, "release_allowed": bool(passes and prospective), "validated_at": as_of})
            all_predictions.extend(predictions)
        if not folds:
            raise ValueError(f"Insufficient trading days: need at least {first + cfg.test_days}")
        data_version = stable_id(self.records)
        experiment_id = stable_id("analog_experiment", self.contract_version, data_version, dates, research, as_of, self.frozen_at)
        report = {"experiment_id": experiment_id, "kind": "analog_validation", "status": "evaluated",
            "frozen_at": self.frozen_at, "report_as_of": as_of, "contract_version": self.contract_version,
            "data_version": data_version, "config": asdict(cfg), "folds": folds,
            "classification": "research_event_time" if research else "observed_availability",
            "input_cases": self.input_count, "independent_trade_cases": len(self.records),
            "trading_gate_enabled": False,
            "account_comparison": account_comparison or {"status": "not_run", "reason": "Independent signal statistics are not account NAV; filtered account replay is Phase 6."},
            "warning": "Historical event-time research ignores late publication only in explicitly marked research mode; it cannot authorize production."}
        if self.ledger:
            for prediction in all_predictions:
                self.ledger.save_case_forecast(prediction)
            self.ledger.save_experiment(experiment_id, self.frozen_at, self.contract_version, data_version, clean(report))
        return clean(report), clean(models), clean(all_predictions)


def evaluate_frozen_model(artifact, query, *, as_of, shadow=False):
    """Forward-only scoring; expired/unpublished/research models fail closed."""
    as_of = event_time("", as_of)
    available = event_time("", artifact.get("available_at"))
    if (not as_of or not available or available >= as_of or artifact.get("research")
            or (not artifact.get("release_allowed") and not shadow)
            or (not shadow and artifact.get("validated_at", available) >= as_of)
            or artifact.get("logical_effective_at", "") > as_of):
        return {"status": "not_ready", "probability": None, "reason": "model_not_published_or_research_only"}
    age = (np.datetime64(as_of[:10]) - np.datetime64(available[:10])).astype(int)
    if age > artifact.get("expiry_days", 30):
        return {"status": "expired", "probability": None, "reason": "model_requires_revalidation"}
    model = SimilarCaseService(artifact["training_records"], SimilarityConfig(**artifact["config"]))
    model.training = copy.deepcopy(artifact["training_records"])
    model.scales = copy.deepcopy(artifact["scales"])
    model.model_version, model.fitted_at = artifact["model_version"], artifact["fitted_at"]
    result = model.evaluate(query, as_of=as_of)
    result["raw_probability"] = result["probability"]
    result["probability"] = calibrate(result["probability"], artifact["calibration"])
    result["calibration_status"] = artifact["calibration"]["status"]
    result["frozen_model_id"] = artifact["model_id"]
    result["classification"] = "forward_shadow" if shadow else "forward_validated_reference"
    result["prediction_generated_at"] = datetime.now().isoformat(timespec="milliseconds")
    result["forecast_id"] = stable_id("validated_analog_forecast", query["case"]["case_id"], artifact["model_id"], as_of)
    if result.get("drift_status") == "feature_shift":
        result["probability"] = (result.get("baseline") or {}).get("probability")
        result["calibration_status"] = "degraded_feature_shift"
        result["fallback_level"] = "drift_group_baseline"
    elif result.get("probability_interval") and artifact["calibration"].get("status") == "fitted":
        interval = result["probability_interval"]
        result["probability_interval"] = {**interval, "low": calibrate(interval["low"], artifact["calibration"]),
            "high": calibrate(interval["high"], artifact["calibration"]), "calibrator_uncertainty_included": False}
    return clean(result)
