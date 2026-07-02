import json

import numpy as np
import pandas as pd

from core.factors.factor_library import DynamicWeightRepository, FactorLibraryTrainer


def test_repository_never_loads_future_weight_version(tmp_path):
    repo = DynamicWeightRepository(tmp_path)
    repo.publish({
        "profile": "default",
        "effective_date": "20260201",
        "weights": {"factor_a": 1.0},
    })
    repo.publish({
        "profile": "default",
        "effective_date": "20260301",
        "weights": {"factor_b": 1.0},
    })

    assert repo.resolve("20260131", "default") is None
    assert repo.resolve("20260215", "default").weights == {"factor_a": 1.0}
    assert repo.resolve("20260301", "default").weights == {"factor_b": 1.0}


def test_ic_ir_training_rewards_predictive_factor(tmp_path):
    rng = np.random.default_rng(7)
    rows = []
    dates = pd.bdate_range("2026-01-05", periods=65)
    for date in dates:
        signal = rng.normal(size=60)
        noise = rng.normal(size=60)
        target = 0.03 * signal + rng.normal(scale=0.01, size=60)
        for idx in range(60):
            rows.append({
                "trade_date": date.strftime("%Y%m%d"),
                "factor_good": signal[idx],
                "factor_noise": noise[idx],
                "target_return": target[idx],
            })
    frame = pd.DataFrame(rows)
    trainer = FactorLibraryTrainer(
        repository=DynamicWeightRepository(tmp_path), min_daily_samples=20
    )

    weights, report = trainer.fit_frame(
        frame, {"factor_good": 0.5, "factor_noise": 0.5}
    )

    assert report["factor_metrics"]["factor_good"]["ic_mean"] > 0.8
    learned = trainer._learned_weights(report["factor_metrics"])
    assert learned["factor_good"] > learned["factor_noise"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_published_artifact_contains_auditable_metrics(tmp_path):
    repo = DynamicWeightRepository(tmp_path)
    path = repo.publish({
        "schema_version": 1,
        "model_type": "ic_ir_constrained_blend",
        "profile": "default",
        "effective_date": "20260701",
        "weights": {"factor_a": 0.7, "factor_b": 0.3},
        "factor_metrics": {"factor_a": {"ic_mean": 0.03}},
    })
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model_type"] == "ic_ir_constrained_blend"
    assert payload["factor_metrics"]["factor_a"]["ic_mean"] == 0.03
