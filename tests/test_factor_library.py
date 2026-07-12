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


def test_ic_ir_training_keeps_stable_inverse_factor_direction(tmp_path):
    rng = np.random.default_rng(11)
    rows = []
    for date in pd.bdate_range("2026-01-05", periods=40):
        inverse = rng.normal(size=40)
        for value in inverse:
            rows.append({
                "trade_date": date.strftime("%Y%m%d"),
                "factor_inverse": value,
                "target_return": -0.02 * value + rng.normal(scale=0.003),
            })
    trainer = FactorLibraryTrainer(
        repository=DynamicWeightRepository(tmp_path), min_daily_samples=20,
    )
    metrics = trainer.factor_metrics(pd.DataFrame(rows), ["factor_inverse"])
    weights = trainer._learned_weights(metrics)
    assert metrics["factor_inverse"]["ic_mean"] < 0
    assert weights["factor_inverse"] < 0


def test_confidence_profile_contains_calibration_intervals_and_drift_reference(tmp_path):
    rng = np.random.default_rng(17)
    rows = []
    for date in pd.bdate_range("2026-01-05", periods=40):
        for _ in range(40):
            factor = rng.normal()
            target = 0.01 * factor + rng.normal(scale=0.02)
            rows.append({
                "trade_date": date.strftime("%Y%m%d"),
                "factor_a": factor,
                "target_return": target,
                "next_3d_excess_return": target,
                "label_success": int(target > 0),
                "stop_before_profit": int(target < -0.02),
                "mfe_3d": max(target, 0),
                "mae_3d": min(target, 0),
            })
    trainer = FactorLibraryTrainer(repository=DynamicWeightRepository(tmp_path), min_daily_samples=20)
    _, report = trainer.fit_frame(pd.DataFrame(rows), {"factor_a": 1.0})
    profile = report["confidence_profiles"]["all"]
    assert profile["calibration"]["brier_score"] is not None
    assert profile["conformal"]["radius"] > 0
    assert profile["bins"][0]["probability_ci_low"] <= profile["bins"][0]["success_probability"]
    assert report["feature_reference"]["factor_a"]["sample_size"] == len(rows)


def test_feature_drift_references_are_split_by_market_regime(tmp_path):
    rng = np.random.default_rng(29)
    rows = []
    regimes = (("strong", 80.0), ("neutral", 50.0), ("weak", 20.0))
    for regime_index, (regime, center) in enumerate(regimes):
        for date in pd.bdate_range("2026-01-05", periods=6) + pd.offsets.BDay(regime_index * 8):
            for _ in range(40):
                factor = center + rng.normal(scale=2.0)
                target = factor / 10_000 + rng.normal(scale=0.01)
                rows.append({
                    "trade_date": date.strftime("%Y%m%d"),
                    "market_regime": regime,
                    "factor_a": factor,
                    "target_return": target,
                    "next_3d_excess_return": target,
                    "label_success": int(target > 0),
                    "stop_before_profit": int(target < -0.02),
                    "mfe_3d": max(target, 0),
                    "mae_3d": min(target, 0),
                })

    trainer = FactorLibraryTrainer(
        repository=DynamicWeightRepository(tmp_path), min_daily_samples=20,
    )
    _, report = trainer.fit_frame(pd.DataFrame(rows), {"factor_a": 1.0})

    references = report["feature_reference_by_regime"]
    assert set(references) == {"strong", "neutral", "weak"}
    assert references["strong"]["factor_a"]["mean"] > references["neutral"]["factor_a"]["mean"]
    assert references["neutral"]["factor_a"]["mean"] > references["weak"]["factor_a"]["mean"]
    assert report["feature_reference_regime_meta"]["strong"] == {
        "sample_size": 240,
        "trade_days": 6,
    }


def test_outcome_persistence_migrates_legacy_table_columns(tmp_path):
    import duckdb

    db_path = tmp_path / "factors.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE signal_outcome_wide (
            trade_date VARCHAR,
            code VARCHAR,
            entry_date VARCHAR,
            future_date VARCHAR,
            market_regime VARCHAR,
            primary_sector VARCHAR,
            next_3d_excess_return DOUBLE,
            mfe_3d DOUBLE,
            mae_3d DOUBLE,
            stop_before_profit INTEGER,
            tradable_next_day INTEGER,
            label_success INTEGER,
            label_version VARCHAR,
            computed_at VARCHAR
        )
        """
    )
    con.close()
    frame = pd.DataFrame([{
        "trade_date": "20260701", "code": "000001", "entry_date": "20260702",
        "future_date": "20260706", "market_regime": "neutral", "primary_sector": "银行",
        "next_3d_excess_return": 0.01, "mfe_3d": 0.08, "mae_3d": -0.02,
        "stop_before_profit": 0, "tradable_next_day": 1, "label_class": 2,
        "label_name": "strong_buy", "label_strong_buy": 1, "label_hold": 0,
        "label_avoid": 0, "label_success": 1,
    }])
    trainer = FactorLibraryTrainer(
        duckdb_path=db_path, repository=DynamicWeightRepository(tmp_path / "weights")
    )

    assert trainer.persist_outcome_labels(frame) == 1
    con = duckdb.connect(str(db_path), read_only=True)
    row = con.execute(
        "SELECT label_class, label_name, label_strong_buy, label_hold, label_avoid "
        "FROM signal_outcome_wide"
    ).fetchone()
    con.close()
    assert row == (2, "strong_buy", 1, 0, 0)


def test_near_limit_training_scope_keeps_only_comparable_strong_stocks(tmp_path):
    trainer = FactorLibraryTrainer(repository=DynamicWeightRepository(tmp_path))
    frame = pd.DataFrame([
        {"code": "000001", "limit_progress": 0.96, "liquidity_score": 35.0},
        {"code": "000002", "limit_progress": 0.94, "liquidity_score": 90.0},
        {"code": "000003", "limit_progress": 0.99, "liquidity_score": 34.9},
        {"code": "000004", "limit_progress": 0.99, "liquidity_score": 80.0},
    ])

    scoped = trainer._apply_training_scope(frame, "near_limit")

    assert scoped["code"].tolist() == ["000001", "000004"]
