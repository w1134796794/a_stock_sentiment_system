"""Calibrated confidence shared by screening, leader and intraday signals."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _ratio(value: Any, default: float = 1.0) -> float:
    number = _float(value, default)
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def market_regime(score: Any) -> str:
    """Use the same market layers as the daily dashboard and execution rules."""
    value = _float(score, 50.0)
    if value >= 70.0:
        return "strong"
    if value >= 45.0:
        return "neutral"
    return "weak"


class ConfidenceService:
    """Turn calibrated model evidence into an auditable confidence payload.

    ``calibrated_probability`` remains the model probability shown to users.
    ``confidence_score`` additionally discounts incomplete data, regime mismatch,
    insufficient samples and weak tradability. It is deliberately not another
    weighted stock score.
    """

    MIN_RELIABLE_SAMPLES = 40
    FULL_RELIABILITY_SAMPLES = 150

    @classmethod
    def assess(
        cls,
        *,
        calibrated_probability: Any,
        baseline_probability: Any = None,
        expected_return: Any = 0.0,
        stop_probability: Any = 0.5,
        sample_size: Any = 0,
        average_mfe: Any = 0.0,
        average_mae: Any = 0.0,
        data_completeness: Any = 1.0,
        regime_match: Any = 1.0,
        tradability: Any = 1.0,
        model_type: str = "",
        as_of_date: str = "",
    ) -> Dict[str, Any]:
        probability = _ratio(calibrated_probability, 0.5)
        baseline = (
            _ratio(baseline_probability, 0.5)
            if baseline_probability is not None
            else 0.50
        )
        probability_lift = probability / max(baseline, 0.01)
        expected = _float(expected_return)
        samples = max(int(_float(sample_size, 0.0)), 0)
        data_ratio = _ratio(data_completeness)
        regime_ratio = _ratio(regime_match)
        tradability_ratio = _ratio(tradability)
        sample_reliability = min(
            1.0,
            math.sqrt(samples / cls.FULL_RELIABILITY_SAMPLES),
        ) if samples else 0.0
        quality = data_ratio * regime_ratio * sample_reliability * tradability_ratio
        # Reliability discounts only the model's edge over its own historical
        # baseline. Missing evidence should pull a forecast back to baseline,
        # not incorrectly turn a 25% strict-event probability into near zero.
        confidence = (
            baseline + (probability - baseline) * quality
            if samples
            else 0.0
        )
        grade = cls.grade(
            probability=probability,
            baseline_probability=baseline,
            expected_return=expected,
            quality=quality,
            samples=samples,
            data_completeness=data_ratio,
        )
        return {
            "candidate_probability": round(probability * 100.0, 2),
            "baseline_probability": round(baseline * 100.0, 2),
            "probability_lift": round(probability_lift, 2),
            "expected_return_pct": round(expected * 100.0, 2),
            "stop_probability": round(_ratio(stop_probability, 0.5) * 100.0, 2),
            "sample_size": samples,
            "average_mfe_pct": round(_float(average_mfe) * 100.0, 2),
            "average_mae_pct": round(_float(average_mae) * 100.0, 2),
            "data_completeness": round(data_ratio * 100.0, 1),
            "regime_match": round(regime_ratio * 100.0, 1),
            "tradability": round(tradability_ratio * 100.0, 1),
            "sample_reliability": round(sample_reliability * 100.0, 1),
            "confidence_score": round(confidence * 100.0, 2),
            "confidence_grade": grade,
            "model_type": str(model_type or ""),
            "as_of_date": str(as_of_date or ""),
        }

    @staticmethod
    def grade(
        *,
        probability: float,
        baseline_probability: float,
        expected_return: float,
        quality: float,
        samples: int,
        data_completeness: float,
    ) -> str:
        if data_completeness < 0.60 or samples < 20:
            return "D"
        lift = probability / max(baseline_probability, 0.01)
        if (
            (probability >= 0.60 or lift >= 1.50)
            and expected_return >= 0.005
            and quality >= 0.75
            and samples >= 120
        ):
            return "A"
        if (
            (probability >= 0.45 or lift >= 1.20)
            and expected_return >= 0.003
            and quality >= 0.65
            and samples >= 40
        ):
            return "B"
        if (
            (probability >= 0.25 or lift >= 1.00)
            and expected_return >= 0.0
            and quality >= 0.50
        ):
            return "C"
        return "D"

    @classmethod
    def from_profile(
        cls,
        profile: Optional[Mapping[str, Any]],
        *,
        score: Any,
        data_completeness: Any = 1.0,
        regime_match: Any = 1.0,
        tradability: Any = 1.0,
        model_type: str = "",
        as_of_date: str = "",
    ) -> Dict[str, Any]:
        """Resolve the nearest historical score bin without using future rows."""
        payload = dict(profile or {})
        bins = [dict(row) for row in payload.get("bins") or []]
        value = _float(score, 50.0)
        def center(row: Mapping[str, Any]) -> float:
            return _float(
                row.get("score_center"),
                (_float(row.get("score_min")) + _float(row.get("score_max"))) / 2.0,
            )

        ordered = sorted(bins, key=center)
        selected = min(ordered, key=lambda row: abs(value - center(row))) if ordered else {}

        def interpolate(field: str, default: float, *, lower: float = -math.inf, upper: float = math.inf) -> float:
            points = [(center(row), _float(row.get(field), default)) for row in ordered]
            if not points:
                return default
            if len(points) == 1:
                return max(lower, min(upper, points[0][1]))
            if value <= points[0][0]:
                left, right = points[0], points[1]
            elif value >= points[-1][0]:
                left, right = points[-2], points[-1]
            else:
                left, right = points[0], points[-1]
                for index in range(1, len(points)):
                    if value <= points[index][0]:
                        left, right = points[index - 1], points[index]
                        break
            width = max(right[0] - left[0], 1e-9)
            result = left[1] + (value - left[0]) / width * (right[1] - left[1])
            return max(lower, min(upper, result))

        fallback_probability = 0.50 + max(-0.20, min(0.20, (value - 50.0) / 250.0))
        return cls.assess(
            calibrated_probability=interpolate(
                "success_probability", _float(payload.get("success_probability"), fallback_probability), lower=0.01, upper=0.99,
            ),
            baseline_probability=_float(payload.get("success_probability"), 0.50),
            expected_return=interpolate(
                "expected_return", _float(payload.get("expected_return"), 0.0), lower=-0.50, upper=0.50,
            ),
            stop_probability=interpolate(
                "stop_probability", _float(payload.get("stop_probability"), 0.5), lower=0.0, upper=1.0,
            ),
            sample_size=selected.get("sample_size", payload.get("sample_size", 0)),
            average_mfe=interpolate("average_mfe", _float(payload.get("average_mfe"), 0.0), lower=0.0, upper=1.0),
            average_mae=interpolate("average_mae", _float(payload.get("average_mae"), 0.0), lower=-1.0, upper=0.0),
            data_completeness=data_completeness,
            regime_match=regime_match,
            tradability=tradability,
            model_type=model_type,
            as_of_date=as_of_date,
        )


class HistoricalSignalStatsRepository:
    """Read de-duplicated closed trades as mode-specific out-of-sample evidence."""

    def __init__(self, results_dir: Optional[Path] = None) -> None:
        if results_dir is None:
            from config.settings import OUTPUT_DIR

            results_dir = Path(OUTPUT_DIR) / "backtest_results"
        self.results_dir = Path(results_dir)
        self._cache: Optional[pd.DataFrame] = None

    def _load(self) -> pd.DataFrame:
        if self._cache is not None:
            return self._cache
        frames = []
        paths = sorted(
            self.results_dir.glob("backtest_trades_*.csv"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:20]
        for path in paths:
            try:
                frame = pd.read_csv(path, dtype={"stock_code": str, "date": str, "entry_date": str})
            except Exception:
                continue
            if frame.empty or "action" not in frame.columns:
                continue
            frame = frame[frame["action"].astype(str).str.upper().str.startswith("SELL")].copy()
            if frame.empty:
                continue
            frame["_run"] = path.stem.removeprefix("backtest_trades_")
            frames.append(frame)
        if not frames:
            self._cache = pd.DataFrame()
            return self._cache
        data = pd.concat(frames, ignore_index=True)
        data["stock_code"] = data["stock_code"].astype(str).str.split(".").str[0].str.zfill(6)
        data["entry_date"] = data.get("entry_date", data.get("date", "")).astype(str).str[:8]
        data["entry_signal"] = data.get("entry_signal", "").fillna("").astype(str)
        data = data.sort_values("_run", ascending=False).drop_duplicates(
            ["entry_date", "stock_code", "entry_signal"], keep="first",
        )
        self._cache = data
        return data

    def get(self, signal: str, *, as_of_date: str = "") -> Dict[str, Any]:
        data = self._load()
        if data.empty:
            return {}
        aliases = {
            "弱转强": {"弱转强", "盘中转强"},
            "强势延续": {"强势延续"},
            "高开加速": {"高开加速"},
        }
        selected = data[data["entry_signal"].isin(aliases.get(signal, {signal}))].copy()
        if as_of_date:
            selected = selected[selected["entry_date"] < str(as_of_date)]
        if selected.empty:
            return {}
        pnl = pd.to_numeric(selected.get("pnl_pct"), errors="coerce")
        stop = selected.get("stop_loss_triggered", pd.Series(False, index=selected.index)).astype(str).str.lower().isin({"true", "1", "yes"})
        mfe = pd.to_numeric(selected.get("mfe_pct", pd.Series(0.0, index=selected.index)), errors="coerce")
        mae = pd.to_numeric(selected.get("mae_pct", pd.Series(0.0, index=selected.index)), errors="coerce")
        return {
            "sample_size": int(len(selected)),
            "success_probability": float((pnl > 0).mean()),
            "expected_return": float(pnl.mean()),
            "stop_probability": float(stop.mean()),
            "average_mfe": float(mfe.mean()),
            "average_mae": float(mae.mean()),
        }


__all__ = ["ConfidenceService", "HistoricalSignalStatsRepository", "market_regime"]
