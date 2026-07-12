import numpy as np
import pandas as pd

from core.signals.trust_algorithms import (
    ADWINDetector,
    beta_binomial_interval,
    block_bootstrap_mean,
    calibration_metrics,
    conformal_residual_interval,
    ks_distance,
    population_stability_index,
    purged_month_split,
    distribution_reference,
    evaluate_feature_drift,
)


def test_beta_binomial_shrinks_small_samples_and_returns_interval():
    small = beta_binomial_interval(8, 10)
    large = beta_binomial_interval(800, 1000)
    assert 0 < small["lower"] < small["posterior_mean"] < small["upper"] < 1
    assert large["upper"] - large["lower"] < small["upper"] - small["lower"]


def test_calibration_metrics_reward_calibrated_probabilities():
    actual = [0, 0, 1, 1]
    good = calibration_metrics(actual, [0.1, 0.2, 0.8, 0.9], bins=2)
    bad = calibration_metrics(actual, [0.9, 0.8, 0.2, 0.1], bins=2)
    assert good["brier_score"] < bad["brier_score"]
    assert good["ece"] < bad["ece"]


def test_conformal_and_block_bootstrap_are_finite():
    interval = conformal_residual_interval([0.01, 0.02, -0.01], [0.0, 0.01, 0.0])
    bootstrap = block_bootstrap_mean([1, 2, 3, 4], ["a", "a", "b", "b"], simulations=100)
    assert interval["radius"] > 0
    assert bootstrap["p10"] <= bootstrap["median"] <= bootstrap["p90"]


def test_distribution_drift_metrics_detect_shift():
    reference = np.linspace(0, 1, 200)
    stable = reference + 0.001
    shifted = reference + 3
    assert population_stability_index(reference, shifted) > population_stability_index(reference, stable)
    assert ks_distance(reference, shifted) > 0.9


def test_purged_split_removes_overlapping_labels_and_embargoes_first_days():
    frame = pd.DataFrame({
        "trade_date": ["20260128", "20260129", "20260130", "20260202", "20260203", "20260204"],
        "future_date": ["20260202", "20260203", "20260204", "20260205", "20260206", "20260209"],
    })
    train, validation, audit = purged_month_split(
        frame, validation_start="20260201", validation_end="20260228", embargo_days=1,
    )
    assert train.empty
    assert validation["trade_date"].min() == "20260203"
    assert audit["purged_rows"] == 3


def test_adwin_detects_large_mean_change():
    detector = ADWINDetector(delta=0.01, min_window=10)
    triggered = False
    for value in [0.0] * 30 + [1.0] * 30:
        triggered = detector.update(value) or triggered
    assert triggered


def test_feature_drift_returns_auditable_status():
    reference = {"factor": distribution_reference(np.linspace(0, 1, 200))}
    result = evaluate_feature_drift(pd.DataFrame({"factor": np.linspace(3, 4, 20)}), reference)
    assert result["status"] == "degraded"
    assert result["features"][0]["factor"] == "factor"
