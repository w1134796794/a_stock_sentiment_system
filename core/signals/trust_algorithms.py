"""Auditable statistical trust utilities for point-in-time signals.

The functions in this module are deliberately model-agnostic.  They operate on
arrays and dates so the same definitions are shared by training, screening,
backtests and agent evidence APIs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd


def _finite(values: Iterable[Any]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def beta_binomial_interval(
    successes: Any,
    trials: Any,
    *,
    credibility: float = 0.80,
    prior_alpha: float = 0.5,
    prior_beta: float = 0.5,
) -> Dict[str, float]:
    """Jeffreys-prior posterior mean and equal-tail credible interval."""
    n = max(int(float(trials or 0)), 0)
    wins = min(max(float(successes or 0.0), 0.0), float(n))
    alpha = max(float(prior_alpha), 1e-9) + wins
    beta = max(float(prior_beta), 1e-9) + n - wins
    level = min(max(float(credibility), 0.01), 0.999)
    tail = (1.0 - level) / 2.0
    mean = alpha / (alpha + beta)
    try:
        from scipy.stats import beta as beta_distribution  # type: ignore

        lower = float(beta_distribution.ppf(tail, alpha, beta))
        upper = float(beta_distribution.ppf(1.0 - tail, alpha, beta))
        method = "beta_exact"
    except Exception:
        variance = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
        z = 1.2815515655446004 if abs(level - 0.80) < 1e-6 else 1.959963984540054
        lower = max(0.0, mean - z * math.sqrt(variance))
        upper = min(1.0, mean + z * math.sqrt(variance))
        method = "beta_normal_fallback"
    return {
        "posterior_mean": float(mean),
        "lower": float(lower),
        "upper": float(upper),
        "credibility": level,
        "trials": n,
        "successes": int(round(wins)),
        "method": method,
    }


def calibration_metrics(
    actual: Sequence[Any], predicted: Sequence[Any], *, bins: int = 10,
) -> Dict[str, Any]:
    """Return Brier, log loss, ECE/MCE and a reliability curve."""
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    valid = np.isfinite(y) & np.isfinite(p)
    y = y[valid]
    p = np.clip(p[valid], 1e-6, 1.0 - 1e-6)
    if not len(y):
        return {"sample_size": 0, "brier_score": None, "ece": None, "mce": None, "log_loss": None, "curve": []}
    count = max(min(int(bins), len(y)), 1)
    edges = np.linspace(0.0, 1.0, count + 1)
    bucket = np.minimum(np.digitize(p, edges[1:-1], right=False), count - 1)
    curve: List[Dict[str, Any]] = []
    ece = 0.0
    gaps: List[float] = []
    for index in range(count):
        mask = bucket == index
        if not mask.any():
            continue
        predicted_mean = float(p[mask].mean())
        observed_rate = float(y[mask].mean())
        gap = abs(predicted_mean - observed_rate)
        gaps.append(gap)
        ece += float(mask.mean()) * gap
        curve.append({
            "bin": index + 1,
            "sample_size": int(mask.sum()),
            "predicted": predicted_mean,
            "observed": observed_rate,
            "gap": gap,
        })
    return {
        "sample_size": int(len(y)),
        "brier_score": float(np.mean((p - y) ** 2)),
        "ece": float(ece),
        "mce": float(max(gaps) if gaps else 0.0),
        "log_loss": float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))),
        "curve": curve,
    }


def conformal_residual_interval(
    actual: Sequence[Any], predicted: Sequence[Any], *, alpha: float = 0.20,
) -> Dict[str, float]:
    """Split-conformal symmetric residual interval with finite-sample quantile."""
    y = np.asarray(actual, dtype=float)
    p = np.asarray(predicted, dtype=float)
    residual = np.abs(y - p)
    residual = residual[np.isfinite(residual)]
    if not len(residual):
        return {"alpha": float(alpha), "radius": 0.0, "sample_size": 0, "empirical_coverage": 0.0}
    alpha = min(max(float(alpha), 0.01), 0.99)
    rank = min(int(math.ceil((len(residual) + 1) * (1.0 - alpha))), len(residual))
    radius = float(np.partition(residual, rank - 1)[rank - 1])
    return {
        "alpha": alpha,
        "radius": radius,
        "sample_size": int(len(residual)),
        "empirical_coverage": float((residual <= radius).mean()),
    }


def block_bootstrap_mean(
    values: Sequence[Any], block_ids: Sequence[Any], *, simulations: int = 500, seed: int = 42,
) -> Dict[str, Any]:
    """Bootstrap whole trading-day blocks to preserve cross-sectional dependence."""
    frame = pd.DataFrame({"value": pd.to_numeric(pd.Series(values), errors="coerce"), "block": list(block_ids)}).dropna(subset=["value"])
    if frame.empty:
        return {"sample_size": 0, "blocks": 0, "mean": 0.0, "p10": 0.0, "p90": 0.0}
    grouped = [group["value"].to_numpy(dtype=float) for _, group in frame.groupby("block", sort=True)]
    rng = np.random.default_rng(int(seed))
    results = np.empty(max(int(simulations), 50), dtype=float)
    for index in range(len(results)):
        selected = rng.integers(0, len(grouped), size=len(grouped))
        results[index] = float(np.concatenate([grouped[item] for item in selected]).mean())
    return {
        "sample_size": int(len(frame)),
        "blocks": int(len(grouped)),
        "simulations": int(len(results)),
        "mean": float(frame["value"].mean()),
        "p10": float(np.percentile(results, 10)),
        "median": float(np.percentile(results, 50)),
        "p90": float(np.percentile(results, 90)),
    }


def population_stability_index(reference: Sequence[Any], current: Sequence[Any], *, bins: int = 10) -> float:
    ref = _finite(reference)
    cur = _finite(current)
    if len(ref) < 20 or len(cur) < 5:
        return 0.0
    edges = np.unique(np.quantile(ref, np.linspace(0.0, 1.0, max(int(bins), 2) + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    ref_hist = np.histogram(ref, bins=edges)[0] / len(ref)
    cur_hist = np.histogram(cur, bins=edges)[0] / len(cur)
    ref_hist = np.clip(ref_hist, 1e-6, None)
    cur_hist = np.clip(cur_hist, 1e-6, None)
    return float(np.sum((cur_hist - ref_hist) * np.log(cur_hist / ref_hist)))


def ks_distance(reference: Sequence[Any], current: Sequence[Any]) -> float:
    ref = np.sort(_finite(reference))
    cur = np.sort(_finite(current))
    if not len(ref) or not len(cur):
        return 0.0
    points = np.unique(np.concatenate([ref, cur]))
    ref_cdf = np.searchsorted(ref, points, side="right") / len(ref)
    cur_cdf = np.searchsorted(cur, points, side="right") / len(cur)
    return float(np.max(np.abs(ref_cdf - cur_cdf)))


def distribution_reference(values: Sequence[Any], *, bins: int = 10) -> Dict[str, Any]:
    array = _finite(values)
    if not len(array):
        return {"sample_size": 0, "values": []}
    # Preserve discrete and multimodal factor distributions. Eleven deciles are
    # too coarse for PSI and can report false drift when many values are tied.
    sample_points = min(max(len(array), max(int(bins), 20) + 1), 201)
    quantiles = np.quantile(array, np.linspace(0.0, 1.0, sample_points))
    return {
        "sample_size": int(len(array)),
        "mean": float(array.mean()),
        "std": float(array.std()),
        "values": [float(value) for value in quantiles],
    }


def reference_sample(reference: Mapping[str, Any], *, repeats: int = 20) -> np.ndarray:
    values = np.asarray(reference.get("values") or [], dtype=float)
    if not len(values):
        return np.asarray([], dtype=float)
    return np.repeat(values, max(int(repeats), 1))


def evaluate_feature_drift(
    frame: pd.DataFrame, references: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for factor, reference in references.items():
        if factor not in frame.columns:
            continue
        current = pd.to_numeric(frame[factor], errors="coerce").dropna().to_numpy(dtype=float)
        historical = reference_sample(reference)
        if len(current) < 5 or len(historical) < 20:
            continue
        rows.append({
            "factor": factor,
            "psi": population_stability_index(historical, current),
            "ks": ks_distance(historical, current),
            "current_sample_size": int(len(current)),
        })
    max_psi = max((row["psi"] for row in rows), default=0.0)
    max_ks = max((row["ks"] for row in rows), default=0.0)
    if not rows:
        status = "unknown"
    elif max_psi >= 0.25 or max_ks >= 0.35:
        status = "degraded"
    elif max_psi >= 0.10 or max_ks >= 0.20:
        status = "watch"
    else:
        status = "stable"
    return {
        "status": status,
        "max_psi": float(max_psi),
        "max_ks": float(max_ks),
        "features": sorted(rows, key=lambda row: max(row["psi"] / 0.25, row["ks"] / 0.35), reverse=True),
    }


def purged_month_split(
    frame: pd.DataFrame,
    *,
    validation_start: str,
    validation_end: str,
    embargo_days: int = 3,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """Point-in-time split purging labels that overlap the validation window."""
    dates = frame["trade_date"].astype(str)
    validation = frame[(dates >= str(validation_start)) & (dates <= str(validation_end))].copy()
    train = frame[dates < str(validation_start)].copy()
    before = len(train)
    if "future_date" in train.columns:
        train = train[train["future_date"].astype(str) < str(validation_start)].copy()
    unique_validation_dates = sorted(validation["trade_date"].astype(str).unique())
    embargo = set(unique_validation_dates[:max(int(embargo_days), 0)])
    if embargo:
        validation = validation[~validation["trade_date"].astype(str).isin(embargo)].copy()
    return train, validation, {
        "train_rows_before_purge": int(before),
        "train_rows_after_purge": int(len(train)),
        "purged_rows": int(before - len(train)),
        "embargo_dates": sorted(embargo),
        "validation_rows": int(len(validation)),
    }


@dataclass
class ADWINDetector:
    """Small dependency-free adaptive-window drift detector for daily errors."""

    delta: float = 0.002
    min_window: int = 20
    max_window: int = 240
    values: List[float] = field(default_factory=list)

    def update(self, value: Any) -> bool:
        number = float(value)
        if not math.isfinite(number):
            return False
        self.values.append(number)
        if len(self.values) > self.max_window:
            self.values = self.values[-self.max_window:]
        if len(self.values) < self.min_window * 2:
            return False
        array = np.asarray(self.values, dtype=float)
        for cut in range(self.min_window, len(array) - self.min_window + 1):
            left, right = array[:cut], array[cut:]
            harmonic = 1.0 / len(left) + 1.0 / len(right)
            epsilon = math.sqrt(0.5 * harmonic * math.log(4.0 / max(self.delta, 1e-9)))
            if abs(float(left.mean() - right.mean())) > epsilon:
                self.values = list(right)
                return True
        return False


__all__ = [
    "ADWINDetector",
    "beta_binomial_interval",
    "block_bootstrap_mean",
    "calibration_metrics",
    "conformal_residual_interval",
    "distribution_reference",
    "evaluate_feature_drift",
    "ks_distance",
    "population_stability_index",
    "purged_month_split",
    "reference_sample",
]
