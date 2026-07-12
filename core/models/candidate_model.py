"""LightGBM ranking and meta-label models for candidate selection."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd

from core.signals.trust_algorithms import (
    calibration_metrics,
    conformal_residual_interval,
    purged_month_split,
)


def _json_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


class CandidateModelTrainer:
    """Train a ranker plus a meta-label classifier on point-in-time features."""

    def __init__(self, *, max_rows: int = 250_000, random_state: int = 42) -> None:
        self.max_rows = max(int(max_rows), 10_000)
        self.random_state = int(random_state)

    @staticmethod
    def available() -> bool:
        try:
            import lightgbm  # noqa: F401
            from sklearn.linear_model import LogisticRegression  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _matrix(frame: pd.DataFrame, features: Sequence[str], medians: Mapping[str, float] | None = None) -> tuple[pd.DataFrame, Dict[str, float]]:
        matrix = pd.DataFrame(index=frame.index)
        learned_medians: Dict[str, float] = {}
        for feature in features:
            values = pd.to_numeric(frame.get(feature), errors="coerce")
            fallback = _json_number((medians or {}).get(feature), 50.0)
            median = float(values.median()) if values.notna().any() else fallback
            learned_medians[feature] = median
            matrix[feature] = values.fillna(median)
        return matrix, learned_medians

    def fit_and_save(
        self,
        frame: pd.DataFrame,
        *,
        features: Sequence[str],
        base_scores: pd.Series,
        directory: Path,
        effective_date: str,
    ) -> Dict[str, Any]:
        if not self.available():
            return {"status": "dependency_missing", "active": False}
        import lightgbm as lgb  # type: ignore
        from sklearn.linear_model import LogisticRegression  # type: ignore

        data = frame.copy()
        data["_base_score"] = pd.to_numeric(base_scores, errors="coerce").fillna(50.0)
        if "label_class" not in data.columns:
            success = pd.to_numeric(data.get("label_success"), errors="coerce").fillna(0).astype(int)
            stop = pd.to_numeric(data.get("stop_before_profit"), errors="coerce").fillna(0).astype(int)
            data["label_class"] = np.select([success > 0, stop > 0], [2, 0], default=1).astype(int)
        dates = sorted(data["trade_date"].astype(str).unique())
        if len(dates) < 40:
            return {"status": "insufficient_dates", "active": False, "training_days": len(dates)}
        split_index = max(int(len(dates) * 0.80), 20)
        validation_start = dates[min(split_index, len(dates) - 1)]
        train, validation, split_audit = purged_month_split(
            data,
            validation_start=validation_start,
            validation_end=dates[-1],
            embargo_days=3,
        )
        if len(train) > self.max_rows:
            train = train.sample(self.max_rows, random_state=self.random_state).sort_values("trade_date")
        if len(validation) < 200 or len(train) < 1_000:
            return {
                "status": "insufficient_rows",
                "active": False,
                "train_rows": int(len(train)),
                "validation_rows": int(len(validation)),
                "purged_split": split_audit,
            }

        feature_list = [feature for feature in features if feature in data.columns]
        x_train, medians = self._matrix(train, feature_list)
        validation_dates = sorted(validation["trade_date"].astype(str).unique())
        calibration_cut = max(int(len(validation_dates) * 0.40), 1)
        calibration_dates = set(validation_dates[:calibration_cut])
        calibration_frame = validation[validation["trade_date"].astype(str).isin(calibration_dates)].copy()
        evaluation_frame = validation[~validation["trade_date"].astype(str).isin(calibration_dates)].copy()
        if len(calibration_frame) < 100 or len(evaluation_frame) < 100:
            return {
                "status": "insufficient_calibration_rows", "active": False,
                "calibration_rows": int(len(calibration_frame)),
                "evaluation_rows": int(len(evaluation_frame)),
                "purged_split": split_audit,
            }
        x_calibration, _ = self._matrix(calibration_frame, feature_list, medians)
        x_evaluation, _ = self._matrix(evaluation_frame, feature_list, medians)

        ordered = train.assign(_row=np.arange(len(train))).sort_values(["trade_date", "_row"])
        x_rank, _ = self._matrix(ordered, feature_list, medians)
        target_rank = pd.to_numeric(ordered["next_3d_excess_return"], errors="coerce").fillna(0.0)
        relevance = (
            target_rank.groupby(ordered["trade_date"]).rank(method="average", pct=True)
            .mul(4.999).clip(0, 4).astype(int)
        )
        groups = ordered.groupby("trade_date", sort=True).size().tolist()
        ranker = lgb.LGBMRanker(
            objective="lambdarank",
            metric="ndcg",
            n_estimators=160,
            learning_rate=0.035,
            num_leaves=15,
            max_depth=5,
            min_child_samples=80,
            subsample=0.80,
            colsample_bytree=0.85,
            reg_lambda=2.0,
            random_state=self.random_state,
            verbosity=-1,
        )
        ranker.fit(x_rank, relevance, group=groups)

        primary_cut = train.groupby("trade_date")["_base_score"].transform(lambda series: series.quantile(0.70))
        meta_train = train[train["_base_score"] >= primary_cut].copy()
        x_meta, _ = self._matrix(meta_train, feature_list, medians)
        y_meta = pd.to_numeric(meta_train["label_class"], errors="coerce").fillna(1).astype(int)
        if y_meta.nunique() < 3:
            return {
                "status": "insufficient_label_classes", "active": False,
                "label_distribution": {str(k): int(v) for k, v in y_meta.value_counts().items()},
                "train_rows": int(len(train)), "validation_rows": int(len(validation)),
            }
        classifier = lgb.LGBMClassifier(
            objective="multiclass",
            num_class=3,
            n_estimators=160,
            learning_rate=0.035,
            num_leaves=15,
            max_depth=5,
            min_child_samples=80,
            subsample=0.80,
            colsample_bytree=0.85,
            reg_lambda=2.0,
            class_weight="balanced",
            random_state=self.random_state,
            verbosity=-1,
        )
        classifier.fit(x_meta, y_meta)

        return_target = pd.to_numeric(
            train["next_3d_excess_return"], errors="coerce",
        ).fillna(0.0)
        clip_low = float(return_target.quantile(0.01))
        clip_high = float(return_target.quantile(0.99))
        regressor = lgb.LGBMRegressor(
            objective="huber",
            n_estimators=180,
            learning_rate=0.03,
            num_leaves=15,
            max_depth=5,
            min_child_samples=80,
            subsample=0.80,
            colsample_bytree=0.85,
            reg_lambda=2.0,
            random_state=self.random_state,
            verbosity=-1,
        )
        regressor.fit(x_train, return_target.clip(clip_low, clip_high))

        gross_target = pd.to_numeric(
            train.get("raw_forward_return", train["next_3d_excess_return"]), errors="coerce",
        ).fillna(0.0)
        gross_clip_low = float(gross_target.quantile(0.01))
        gross_clip_high = float(gross_target.quantile(0.99))
        gross_regressor = lgb.LGBMRegressor(
            objective="huber",
            n_estimators=180,
            learning_rate=0.03,
            num_leaves=15,
            max_depth=5,
            min_child_samples=80,
            subsample=0.80,
            colsample_bytree=0.85,
            reg_lambda=2.0,
            random_state=self.random_state,
            verbosity=-1,
        )
        gross_regressor.fit(x_train, gross_target.clip(gross_clip_low, gross_clip_high))

        stop_train_values = pd.to_numeric(
            train["stop_before_profit"]
            if "stop_before_profit" in train.columns
            else pd.Series(np.nan, index=train.index),
            errors="coerce",
        )
        stop_train = train[stop_train_values.notna()].copy()
        stop_classifier = None
        if not stop_train.empty:
            y_stop = pd.to_numeric(stop_train["stop_before_profit"], errors="coerce").astype(int)
            if y_stop.nunique() >= 2:
                x_stop, _ = self._matrix(stop_train, feature_list, medians)
                stop_classifier = lgb.LGBMClassifier(
                    objective="binary",
                    n_estimators=160,
                    learning_rate=0.035,
                    num_leaves=15,
                    max_depth=5,
                    min_child_samples=80,
                    subsample=0.80,
                    colsample_bytree=0.85,
                    reg_lambda=2.0,
                    random_state=self.random_state,
                    verbosity=-1,
                )
                stop_classifier.fit(x_stop, y_stop)

        calibration_probabilities = np.asarray(classifier.booster_.predict(x_calibration), dtype=float)
        if calibration_probabilities.ndim != 2 or calibration_probabilities.shape[1] < 3:
            return {"status": "invalid_multiclass_output", "active": False}
        strong_calibration_probability = np.clip(calibration_probabilities[:, 2], 1e-6, 1 - 1e-6)
        raw_calibration = np.log(strong_calibration_probability / (1.0 - strong_calibration_probability))
        y_calibration = (
            pd.to_numeric(calibration_frame["label_class"], errors="coerce").fillna(1).astype(int).to_numpy() == 2
        ).astype(int)
        if len(np.unique(y_calibration)) < 2:
            return {"status": "insufficient_calibration_classes", "active": False}
        calibrator = LogisticRegression(C=1.0, solver="lbfgs", random_state=self.random_state)
        calibrator.fit(raw_calibration.reshape(-1, 1), y_calibration)
        evaluation_probabilities = np.asarray(classifier.booster_.predict(x_evaluation), dtype=float)
        strong_evaluation_probability = np.clip(evaluation_probabilities[:, 2], 1e-6, 1 - 1e-6)
        raw_evaluation = np.log(strong_evaluation_probability / (1.0 - strong_evaluation_probability))
        y_evaluation = (
            pd.to_numeric(evaluation_frame["label_class"], errors="coerce").fillna(1).astype(int).to_numpy() == 2
        ).astype(int)
        probability = calibrator.predict_proba(raw_evaluation.reshape(-1, 1))[:, 1]
        calibration = calibration_metrics(y_evaluation, probability, bins=10)
        baseline = float(y_calibration.mean())
        evaluation_baseline = float(y_evaluation.mean())
        baseline_brier = float(np.mean((evaluation_baseline - y_evaluation) ** 2))
        calibration_return = pd.to_numeric(
            calibration_frame["next_3d_excess_return"], errors="coerce",
        ).fillna(0.0).to_numpy(dtype=float)
        calibration_return_prediction = np.asarray(
            regressor.predict(x_calibration), dtype=float,
        )
        return_interval = conformal_residual_interval(
            calibration_return, calibration_return_prediction, alpha=0.20,
        )
        evaluation_return_prediction = np.asarray(
            regressor.predict(x_evaluation), dtype=float,
        )
        evaluation_return = pd.to_numeric(
            evaluation_frame["next_3d_excess_return"], errors="coerce",
        ).fillna(0.0).to_numpy(dtype=float)
        conformal_radius = float(return_interval["radius"])
        conformal_coverage = float(np.mean(
            (evaluation_return >= evaluation_return_prediction - conformal_radius)
            & (evaluation_return <= evaluation_return_prediction + conformal_radius)
        ))
        calibration_gross = pd.to_numeric(
            calibration_frame.get("raw_forward_return", calibration_frame["next_3d_excess_return"]),
            errors="coerce",
        ).fillna(0.0).to_numpy(dtype=float)
        calibration_gross_prediction = np.asarray(
            gross_regressor.predict(x_calibration), dtype=float,
        )
        gross_return_interval = conformal_residual_interval(
            calibration_gross, calibration_gross_prediction, alpha=0.20,
        )
        evaluation_gross = pd.to_numeric(
            evaluation_frame.get("raw_forward_return", evaluation_frame["next_3d_excess_return"]),
            errors="coerce",
        ).fillna(0.0).to_numpy(dtype=float)
        evaluation_gross_prediction = np.asarray(
            gross_regressor.predict(x_evaluation), dtype=float,
        )
        gross_conformal_radius = float(gross_return_interval["radius"])
        gross_conformal_coverage = float(np.mean(
            (evaluation_gross >= evaluation_gross_prediction - gross_conformal_radius)
            & (evaluation_gross <= evaluation_gross_prediction + gross_conformal_radius)
        ))
        stop_evaluation_values = pd.to_numeric(
            evaluation_frame["stop_before_profit"]
            if "stop_before_profit" in evaluation_frame.columns
            else pd.Series(np.nan, index=evaluation_frame.index),
            errors="coerce",
        )
        stop_evaluation = evaluation_frame[stop_evaluation_values.notna()].copy()
        stop_diagnostics: Dict[str, Any] = {"available": False}
        stop_platt_coef = 0.0
        stop_platt_intercept = 0.0
        stop_model_deployable = False
        stop_calibration_values = pd.to_numeric(
            calibration_frame["stop_before_profit"]
            if "stop_before_profit" in calibration_frame.columns
            else pd.Series(np.nan, index=calibration_frame.index),
            errors="coerce",
        )
        stop_calibration = calibration_frame[stop_calibration_values.notna()].copy()
        if (
            stop_classifier is not None
            and not stop_calibration.empty
            and not stop_evaluation.empty
        ):
            x_stop_calibration, _ = self._matrix(stop_calibration, feature_list, medians)
            x_stop_evaluation, _ = self._matrix(stop_evaluation, feature_list, medians)
            y_stop_calibration = pd.to_numeric(
                stop_calibration["stop_before_profit"], errors="coerce",
            ).astype(int).to_numpy()
            y_stop_evaluation = pd.to_numeric(
                stop_evaluation["stop_before_profit"], errors="coerce",
            ).astype(int).to_numpy()
            if len(np.unique(y_stop_calibration)) >= 2:
                raw_stop_calibration = np.asarray(
                    stop_classifier.booster_.predict(x_stop_calibration, raw_score=True), dtype=float,
                )
                stop_calibrator = LogisticRegression(
                    C=1.0, solver="lbfgs", random_state=self.random_state,
                )
                stop_calibrator.fit(raw_stop_calibration.reshape(-1, 1), y_stop_calibration)
                raw_stop_evaluation = np.asarray(
                    stop_classifier.booster_.predict(x_stop_evaluation, raw_score=True), dtype=float,
                )
                stop_prediction = stop_calibrator.predict_proba(
                    raw_stop_evaluation.reshape(-1, 1),
                )[:, 1]
                stop_base_rate = float(y_stop_evaluation.mean())
                stop_brier = float(np.mean((stop_prediction - y_stop_evaluation) ** 2))
                stop_baseline_brier = float(np.mean((stop_base_rate - y_stop_evaluation) ** 2))
                stop_model_deployable = stop_brier <= stop_baseline_brier
                stop_platt_coef = float(stop_calibrator.coef_[0][0])
                stop_platt_intercept = float(stop_calibrator.intercept_[0])
                stop_diagnostics = {
                    "available": stop_model_deployable,
                    "sample_size": int(len(stop_evaluation)),
                    "calibration_sample_size": int(len(stop_calibration)),
                    "base_rate": stop_base_rate,
                    "brier_score": stop_brier,
                    "baseline_brier": stop_baseline_brier,
                    "gate_passed": stop_model_deployable,
                }

        rank_prediction = np.asarray(ranker.predict(x_evaluation), dtype=float)
        evaluation_target = pd.to_numeric(
            evaluation_frame["next_3d_excess_return"], errors="coerce",
        ).fillna(0.0)
        rank_series = pd.Series(rank_prediction, index=evaluation_frame.index)
        daily_percentile = rank_series.groupby(evaluation_frame["trade_date"]).rank(method="average", pct=True)
        top = evaluation_target[daily_percentile >= 0.90]
        rank_ic = float(rank_series.rank().corr(evaluation_target.rank())) if len(evaluation_target) > 2 else 0.0
        top_excess = float(top.mean() - evaluation_target.mean()) if len(top) else 0.0

        monthly_rank_ic = []
        for month, indices in evaluation_frame.groupby(
            evaluation_frame["trade_date"].astype(str).str.slice(0, 6), sort=True,
        ).groups.items():
            month_score = rank_series.loc[indices]
            month_target = evaluation_target.loc[indices]
            value = month_score.rank().corr(month_target.rank()) if len(month_target) > 2 else math.nan
            if pd.notna(value):
                monthly_rank_ic.append({"month": str(month), "rank_ic": float(value)})
        positive_months = sum(row["rank_ic"] > 0 for row in monthly_rank_ic[-3:])
        regime_source = evaluation_frame.get("market_regime")
        if regime_source is None:
            market_score = pd.to_numeric(evaluation_frame.get("market_score"), errors="coerce").fillna(50.0)
            regime_source = pd.Series(
                np.select([market_score >= 70.0, market_score < 45.0], ["strong", "weak"], default="neutral"),
                index=evaluation_frame.index,
            )
        regime_days = {
            regime: int(evaluation_frame.loc[regime_source.astype(str) == regime, "trade_date"].astype(str).nunique())
            for regime in ("strong", "neutral", "weak")
        }
        regime_coverage_ok = all(days >= 10 for days in regime_days.values())

        sample = x_evaluation.sample(min(len(x_evaluation), 2000), random_state=self.random_state)
        contributions = np.asarray(classifier.booster_.predict(sample, pred_contrib=True), dtype=float)
        if contributions.ndim == 2 and contributions.shape[1] >= (len(feature_list) + 1) * 3:
            start = (len(feature_list) + 1) * 2
            contributions = contributions[:, start:start + len(feature_list) + 1]
        mean_abs = np.mean(np.abs(contributions[:, :len(feature_list)]), axis=0)
        shap_summary = [
            {"factor": feature, "mean_abs_shap": float(value)}
            for feature, value in sorted(zip(feature_list, mean_abs), key=lambda item: item[1], reverse=True)
        ]

        gate_passed = bool(
            calibration["brier_score"] is not None
            and float(calibration["brier_score"]) <= baseline_brier
            and top_excess > 0
            and rank_ic > 0
            and len(monthly_rank_ic) >= 3
            and positive_months >= 2
            and regime_coverage_ok
        )
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        rank_file = directory / f"lgb_rank_{effective_date}.txt"
        meta_file = directory / f"lgb_meta_{effective_date}.txt"
        return_file = directory / f"lgb_return_{effective_date}.txt"
        gross_return_file = directory / f"lgb_gross_return_{effective_date}.txt"
        stop_file = directory / f"lgb_stop_{effective_date}.txt"
        ranker.booster_.save_model(str(rank_file))
        classifier.booster_.save_model(str(meta_file))
        regressor.booster_.save_model(str(return_file))
        gross_regressor.booster_.save_model(str(gross_return_file))
        if stop_classifier is not None and stop_model_deployable:
            stop_classifier.booster_.save_model(str(stop_file))
        return {
            "status": "active" if gate_passed else "rejected_oos_gate",
            "active": gate_passed,
            "model_type": "lightgbm_ranker_plus_meta_label",
            "classifier_mode": "three_class_strong_hold_avoid",
            "strong_class_index": 2,
            "label_version": "candidate_v3_three_tier_3d_stop4_profit6",
            "effective_date": str(effective_date),
            "features": feature_list,
            "medians": medians,
            "rank_model_file": rank_file.name,
            "meta_model_file": meta_file.name,
            "return_model_file": return_file.name,
            "gross_return_model_file": gross_return_file.name,
            "stop_model_file": stop_file.name if stop_classifier is not None and stop_model_deployable else "",
            "stop_calibration_method": "platt_out_of_sample" if stop_model_deployable else "",
            "stop_platt_coef": stop_platt_coef,
            "stop_platt_intercept": stop_platt_intercept,
            "calibration_method": "platt_out_of_sample",
            "platt_coef": float(calibrator.coef_[0][0]),
            "platt_intercept": float(calibrator.intercept_[0]),
            "baseline_probability": baseline,
            "evaluation_probability": evaluation_baseline,
            "baseline_brier": baseline_brier,
            "calibration": calibration,
            "return_conformal": {
                "alpha": 0.20,
                "radius": conformal_radius,
                "evaluation_coverage": conformal_coverage,
                "evaluation_mae": float(np.mean(np.abs(evaluation_return - evaluation_return_prediction))),
            },
            "return_clip_low": clip_low,
            "return_clip_high": clip_high,
            "gross_return_conformal": {
                "alpha": 0.20,
                "radius": gross_conformal_radius,
                "evaluation_coverage": gross_conformal_coverage,
                "evaluation_mae": float(np.mean(np.abs(evaluation_gross - evaluation_gross_prediction))),
            },
            "gross_return_clip_low": gross_clip_low,
            "gross_return_clip_high": gross_clip_high,
            "stop_model": stop_diagnostics,
            "rank_ic": rank_ic,
            "top_decile_excess_return": top_excess,
            "monthly_rank_ic": monthly_rank_ic,
            "positive_rank_ic_months_last3": int(positive_months),
            "regime_validation_days": regime_days,
            "regime_coverage_gate": regime_coverage_ok,
            "label_distribution": {str(k): int(v) for k, v in y_meta.value_counts().items()},
            "train_rows": int(len(train)),
            "meta_train_rows": int(len(meta_train)),
            "validation_rows": int(len(evaluation_frame)),
            "calibration_rows": int(len(calibration_frame)),
            "validation_days": int(evaluation_frame["trade_date"].astype(str).nunique()),
            "calibration_days": int(calibration_frame["trade_date"].astype(str).nunique()),
            "validation_start": str(evaluation_frame["trade_date"].astype(str).min()),
            "validation_end": str(evaluation_frame["trade_date"].astype(str).max()),
            "purged_split": split_audit,
            "shap_method": "lightgbm_tree_shap_pred_contrib",
            "shap_summary": shap_summary,
        }


class CandidateModelRuntime:
    def __init__(self, metadata: Mapping[str, Any], *, base_dir: Path) -> None:
        self.metadata = dict(metadata or {})
        self.base_dir = Path(base_dir)

    def available(self) -> bool:
        rank_name = str(self.metadata.get("rank_model_file") or "").strip()
        meta_name = str(self.metadata.get("meta_model_file") or "").strip()
        return bool(
            self.metadata.get("active")
            and rank_name
            and meta_name
            and (self.base_dir / rank_name).is_file()
            and (self.base_dir / meta_name).is_file()
        )

    def score(self, frame: pd.DataFrame) -> Dict[str, Any]:
        if frame.empty or not self.available():
            return {"available": False}
        import lightgbm as lgb  # type: ignore

        features = list(self.metadata.get("features") or [])
        matrix, _ = CandidateModelTrainer._matrix(frame, features, self.metadata.get("medians") or {})
        ranker = lgb.Booster(model_file=str(self.base_dir / self.metadata["rank_model_file"]))
        classifier = lgb.Booster(model_file=str(self.base_dir / self.metadata["meta_model_file"]))
        return_name = str(self.metadata.get("return_model_file") or "").strip()
        return_path = self.base_dir / return_name if return_name else None
        return_model = (
            lgb.Booster(model_file=str(return_path))
            if return_path is not None and return_path.is_file()
            else None
        )
        gross_return_name = str(self.metadata.get("gross_return_model_file") or "").strip()
        gross_return_path = self.base_dir / gross_return_name if gross_return_name else None
        gross_return_model = (
            lgb.Booster(model_file=str(gross_return_path))
            if gross_return_path is not None and gross_return_path.is_file()
            else None
        )
        stop_name = str(self.metadata.get("stop_model_file") or "").strip()
        stop_path = self.base_dir / stop_name if stop_name else None
        stop_model = (
            lgb.Booster(model_file=str(stop_path))
            if stop_path is not None and stop_path.is_file()
            else None
        )
        raw_rank = np.asarray(ranker.predict(matrix), dtype=float)
        rank_score = pd.Series(raw_rank, index=frame.index).rank(method="average", pct=True).mul(100.0)
        if self.metadata.get("calibration_method") == "platt_out_of_sample":
            if self.metadata.get("classifier_mode") == "three_class_strong_hold_avoid":
                class_probability = np.asarray(classifier.predict(matrix), dtype=float)
                strong_index = int(self.metadata.get("strong_class_index") or 2)
                strong_probability = np.clip(class_probability[:, strong_index], 1e-6, 1 - 1e-6)
                raw_margin = np.log(strong_probability / (1.0 - strong_probability))
            else:
                raw_margin = np.asarray(classifier.predict(matrix, raw_score=True), dtype=float)
            logits = (
                float(self.metadata.get("platt_coef") or 0.0) * raw_margin
                + float(self.metadata.get("platt_intercept") or 0.0)
            )
            probability = 1.0 / (1.0 + np.exp(-np.clip(logits, -35.0, 35.0)))
        else:
            raw_probability = np.asarray(classifier.predict(matrix), dtype=float)
            cal_x = np.asarray(self.metadata.get("calibrator_x") or [0.0, 1.0], dtype=float)
            cal_y = np.asarray(self.metadata.get("calibrator_y") or [0.0, 1.0], dtype=float)
            probability = np.interp(raw_probability, cal_x, cal_y)
        if return_model is not None:
            expected_return = np.asarray(return_model.predict(matrix), dtype=float)
            expected_return = np.clip(
                expected_return,
                float(self.metadata.get("return_clip_low") or -0.30),
                float(self.metadata.get("return_clip_high") or 0.30),
            )
            radius = float((self.metadata.get("return_conformal") or {}).get("radius") or 0.0)
        else:
            expected_return = np.full(len(frame), np.nan, dtype=float)
            radius = 0.0
        if gross_return_model is not None:
            expected_gross_return = np.asarray(gross_return_model.predict(matrix), dtype=float)
            expected_gross_return = np.clip(
                expected_gross_return,
                float(self.metadata.get("gross_return_clip_low") or -0.30),
                float(self.metadata.get("gross_return_clip_high") or 0.30),
            )
            gross_radius = float(
                (self.metadata.get("gross_return_conformal") or {}).get("radius") or 0.0
            )
        else:
            expected_gross_return = np.full(len(frame), np.nan, dtype=float)
            gross_radius = 0.0
        if stop_model is not None:
            if self.metadata.get("stop_calibration_method") == "platt_out_of_sample":
                raw_stop_margin = np.asarray(
                    stop_model.predict(matrix, raw_score=True), dtype=float,
                )
                stop_logits = (
                    float(self.metadata.get("stop_platt_coef") or 0.0) * raw_stop_margin
                    + float(self.metadata.get("stop_platt_intercept") or 0.0)
                )
                stop_probability = 1.0 / (1.0 + np.exp(-np.clip(stop_logits, -35.0, 35.0)))
            else:
                stop_probability = np.asarray(stop_model.predict(matrix), dtype=float)
        else:
            stop_probability = np.full(len(frame), np.nan, dtype=float)
        contributions = np.asarray(classifier.predict(matrix, pred_contrib=True), dtype=float)
        if (
            self.metadata.get("classifier_mode") == "three_class_strong_hold_avoid"
            and contributions.ndim == 2
            and contributions.shape[1] >= (len(features) + 1) * 3
        ):
            start = (len(features) + 1) * int(self.metadata.get("strong_class_index") or 2)
            contributions = contributions[:, start:start + len(features) + 1]
        explanations = []
        for row in contributions[:, :len(features)]:
            ordered = sorted(zip(features, row), key=lambda item: abs(item[1]), reverse=True)[:4]
            explanations.append([
                {"factor": factor, "contribution": float(value)} for factor, value in ordered
            ])
        return {
            "available": True,
            "rank_score": rank_score,
            "probability": pd.Series(probability, index=frame.index),
            "expected_return": pd.Series(expected_return, index=frame.index),
            "return_interval_low": pd.Series(expected_return - radius, index=frame.index),
            "return_interval_high": pd.Series(expected_return + radius, index=frame.index),
            "expected_gross_return": pd.Series(expected_gross_return, index=frame.index),
            "gross_return_interval_low": pd.Series(expected_gross_return - gross_radius, index=frame.index),
            "gross_return_interval_high": pd.Series(expected_gross_return + gross_radius, index=frame.index),
            "stop_probability": pd.Series(stop_probability, index=frame.index),
            "shap": pd.Series(explanations, index=frame.index),
        }


__all__ = ["CandidateModelRuntime", "CandidateModelTrainer"]
