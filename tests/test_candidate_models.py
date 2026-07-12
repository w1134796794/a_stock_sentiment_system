import numpy as np
import pandas as pd
import sys
from types import SimpleNamespace

from core.models.candidate_model import CandidateModelRuntime, CandidateModelTrainer
from core.models.market_regime import MarketRegimeDetector


def _training_frame():
    rng = np.random.default_rng(23)
    rows = []
    for date in pd.bdate_range("2026-01-05", periods=55):
        for code in range(45):
            signal = rng.normal()
            target = 0.03 * signal + rng.normal(scale=0.015)
            rows.append({
                "trade_date": date.strftime("%Y%m%d"),
                "future_date": (date + pd.offsets.BDay(3)).strftime("%Y%m%d"),
                "code": f"{code:06d}",
                "factor_good": signal,
                "factor_noise": rng.normal(),
                "next_3d_excess_return": target,
                "raw_forward_return": target,
                "market_score": 50 + 10 * np.sin(len(rows) / 100),
                "label_success": int(target > 0.01),
                "stop_before_profit": int(target < -0.005),
            })
    return pd.DataFrame(rows)


def test_lightgbm_ranker_meta_label_and_runtime(tmp_path):
    frame = _training_frame()
    trainer = CandidateModelTrainer(max_rows=20_000)
    result = trainer.fit_and_save(
        frame,
        features=["factor_good", "factor_noise"],
        base_scores=frame["factor_good"].rank(pct=True).mul(100),
        directory=tmp_path,
        effective_date="20260401",
    )
    assert result["status"] in {"active", "rejected_oos_gate"}
    assert result["calibration"]["brier_score"] is not None
    assert result["shap_summary"]
    if result["active"]:
        runtime = CandidateModelRuntime(result, base_dir=tmp_path)
        scored = runtime.score(frame.tail(20))
        assert scored["available"] is True
        assert len(scored["probability"]) == 20
        assert scored["expected_gross_return"].notna().all()
        assert scored["stop_probability"].notna().all()


def test_hmm_market_regime_trains_and_predicts():
    frame = _training_frame()
    result = MarketRegimeDetector.fit(frame)
    assert result["method"] in {"gaussian_hmm_3_state", "threshold_fallback"}
    if result["method"] == "gaussian_hmm_3_state":
        assert len(result["date_states"]) == frame["trade_date"].nunique()
        assert set(result["date_states"].values()) == {"strong", "neutral", "weak"}
    regime = MarketRegimeDetector.predict_current(result, 80.0, 0.02)
    assert regime in {"strong", "neutral", "weak"}


def test_runtime_does_not_treat_model_directory_as_model_file(tmp_path):
    runtime = CandidateModelRuntime(
        {"active": True, "rank_model_file": "", "meta_model_file": ""},
        base_dir=tmp_path,
    )
    assert runtime.available() is False


def test_runtime_supports_legacy_artifact_without_return_model(tmp_path, monkeypatch):
    rank_file = tmp_path / "rank.txt"
    meta_file = tmp_path / "meta.txt"
    rank_file.touch()
    meta_file.touch()
    opened = []

    class FakeBooster:
        def __init__(self, *, model_file):
            opened.append(model_file)
            self.model_file = model_file

        def predict(self, matrix, raw_score=False, pred_contrib=False):
            size = len(matrix)
            if pred_contrib:
                return np.column_stack([np.full(size, 0.2), np.zeros(size)])
            if raw_score:
                return np.linspace(-0.2, 0.2, size)
            return np.linspace(0.1, 0.9, size)

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(Booster=FakeBooster))
    runtime = CandidateModelRuntime(
        {
            "active": True,
            "features": ["factor_good"],
            "medians": {"factor_good": 0.0},
            "rank_model_file": rank_file.name,
            "meta_model_file": meta_file.name,
            "calibration_method": "platt_out_of_sample",
            "platt_coef": 1.0,
            "platt_intercept": 0.0,
        },
        base_dir=tmp_path,
    )

    scored = runtime.score(pd.DataFrame({"factor_good": [0.1, 0.2, 0.3]}))

    assert scored["available"] is True
    assert np.isnan(scored["expected_return"]).all()
    assert np.isnan(scored["expected_gross_return"]).all()
    assert np.isnan(scored["stop_probability"]).all()
    assert opened == [str(rank_file), str(meta_file)]
