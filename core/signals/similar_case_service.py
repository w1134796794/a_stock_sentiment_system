"""Version-isolated analog statistics, never a trading gate or a price forecast."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

from core.operations.ledger import stable_id
from core.signals.case_features import clean, event_time

SIMILARITY_VERSION = "analog_day_cluster_v1"
# Each category has equal influence, irrespective of its number of factors.
FEATURE_GROUPS = {
    "market": ("market_score", "profit_effect_score", "promotion_trend_score", "crowding_score"),
    "identity": ("leader_score", "continuity_score", "board_height", "sector_status_score", "safety_score"),
    "daily": ("amount_ratio_5d", "liquidity_score", "new_high_ratio", "capital_recognition_score"),
    "intraday": ("open_gap_pct", "vwap_distance", "opening_high_distance", "amount_pace", "hold_minutes"),
}


@lru_cache(maxsize=16384)
def timestamp(value):
    return event_time("", value)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def known_label(record, label, as_of, *, research=False):
    value = record.get("labels", {}).get(label, {})
    ended = timestamp(value.get("label_end_at"))
    available = timestamp(value.get("available_at"))
    if value.get("state") != "mature" or not ended or not available or available < ended:
        return {}
    # Explicit historical research uses event time, not fabricated publication time.
    if ended >= as_of or (not research and available >= as_of):
        return {}
    return value


def family(record):
    case = record["case"]
    features, quality = case.get("features", {}), case.get("quality", {})
    versions = record.get("versions", {})
    return (case.get("strategy_id"), case.get("entry_mode"), case.get("config_version"),
            case.get("feature_version"), number(features.get("limit_pct")),
            quality.get("execution_precision"), versions.get("cost_version"),
            versions.get("exit_rule_version"), versions.get("outcome_version"))


def phase(record):
    return record["case"].get("features", {}).get("emotion_phase")


def theme(record):
    value = record["case"].get("features", {}).get("resonance_sectors")
    if isinstance(value, list):
        return str(value[0]) if value else "unknown"
    return str(value or "unknown").replace("，", ",").split(",")[0]


def block_weights(records, raw):
    """One day gets at most one unit, split equally among its observed themes."""
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(record["case"]["market_date"], {}).setdefault(theme(record), []).append(i)
    weights = np.zeros(len(records))
    for themes in groups.values():
        for indices in themes.values():
            total = sum(raw[i] for i in indices)
            if total > 0:
                for i in indices:
                    weights[i] = raw[i] / total * min(max(raw[j] for j in indices), 1.0) / len(themes)
    return weights


@dataclass(frozen=True)
class SimilarityConfig:
    neighbors: int = 40
    max_distance: float = 1.2
    min_samples: int = 20
    min_days: int = 8
    min_effective: float = 12
    min_quality: float = .35
    min_common_features: int = 3
    min_common_groups: int = 2
    half_life_days: float = 120
    prior_days: float = 4
    bootstrap_draws: int = 200

    def __post_init__(self):
        if (self.neighbors < 1 or self.max_distance <= 0 or self.min_samples < 2 or self.min_days < 2
                or self.min_effective <= 0 or not 0 <= self.min_quality <= 1
                or self.min_common_features < 1 or self.min_common_groups < 1
                or self.half_life_days <= 0 or self.prior_days < 0 or self.bootstrap_draws < 20):
            raise ValueError("Invalid similarity research configuration")


class SimilarCaseService:
    def __init__(self, records: list[dict], config: SimilarityConfig | None = None):
        self.records = records
        self.config = config or SimilarityConfig()
        self.training = []
        self.scales = {}
        self.model_version = ""
        self.research = False
        self.fitted_at = ""

    def _eligible(self, record, as_of):
        case = record["case"]
        stamp = timestamp(case.get("confirm_at"))
        cutoff = timestamp(case.get("feature_cutoff_at"))
        if not stamp or not cutoff or cutoff > stamp or stamp >= as_of:
            return False
        if not self.research and not case.get("pit_verified"):
            return False
        if case.get("quality", {}).get("sector_observed_after_cutoff"):
            return False
        if (number(case.get("quality", {}).get("feature_coverage")) or 0) < self.config.min_quality:
            return False
        if any(part is None or part == "" for part in family(record)):
            return False
        label = known_label(record, "net_return", as_of, research=self.research)
        return number(label.get("net_return")) is not None

    def fit(self, *, as_of: str, research: bool = False):
        as_of = event_time("", as_of)
        if not as_of:
            raise ValueError("A valid training cutoff is required")
        self.research, self.fitted_at = research, as_of
        # First objective event only; duplicate strategy/run rows cannot inflate n.
        seen = set()
        self.training = []
        for record in sorted(self.records, key=lambda r: (r["case"].get("confirm_at", ""), r["case"]["case_id"])):
            key = (record["case"]["market_date"], record["case"]["code"], family(record))
            if key not in seen and self._eligible(record, as_of):
                self.training.append(record)
                seen.add(key)
        self.training = [{"case": r["case"], "versions": r["versions"],
                          "labels": {label: known_label(r, label, as_of, research=research)
                                     for label in r.get("labels", {})
                                     if known_label(r, label, as_of, research=research)}} for r in self.training]
        # Fit scales separately per hard-compatible family, on past data only.
        self.scales = {}
        for signature in sorted({family(r) for r in self.training}, key=str):
            subset = [r for r in self.training if family(r) == signature]
            scale = {}
            for name in [name for names in FEATURE_GROUPS.values() for name in names]:
                values = [number(r["case"].get("features", {}).get(name)) for r in subset]
                values = [v for v in values if v is not None]
                if values:
                    q25, median, q75 = np.quantile(values, [.25, .5, .75])
                    scale[name] = {"median": float(median), "scale": max(float(q75 - q25), abs(float(median)) * .05, .0001)}
            self.scales[str(signature)] = scale
        self.model_version = stable_id(SIMILARITY_VERSION, asdict(self.config), research, as_of, self.scales,
                                       [(r["case"]["case_id"], r.get("labels"), r.get("versions")) for r in self.training])
        return self

    def _distance(self, query, record):
        left, right = query["case"].get("features", {}), record["case"].get("features", {})
        scale = self.scales.get(str(family(query)), {})
        groups, differences, count = [], [], 0
        for names in FEATURE_GROUPS.values():
            deltas = []
            for name in names:
                a, b = number(left.get(name)), number(right.get(name))
                if a is not None and b is not None and name in scale:
                    delta = min(abs(a - b) / scale[name]["scale"], 6)
                    deltas.append(delta ** 2)
                    differences.append({"feature": name, "standardized_difference": delta})
                    count += 1
            if deltas:
                groups.append(sum(deltas) / len(deltas))
        if count < self.config.min_common_features or len(groups) < self.config.min_common_groups:
            return math.inf, ["insufficient_comparable_features"]
        distance = math.sqrt(sum(groups) / len(groups))
        penalties = []
        for name, penalty in (("emotion_phase", .4), ("lifecycle_state", .3), ("leader_roles", .3)):
            if left.get(name) is not None and right.get(name) is not None and left[name] != right[name]:
                distance += penalty
                penalties.append(name)
        def slot(r):
            time = r["case"]["confirm_at"].split("T")[-1]
            return "early" if time < "10:00:00" else "morning" if time < "11:30:00" else "afternoon"
        if slot(query) != slot(record):
            distance += .3
            penalties.append("confirmation_time_bucket")
        largest = sorted(differences, key=lambda row: (-row["standardized_difference"], row["feature"]))[:3]
        return distance, {"different_categories": penalties, "largest_feature_differences": largest,
                          "comparable_features": count, "comparable_groups": len(groups)}

    def _weights(self, records, as_of, distances=None):
        age = [(np.datetime64(as_of[:10]) - np.datetime64(timestamp(r["case"]["confirm_at"])[:10])).astype(int)
               for r in records]
        raw = [math.exp(-math.log(2) * max(int(days), 0) / self.config.half_life_days)
               * (math.exp(-distance) if distances is not None else 1)
               for days, distance in zip(age, distances if distances is not None else [0] * len(age))]
        return block_weights(records, raw)

    def _reliability(self, records, weights):
        day_weights = {}
        for record, weight in zip(records, weights):
            day = record["case"]["market_date"]
            day_weights[day] = day_weights.get(day, 0.) + float(weight)
        days = len(day_weights)
        effective = float(weights.sum() ** 2 / np.square(weights).sum()) if weights.sum() else 0
        return {"raw_sample_count": len(records), "effective_sample_count": effective,
                "trading_days": days, "theme_count": len({theme(r) for r in records}),
                "max_day_weight_share": max(day_weights.values(), default=0) / float(weights.sum()) if weights.sum() else 0,
                "sufficient": len(records) >= self.config.min_samples and days >= self.config.min_days
                              and effective >= self.config.min_effective}

    def evaluate(self, query, *, as_of: str | None = None):
        if not self.model_version:
            raise ValueError("Fit a frozen training window before evaluation")
        case = query["case"]
        as_of = event_time("", as_of or case.get("confirm_at"))
        cutoff = timestamp(case.get("feature_cutoff_at"))
        if not as_of or not cutoff or cutoff > as_of or self.fitted_at > as_of:
            raise ValueError("Query precedes its features or frozen training cutoff")
        result = {"case_id": case["case_id"], "signal_id": case.get("signal_id", ""), "as_of": as_of,
                  "model_version": self.model_version, "classification": "research_event_time" if self.research else "pit_observed",
                  "calibration_status": "uncalibrated", "trading_gate_enabled": False,
                  "probability": None, "expected_net_return": None, "payoff_ratio": None,
                  "profit_factor": None, "probability_interval": None, "matches": [], "rejections": {}}
        result["forecast_id"] = stable_id("analog_forecast", case["case_id"], self.model_version, as_of)
        result["query_missing_fields"] = [name for name, value in zip(
            ("strategy_id", "entry_mode", "config_version", "feature_version", "limit_pct", "execution_precision",
             "cost_version", "exit_rule_version", "outcome_version"), family(query)) if value is None or value == ""]
        pool = [r for r in self.training if r["case"]["case_id"] != case["case_id"]
                and r["case"]["market_date"] < case["market_date"]
                and self._eligible(r, as_of) and family(r) == family(query)]
        result["rejections"]["incompatible_or_unknown"] = len(self.training) - len(pool)
        result["compatible_count"] = len(pool)
        if (any(part is None or part == "" for part in family(query))
                or (number(case.get("quality", {}).get("feature_coverage")) or 0) < self.config.min_quality
                or (not self.research and not case.get("pit_verified"))):
            return {**result, "status": "insufficient", "reason": "query_quality_or_version_unknown", "fallback_level": "none"}
        same_phase = [r for r in pool if phase(query) is not None and phase(r) == phase(query)]
        baseline_pool = same_phase if self._reliability(same_phase, self._weights(same_phase, as_of))["sufficient"] else pool
        base_weights = self._weights(baseline_pool, as_of)
        base_ok = self._reliability(baseline_pool, base_weights)["sufficient"]
        baseline = self._simple(baseline_pool, base_weights) if base_ok else {}
        ranked = []
        for record in pool:
            distance, explanation = self._distance(query, record)
            if math.isfinite(distance) and distance <= self.config.max_distance:
                ranked.append((distance, record["case"]["case_id"], record, explanation))
        ranked.sort(key=lambda item: (item[0], item[1]))
        result["rejections"]["distance_or_features"] = len(pool) - len(ranked)
        result["drift_status"] = "feature_shift" if len(pool) >= self.config.min_samples and len(ranked) / len(pool) < .1 else "not_detected"
        chosen = ranked[:self.config.neighbors]
        records = [item[2] for item in chosen]
        weights = self._weights(records, as_of, [item[0] for item in chosen])
        level = "similar_cases"
        if not self._reliability(records, weights)["sufficient"]:
            records = same_phase
            weights = self._weights(records, as_of)
            level = "strategy_mode_phase"
        if not self._reliability(records, weights)["sufficient"]:
            records, weights, level = pool, self._weights(pool, as_of), "strategy_mode"
        reliability = self._reliability(records, weights)
        result.update(reliability=reliability, fallback_level=level, baseline=baseline)
        if not reliability["sufficient"]:
            return {**result, "status": "insufficient", "reason": "too_few_independent_days_or_cases", "fallback_level": "none"}
        stats = self._statistics(records, weights, baseline, as_of, result["forecast_id"])
        result.update(stats, status="estimated", reason="", basis="closed_trade_net_return_positive")
        result["baseline_lift"] = stats["probability"] - baseline["probability"] if baseline else None
        selected_ids = {r["case"]["case_id"] for r in records}
        # Bound stored/displayed matches; all contributing IDs remain auditable.
        result["sample_case_ids"] = sorted(selected_ids)
        total = float(weights.sum())
        lookup = {r["case"]["case_id"]: float(w / total) for r, w in zip(records, weights)}
        for distance, ident, record, explanation in chosen:
            if ident in selected_ids:
                label = known_label(record, "net_return", as_of, research=self.research)
                result["matches"].append({"case_id": ident, "code": record["case"]["code"],
                    "market_date": record["case"]["market_date"], "distance": distance,
                    "weight": lookup[ident], "net_return": label["net_return"],
                    "success": float(label["net_return"]) > 0, "explanation": explanation})
        if not result["matches"]:
            for record in sorted(records, key=lambda r: r["case"]["case_id"])[:self.config.neighbors]:
                distance, explanation = self._distance(query, record)
                label = known_label(record, "net_return", as_of, research=self.research)
                result["matches"].append({"case_id": record["case"]["case_id"], "code": record["case"]["code"],
                    "market_date": record["case"]["market_date"], "distance": distance if math.isfinite(distance) else None,
                    "weight": lookup[record["case"]["case_id"]], "net_return": label["net_return"],
                    "success": float(label["net_return"]) > 0, "explanation": explanation, "group_reference_only": True})
        return clean(result)

    @staticmethod
    def _simple(records, weights):
        values = np.array([float(r["labels"]["net_return"]["net_return"]) for r in records])
        return {"probability": float(np.average(values > 0, weights=weights)),
                "expected_net_return": float(np.average(values, weights=weights))}

    def _statistics(self, records, weights, baseline, as_of, seed):
        values = np.array([float(r["labels"]["net_return"]["net_return"]) for r in records])
        total, prior = float(weights.sum()), self.config.prior_days if baseline else 0
        wins, losses = values > 0, values < 0
        p = (float(np.dot(weights, wins)) + prior * baseline.get("probability", 0)) / (total + prior)
        expected = (float(np.dot(weights, values)) + prior * baseline.get("expected_net_return", 0)) / (total + prior)
        gain = float(np.average(values[wins], weights=weights[wins])) if wins.any() else None
        loss = -float(np.average(values[losses], weights=weights[losses])) if losses.any() else None
        dates = sorted({r["case"]["market_date"] for r in records})
        blocks = [np.array([i for i, r in enumerate(records) if r["case"]["market_date"] == day]) for day in dates]
        rng = np.random.default_rng(int(seed[:8], 16))
        probabilities, means = [], []
        for _ in range(self.config.bootstrap_draws):
            indices = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
            w, v = weights[indices], values[indices]
            probabilities.append((float(np.dot(w, v > 0)) + prior * baseline.get("probability", 0)) / (float(w.sum()) + prior))
            means.append((float(np.dot(w, v)) + prior * baseline.get("expected_net_return", 0)) / (float(w.sum()) + prior))
        def quantiles(v, w):
            order = np.argsort(v, kind="stable")
            v, w = np.array(v)[order], np.array(w)[order]
            cumulative = np.cumsum(w) / sum(w)
            return {name: float(v[min(int(np.searchsorted(cumulative, q)), len(v) - 1)])
                    for name, q in (("p05", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))}
        distributions = {"net_return": quantiles(values, weights)}
        for label in ("return_d1", "return_d3", "return_d5", "mfe", "mae"):
            pairs = [(known_label(r, label, as_of, research=self.research), w) for r, w in zip(records, weights)]
            pairs = [(number(v.get("value")), w) for v, w in pairs if number(v.get("value")) is not None]
            distributions[label] = {"count": len(pairs), "quantiles": quantiles(*zip(*pairs)) if pairs else None}
        from scipy.stats import beta
        # An all-win/all-loss block bootstrap alone collapses to false certainty.
        independent = min(len(dates), self._reliability(records, weights)["effective_sample_count"])
        beta_low, beta_high = beta.ppf([.025, .975], p * independent + .5, (1 - p) * independent + .5)
        return {"probability": p, "expected_net_return": expected, "average_win": gain, "average_loss": loss,
                "payoff_ratio": gain / loss if gain is not None and loss else None,
                "profit_factor": float(np.dot(weights[wins], values[wins]) / -np.dot(weights[losses], values[losses])) if losses.any() else None,
                "median_net_return": distributions["net_return"]["p50"], "return_distributions": distributions,
                "probability_interval": {"low": min(float(np.quantile(probabilities, .025)), float(beta_low)),
                                         "high": max(float(np.quantile(probabilities, .975)), float(beta_high)),
                                         "method": "day_block_bootstrap_with_cluster_beta_envelope", "coverage": .95},
                "expected_return_interval": {"low": float(np.quantile(means, .025)), "high": float(np.quantile(means, .975))}}


def publish_case_index(repository, directory: Path, *, as_of: str, max_cases: int = 5000) -> dict:
    as_of = event_time("", as_of)
    if not as_of:
        raise ValueError("Valid publication cutoff required")
    records = repository.export_records(end=as_of[:10].replace("-", ""), limit=max_cases)
    records = [{**r, "case": {key: value for key, value in r["case"].items()
                               if key not in {"backtest_config", "resolved_exit_policy", "strategy_execution"}}}
               for r in records]
    payload = {"index_version": SIMILARITY_VERSION, "as_of": as_of,
               "published_at": datetime.now().isoformat(timespec="milliseconds"), "max_cases": max_cases,
               "records": records, "data_version": stable_id(records)}
    encoded = json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)
    while len(encoded.encode("utf-8")) > 16 * 1024 * 1024 and records:
        records = records[:max(0, len(records) - max(1, len(records) // 10))]
        payload.update(records=records, data_version=stable_id(records))
        encoded = json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "matching_index.json"
    pending = path.with_suffix(".json.tmp")
    pending.write_text(encoded, encoding="utf-8")
    pending.replace(path)
    return {key: value for key, value in payload.items() if key != "records"} | {"record_count": len(records)}


def load_case_index(path: Path) -> dict:
    from snapshot.artifact_cache import GLOBAL_ARTIFACT_CACHE
    payload = GLOBAL_ARTIFACT_CACHE.load_json(path) or {}
    if payload.get("index_version") != SIMILARITY_VERSION:
        raise ValueError("Missing or incompatible published matching index")
    return payload
