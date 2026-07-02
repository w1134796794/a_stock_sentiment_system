"""Historical IC/IR analysis, constrained weight search and dated publication."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import yaml
from loguru import logger


_SAFE_FACTOR = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _normalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    clean = {str(key): max(float(value or 0.0), 0.0) for key, value in weights.items()}
    total = sum(clean.values())
    if total <= 0:
        return {key: 1.0 / len(clean) for key in clean} if clean else {}
    return {key: value / total for key, value in clean.items()}


def _cap_weights(weights: Mapping[str, float], cap: float) -> Dict[str, float]:
    """Project positive weights onto a simplex with an upper bound."""
    normalized = _normalize_weights(weights)
    if not normalized:
        return {}
    cap = max(float(cap), 1.0 / len(normalized))
    remaining = set(normalized)
    result: Dict[str, float] = {}
    budget = 1.0
    while remaining:
        subtotal = sum(normalized[key] for key in remaining)
        changed = False
        for key in list(remaining):
            proposed = budget * normalized[key] / max(subtotal, 1e-12)
            if proposed > cap + 1e-12:
                result[key] = cap
                budget -= cap
                remaining.remove(key)
                changed = True
        if not changed:
            subtotal = sum(normalized[key] for key in remaining)
            for key in remaining:
                result[key] = budget * normalized[key] / max(subtotal, 1e-12)
            break
    return result


@dataclass(frozen=True)
class WeightArtifact:
    profile: str
    effective_date: str
    weights: Dict[str, float]
    payload: Dict[str, Any]
    path: Path


class DynamicWeightRepository:
    """Store immutable weight versions and select only versions known by trade date."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            from config.settings import FACTOR_WEIGHT_DIR

            root = FACTOR_WEIGHT_DIR
        self.root = Path(root)

    def profile_dir(self, profile: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(profile or "default"))
        return self.root / safe

    def publish(self, payload: Mapping[str, Any]) -> Path:
        profile = str(payload.get("profile") or "default")
        effective = str(payload.get("effective_date") or "")
        if len(effective) != 8 or not effective.isdigit():
            raise ValueError("dynamic weight effective_date must be YYYYMMDD")
        directory = self.profile_dir(profile)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"weights_{effective}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def resolve(self, trade_date: str, profile: str = "default") -> Optional[WeightArtifact]:
        target = str(trade_date or "")
        directory = self.profile_dir(profile)
        if not directory.exists():
            return None
        candidates: List[Tuple[str, Path]] = []
        for path in directory.glob("weights_*.json"):
            effective = path.stem.removeprefix("weights_")
            if len(effective) == 8 and effective.isdigit() and effective <= target:
                candidates.append((effective, path))
        if not candidates:
            return None
        effective, path = max(candidates, key=lambda item: item[0])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            weights = _normalize_weights(payload.get("weights") or {})
            if not weights:
                return None
            return WeightArtifact(profile, effective, weights, payload, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[FactorLibrary] 动态权重读取失败 {path}: {exc}")
            return None


class FactorLibraryTrainer:
    """Fit non-negative ranking weights from daily cross-sectional IC/IR."""

    def __init__(
        self,
        *,
        duckdb_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        repository: Optional[DynamicWeightRepository] = None,
        horizon_days: int = 3,
        min_daily_samples: int = 30,
    ) -> None:
        from config.settings import BASE_DIR, FACTOR_DB_PATH

        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.profile_path = Path(profile_path or BASE_DIR / "config" / "screening_profiles.yaml")
        self.repository = repository or DynamicWeightRepository()
        self.horizon_days = max(int(horizon_days), 1)
        self.min_daily_samples = max(int(min_daily_samples), 10)

    def prior_weights(self, profile: str = "default") -> Dict[str, float]:
        data = yaml.safe_load(self.profile_path.read_text(encoding="utf-8")) or {}
        cfg = (data.get("screening_profiles") or {}).get(profile) or {}
        weights = ((cfg.get("ranking") or {}).get("prior_weights")
                   or (cfg.get("ranking") or {}).get("weights") or {})
        invalid = [key for key in weights if not _SAFE_FACTOR.match(str(key))]
        if invalid:
            raise ValueError(f"invalid factor identifiers: {invalid}")
        return _normalize_weights(weights)

    def load_training_frame(
        self, start_date: str, end_date: str, factors: Sequence[str],
    ) -> pd.DataFrame:
        if not self.duckdb_path.exists():
            return pd.DataFrame()
        import duckdb  # type: ignore

        factor_ids = [factor for factor in factors if factor != "tech_score"]
        for factor in factor_ids:
            if not _SAFE_FACTOR.match(factor):
                raise ValueError(f"unsafe factor identifier: {factor}")
        pivot_columns = ",\n".join(
            f"MAX(CASE WHEN factor_id = '{factor}' THEN score END) AS \"{factor}\""
            for factor in factor_ids
        )
        tech_column = ", w.tech_score AS tech_score" if "tech_score" in factors else ""
        sql = f"""
        WITH prices AS (
          SELECT trade_date, code, close,
                 LEAD(close, {self.horizon_days}) OVER (PARTITION BY code ORDER BY trade_date) AS future_close,
                 LEAD(trade_date, {self.horizon_days}) OVER (PARTITION BY code ORDER BY trade_date) AS future_date
          FROM stock_daily_silver
        ), factor_pivot AS (
          SELECT l.trade_date, l.entity_id AS code,
                 {pivot_columns}
          FROM factor_value_long l
          WHERE l.entity_type = 'stock'
            AND l.trade_date BETWEEN ? AND ?
            AND l.factor_id IN ({','.join('?' for _ in factor_ids)})
          GROUP BY l.trade_date, l.entity_id
        )
        SELECT f.*{tech_column},
               (p.future_close / NULLIF(p.close, 0) - 1.0) AS target_return,
               p.future_date
        FROM factor_pivot f
        JOIN prices p ON p.trade_date = f.trade_date AND p.code = f.code
        LEFT JOIN factor_stock_wide w
          ON w.trade_date = f.trade_date AND w.code = f.code
        WHERE p.future_date <= ? AND p.future_close > 0 AND p.close > 0
        ORDER BY f.trade_date, f.code
        """
        params: List[Any] = [str(start_date), str(end_date), *factor_ids, str(end_date)]
        con = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            return con.execute(sql, params).fetchdf()
        finally:
            con.close()

    def factor_metrics(self, frame: pd.DataFrame, factors: Sequence[str]) -> Dict[str, Dict[str, float]]:
        metrics: Dict[str, Dict[str, float]] = {}
        for factor in factors:
            daily: List[float] = []
            available = 0
            total = 0
            for _, group in frame.groupby("trade_date", sort=True):
                values = pd.to_numeric(group.get(factor), errors="coerce")
                target = pd.to_numeric(group.get("target_return"), errors="coerce")
                valid = values.notna() & target.notna()
                total += len(group)
                available += int(valid.sum())
                if valid.sum() < self.min_daily_samples or values[valid].nunique() < 2:
                    continue
                corr = values[valid].rank(method="average").corr(
                    target[valid].rank(method="average")
                )
                if pd.notna(corr):
                    daily.append(float(corr))
            series = pd.Series(daily, dtype=float)
            mean_ic = float(series.mean()) if not series.empty else 0.0
            std_ic = float(series.std(ddof=1)) if len(series) > 1 else 0.0
            metrics[factor] = {
                "daily_samples": int(len(series)),
                "coverage": float(available / total) if total else 0.0,
                "ic_mean": mean_ic,
                "ic_std": std_ic,
                "ic_ir": float(mean_ic / std_ic * math.sqrt(252)) if std_ic > 1e-12 else 0.0,
                "positive_ratio": float((series > 0).mean()) if not series.empty else 0.0,
            }
        return metrics

    @staticmethod
    def _learned_weights(metrics: Mapping[str, Mapping[str, float]]) -> Dict[str, float]:
        signal: Dict[str, float] = {}
        for factor, row in metrics.items():
            ic = max(float(row.get("ic_mean") or 0.0), 0.0)
            ir_reliability = min(abs(float(row.get("ic_ir") or 0.0)) / 2.0, 1.0)
            breadth = math.sqrt(max(float(row.get("coverage") or 0.0), 0.0))
            persistence = max(float(row.get("positive_ratio") or 0.0) - 0.45, 0.0) / 0.55
            signal[factor] = ic * (0.5 + 0.5 * ir_reliability) * breadth * (0.5 + 0.5 * persistence)
        return _normalize_weights(signal)

    @staticmethod
    def blend_weights(
        prior: Mapping[str, float], learned: Mapping[str, float], prior_blend: float, max_weight: float,
    ) -> Dict[str, float]:
        prior = _normalize_weights(prior)
        learned = _normalize_weights(learned) or prior
        blend = min(max(float(prior_blend), 0.0), 1.0)
        combined = {
            factor: blend * prior.get(factor, 0.0) + (1.0 - blend) * learned.get(factor, 0.0)
            for factor in prior
        }
        return _cap_weights(combined, max_weight)

    def evaluate_weights(
        self, frame: pd.DataFrame, weights: Mapping[str, float], top_n: int = 10,
    ) -> Dict[str, float]:
        daily_ic: List[float] = []
        daily_excess: List[float] = []
        for _, group in frame.groupby("trade_date", sort=True):
            if len(group) < self.min_daily_samples:
                continue
            score = pd.Series(0.0, index=group.index, dtype=float)
            for factor, weight in weights.items():
                values = pd.to_numeric(group.get(factor), errors="coerce")
                if values.notna().sum() < self.min_daily_samples or values.nunique(dropna=True) < 2:
                    percentile = pd.Series(0.5, index=group.index)
                else:
                    percentile = values.rank(method="average", pct=True).fillna(0.5)
                score += percentile * float(weight)
            target = pd.to_numeric(group["target_return"], errors="coerce")
            valid = score.notna() & target.notna()
            if valid.sum() < self.min_daily_samples:
                continue
            corr = score[valid].rank(method="average").corr(
                target[valid].rank(method="average")
            )
            if pd.notna(corr):
                daily_ic.append(float(corr))
            selected = target.loc[score[valid].nlargest(min(top_n, int(valid.sum()))).index]
            daily_excess.append(float(selected.mean() - target[valid].mean()))
        ic = pd.Series(daily_ic, dtype=float)
        excess = pd.Series(daily_excess, dtype=float)
        excess_ir = float(excess.mean() / excess.std(ddof=1)) if len(excess) > 1 and excess.std(ddof=1) > 0 else 0.0
        return {
            "days": int(len(excess)),
            "rank_ic": float(ic.mean()) if not ic.empty else 0.0,
            "top_excess_return": float(excess.mean()) if not excess.empty else 0.0,
            "top_excess_win_rate": float((excess > 0).mean()) if not excess.empty else 0.0,
            "objective": excess_ir + 0.5 * (float(ic.mean()) if not ic.empty else 0.0),
        }

    def fit_frame(
        self, frame: pd.DataFrame, prior: Mapping[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        factors = list(prior)
        months = frame["trade_date"].astype(str).str.slice(0, 6)
        unique_months = sorted(months.dropna().unique())
        tune_mask = months == unique_months[-1] if len(unique_months) > 1 else pd.Series(False, index=frame.index)
        learn_frame = frame.loc[~tune_mask] if (~tune_mask).any() else frame
        tune_frame = frame.loc[tune_mask] if tune_mask.any() else frame
        train_metrics = self.factor_metrics(learn_frame, factors)
        learned = self._learned_weights(train_metrics)

        trials: List[Dict[str, Any]] = []
        for prior_blend in (0.25, 0.50, 0.75):
            for max_weight in (0.20, 0.25, 0.35):
                weights = self.blend_weights(prior, learned, prior_blend, max_weight)
                evaluation = self.evaluate_weights(tune_frame, weights)
                trials.append({
                    "prior_blend": prior_blend,
                    "max_weight": max_weight,
                    "weights": weights,
                    **evaluation,
                })
        best = max(trials, key=lambda row: (row["objective"], row["rank_ic"]))
        full_metrics = self.factor_metrics(frame, factors)
        final_learned = self._learned_weights(full_metrics)
        final_weights = self.blend_weights(
            prior, final_learned, best["prior_blend"], best["max_weight"]
        )
        report = {
            "factor_metrics": full_metrics,
            "selected_hyperparameters": {
                "prior_blend": best["prior_blend"],
                "max_weight": best["max_weight"],
            },
            "tuning_evaluation": {key: best[key] for key in (
                "days", "rank_ic", "top_excess_return", "top_excess_win_rate", "objective"
            )},
            "search_trials": trials,
        }
        return final_weights, report

    def train_and_publish(
        self,
        start_date: str,
        end_date: str,
        *,
        profile: str = "default",
        effective_date: str = "",
    ) -> Dict[str, Any]:
        prior = self.prior_weights(profile)
        frame = self.load_training_frame(start_date, end_date, list(prior))
        if frame.empty or frame["trade_date"].nunique() < 20:
            raise RuntimeError("动态权重训练样本不足，至少需要 20 个有效交易日")
        weights, report = self.fit_frame(frame, prior)
        if not effective_date:
            effective_date = (datetime.strptime(str(end_date), "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        payload: Dict[str, Any] = {
            "schema_version": 1,
            "model_type": "ic_ir_constrained_blend",
            "profile": profile,
            "effective_date": str(effective_date),
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "train_start": str(frame["trade_date"].min()),
            "train_end": str(frame["trade_date"].max()),
            "horizon_days": self.horizon_days,
            "training_rows": int(len(frame)),
            "training_days": int(frame["trade_date"].nunique()),
            "prior_weights": prior,
            "weights": weights,
            **report,
        }
        path = self.repository.publish(payload)
        payload["path"] = str(path)
        logger.info(
            f"[FactorLibrary] 动态权重已发布 profile={profile} effective={effective_date} "
            f"days={payload['training_days']} path={path}"
        )
        return payload

    def walk_forward(
        self,
        start_date: str,
        end_date: str,
        *,
        profile: str = "default",
        train_months: int = 3,
    ) -> Dict[str, Any]:
        prior = self.prior_weights(profile)
        frame = self.load_training_frame(start_date, end_date, list(prior))
        if frame.empty:
            return {"folds": [], "summary": {"folds": 0}}
        frame = frame.copy()
        frame["month"] = frame["trade_date"].astype(str).str.slice(0, 6)
        months = sorted(frame["month"].unique())
        folds: List[Dict[str, Any]] = []
        for index in range(max(int(train_months), 1), len(months)):
            train_keys = months[index - train_months:index]
            validation_key = months[index]
            train = frame[frame["month"].isin(train_keys)]
            validation = frame[frame["month"] == validation_key]
            if train["trade_date"].nunique() < 20 or validation.empty:
                continue
            weights, report = self.fit_frame(train, prior)
            evaluation = self.evaluate_weights(validation, weights)
            effective = str(validation["trade_date"].min())
            payload = {
                "schema_version": 1,
                "model_type": "ic_ir_constrained_blend",
                "profile": profile,
                "effective_date": effective,
                "trained_at": datetime.now().isoformat(timespec="seconds"),
                "train_start": str(train["trade_date"].min()),
                "train_end": str(train["trade_date"].max()),
                "horizon_days": self.horizon_days,
                "training_rows": int(len(train)),
                "training_days": int(train["trade_date"].nunique()),
                "prior_weights": prior,
                "weights": weights,
                **report,
                "oos_evaluation": evaluation,
            }
            path = self.repository.publish(payload)
            folds.append({
                "effective_date": effective,
                "train_months": train_keys,
                "validation_month": validation_key,
                "path": str(path),
                **evaluation,
            })
        summary = {
            "folds": len(folds),
            "mean_oos_rank_ic": float(np.mean([row["rank_ic"] for row in folds])) if folds else 0.0,
            "mean_oos_top_excess_return": float(np.mean([row["top_excess_return"] for row in folds])) if folds else 0.0,
            "oos_positive_fold_ratio": float(np.mean([row["top_excess_return"] > 0 for row in folds])) if folds else 0.0,
        }
        return {"folds": folds, "summary": summary}

    def refresh_if_due(
        self,
        trade_date: str,
        previous_trade_date: str,
        *,
        profile: str = "default",
        lookback_days: int = 150,
    ) -> Optional[Dict[str, Any]]:
        """Publish once per month using data ending at the previous trade date."""
        if not previous_trade_date:
            return None
        current = self.repository.resolve(trade_date, profile)
        if current and current.effective_date[:6] == str(trade_date)[:6]:
            return None
        end = datetime.strptime(str(previous_trade_date), "%Y%m%d")
        start = (end - timedelta(days=max(int(lookback_days), 60))).strftime("%Y%m%d")
        return self.train_and_publish(
            start, str(previous_trade_date), profile=profile, effective_date=str(trade_date)
        )


__all__ = ["DynamicWeightRepository", "FactorLibraryTrainer", "WeightArtifact"]
