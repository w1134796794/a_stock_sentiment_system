"""Three-state Gaussian HMM market regime with threshold fallback."""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping

import numpy as np
import pandas as pd


class MarketRegimeDetector:
    LABELS = ("weak", "neutral", "strong")

    @classmethod
    def fit(cls, frame: pd.DataFrame) -> Dict[str, Any]:
        daily = frame.groupby("trade_date", sort=True).agg(
            market_score=("market_score", "median"),
            market_return=("raw_forward_return", "mean"),
        ).dropna()
        if len(daily) < 40:
            return {"status": "insufficient_history", "method": "threshold_fallback"}
        values = daily[["market_score", "market_return"]].to_numpy(dtype=float)
        mean = values.mean(axis=0)
        std = values.std(axis=0)
        std[std < 1e-8] = 1.0
        normalized = (values - mean) / std
        try:
            from hmmlearn.hmm import GaussianHMM  # type: ignore

            model = GaussianHMM(
                n_components=3,
                covariance_type="diag",
                n_iter=200,
                random_state=42,
                min_covar=1e-4,
            )
            model.fit(normalized)
            states = model.predict(normalized)
            state_strength = {
                state: float(values[states == state, 0].mean() + values[states == state, 1].mean() * 100.0)
                for state in range(3)
            }
            ordered_states = sorted(state_strength, key=state_strength.get)
            labels = {str(state): cls.LABELS[index] for index, state in enumerate(ordered_states)}
            return {
                "status": "trained",
                "method": "gaussian_hmm_3_state",
                "sample_days": int(len(daily)),
                "feature_mean": [float(value) for value in mean],
                "feature_std": [float(value) for value in std],
                "means": model.means_.tolist(),
                "covars": model.covars_.tolist(),
                "transmat": model.transmat_.tolist(),
                "state_labels": labels,
                "last_state": int(states[-1]),
                "state_counts": {labels[str(state)]: int((states == state).sum()) for state in range(3)},
                "date_states": {
                    str(date): labels[str(int(state))]
                    for date, state in zip(daily.index.astype(str), states)
                },
            }
        except Exception as exc:
            return {"status": "fit_failed", "method": "threshold_fallback", "reason": str(exc)}

    @classmethod
    def predict_current(cls, metadata: Mapping[str, Any], market_score: float, market_return: float = 0.0) -> str:
        if metadata.get("status") != "trained":
            from core.models.market_state import classify_market_score

            return classify_market_score(market_score)
        mean = np.asarray(metadata.get("feature_mean"), dtype=float)
        std = np.asarray(metadata.get("feature_std"), dtype=float)
        value = (np.asarray([market_score, market_return], dtype=float) - mean) / np.where(std > 0, std, 1.0)
        means = np.asarray(metadata.get("means"), dtype=float)
        covars = np.asarray(metadata.get("covars"), dtype=float)
        if covars.ndim == 3:
            covars = np.asarray([np.diag(item) for item in covars], dtype=float)
        transition = np.asarray(metadata.get("transmat"), dtype=float)
        previous = int(metadata.get("last_state") or 0)
        prior = transition[previous] if transition.ndim == 2 else np.ones(len(means)) / len(means)
        likelihood = []
        for state in range(len(means)):
            variance = np.clip(covars[state], 1e-6, None)
            log_probability = -0.5 * float(np.sum(np.log(2 * math.pi * variance) + (value - means[state]) ** 2 / variance))
            likelihood.append(math.log(max(float(prior[state]), 1e-12)) + log_probability)
        state = int(np.argmax(likelihood))
        return str((metadata.get("state_labels") or {}).get(str(state)) or "neutral")


__all__ = ["MarketRegimeDetector"]
