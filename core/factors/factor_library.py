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

from core.signals.trust_algorithms import (
    ADWINDetector,
    beta_binomial_interval,
    block_bootstrap_mean,
    calibration_metrics,
    conformal_residual_interval,
    distribution_reference,
    purged_month_split,
)


_SAFE_FACTOR = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _normalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    """Normalize by absolute exposure while preserving inverse factors."""
    clean = {str(key): float(value or 0.0) for key, value in weights.items()}
    total = sum(abs(value) for value in clean.values())
    if total <= 0:
        return {key: 1.0 / len(clean) for key in clean} if clean else {}
    return {key: value / total for key, value in clean.items()}


def _cap_weights(weights: Mapping[str, float], cap: float) -> Dict[str, float]:
    """Cap absolute factor exposure and retain each learned direction."""
    normalized = _normalize_weights(weights)
    if not normalized:
        return {}
    signs = {key: -1.0 if value < 0 else 1.0 for key, value in normalized.items()}
    normalized = {key: abs(value) for key, value in normalized.items()}
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
    return {key: value * signs[key] for key, value in result.items()}


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
    """Fit dated, regime-aware ranking weights from executable forward labels."""

    def __init__(
        self,
        *,
        duckdb_path: Optional[Path] = None,
        profile_path: Optional[Path] = None,
        repository: Optional[DynamicWeightRepository] = None,
        horizon_days: int = 3,
        min_daily_samples: int = 20,
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
        if not weights:
            try:
                from core.screening.strategy_profiles import StrategyProfileRepository

                strategy = StrategyProfileRepository().get_profile(profile) or {}
                weights = {
                    str(item.get("factor") or ""): float(item.get("weight") or 0.0)
                    for item in strategy.get("ranking_factors") or []
                    if str(item.get("factor") or "")
                }
            except Exception:
                weights = {}
        invalid = [key for key in weights if not _SAFE_FACTOR.match(str(key))]
        if invalid:
            raise ValueError(f"invalid factor identifiers: {invalid}")
        return _normalize_weights(weights)

    @staticmethod
    def _normalize_training_scope(scope: Any) -> str:
        value = str(scope or "all").strip().lower()
        return value if value in {"all", "near_limit"} else "all"

    def training_scope(self, profile: str = "default") -> str:
        """Read the model universe from the versioned strategy configuration."""
        try:
            from core.screening.strategy_profiles import StrategyProfileRepository

            strategy = StrategyProfileRepository().get_profile(profile) or {}
            return self._normalize_training_scope(strategy.get("training_scope"))
        except Exception:
            return "all"

    def _apply_training_scope(self, frame: pd.DataFrame, scope: str) -> pd.DataFrame:
        if frame.empty or self._normalize_training_scope(scope) != "near_limit":
            return frame
        limit_progress = pd.to_numeric(frame.get("limit_progress"), errors="coerce").fillna(0.0)
        liquidity = pd.to_numeric(frame.get("liquidity_score"), errors="coerce").fillna(0.0)
        return frame[(limit_progress >= 0.95) & (liquidity >= 35.0)].copy()

    def load_training_frame(
        self, start_date: str, end_date: str, factors: Sequence[str], *, training_scope: str = "all",
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
                 LEAD(trade_date, 1) OVER stock_window AS entry_date,
                 LEAD(open, 1) OVER stock_window AS entry_open,
                 LEAD(pre_close, 1) OVER stock_window AS entry_pre_close,
                 LEAD(vol_hand, 1) OVER stock_window AS entry_volume,
                 LEAD(low, 1) OVER stock_window AS low_1,
                 LEAD(high, 1) OVER stock_window AS high_1,
                 LEAD(low, 2) OVER stock_window AS low_2,
                 LEAD(high, 2) OVER stock_window AS high_2,
                 LEAD(low, 3) OVER stock_window AS low_3,
                 LEAD(high, 3) OVER stock_window AS high_3,
                 LEAD(close, {self.horizon_days}) OVER stock_window AS future_close,
                 LEAD(trade_date, {self.horizon_days}) OVER stock_window AS future_date
          FROM stock_daily_silver
          WINDOW stock_window AS (PARTITION BY code ORDER BY trade_date)
        ), factor_pivot AS (
          SELECT l.trade_date, l.entity_id AS code,
                 {pivot_columns}
          FROM factor_value_long l
          WHERE l.entity_type = 'stock'
            AND l.trade_date BETWEEN ? AND ?
            AND l.factor_id IN ({','.join('?' for _ in factor_ids)})
          GROUP BY l.trade_date, l.entity_id
        )
        SELECT f.*{tech_column}, w.resonance_sectors, w.limit_pct, w.limit_progress, w.liquidity_score,
               m.market_score,
               p.entry_date, p.future_date, p.entry_open,
               (p.future_close / NULLIF(p.entry_open, 0) - 1.0) AS raw_forward_return,
               (GREATEST(p.high_1, p.high_2, p.high_3) / NULLIF(p.entry_open, 0) - 1.0) AS mfe_3d,
               (LEAST(p.low_1, p.low_2, p.low_3) / NULLIF(p.entry_open, 0) - 1.0) AS mae_3d,
               CASE
                 WHEN p.low_1 <= p.entry_open * 0.96 AND p.high_1 >= p.entry_open * 1.06 THEN NULL
                 WHEN p.low_1 <= p.entry_open * 0.96 THEN 1
                 WHEN p.high_1 >= p.entry_open * 1.06 THEN 0
                 WHEN p.low_2 <= p.entry_open * 0.96 AND p.high_2 >= p.entry_open * 1.06 THEN NULL
                 WHEN p.low_2 <= p.entry_open * 0.96 THEN 1
                 WHEN p.high_2 >= p.entry_open * 1.06 THEN 0
                 WHEN p.low_3 <= p.entry_open * 0.96 AND p.high_3 >= p.entry_open * 1.06 THEN NULL
                 WHEN p.low_3 <= p.entry_open * 0.96 THEN 1
                 ELSE 0
               END AS stop_before_profit,
               CASE WHEN p.entry_open > 0 AND p.entry_volume > 0
                         AND p.low_1 < p.entry_pre_close * (1.0 + COALESCE(w.limit_pct, 10.0) / 100.0) * 0.998
                    THEN 1 ELSE 0 END AS tradable_next_day
        FROM factor_pivot f
        JOIN prices p ON p.trade_date = f.trade_date AND p.code = f.code
        LEFT JOIN factor_stock_wide w
          ON w.trade_date = f.trade_date AND w.code = f.code
        LEFT JOIN factor_market_wide m ON m.trade_date = f.trade_date
        WHERE p.future_date <= ? AND p.future_close > 0 AND p.close > 0
        ORDER BY f.trade_date, f.code
        """
        params: List[Any] = [str(start_date), str(end_date), *factor_ids, str(end_date)]
        con = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            frame = con.execute(sql, params).fetchdf()
        finally:
            con.close()
        if frame.empty:
            return frame
        frame["primary_sector"] = (
            frame.get("resonance_sectors", pd.Series("", index=frame.index))
            .fillna("").astype(str).str.split(",").str[0].str.strip()
        )
        raw = pd.to_numeric(frame["raw_forward_return"], errors="coerce")
        market_return = raw.groupby(frame["trade_date"]).transform("mean")
        sector_return = raw.groupby([frame["trade_date"], frame["primary_sector"]]).transform("mean")
        sector_return = sector_return.where(frame["primary_sector"].ne(""), market_return)
        frame["next_3d_excess_return"] = raw - (market_return + sector_return) / 2.0
        frame["target_return"] = frame["next_3d_excess_return"]
        score = pd.to_numeric(frame.get("market_score"), errors="coerce").fillna(50.0)
        frame["market_regime"] = np.select(
            [score >= 70.0, score < 45.0], ["strong", "weak"], default="neutral",
        )
        strong = (
            (pd.to_numeric(frame["tradable_next_day"], errors="coerce").fillna(0) > 0)
            & (pd.to_numeric(frame["mfe_3d"], errors="coerce").fillna(0) >= 0.08)
            & (frame["next_3d_excess_return"].fillna(0) > 0)
        )
        avoid = (
            (pd.to_numeric(frame["mae_3d"], errors="coerce").fillna(0) <= -0.04)
            & (pd.to_numeric(frame["mfe_3d"], errors="coerce").fillna(0) < 0.02)
        )
        frame["label_class"] = np.select([strong, avoid], [2, 0], default=1).astype(int)
        frame["label_name"] = frame["label_class"].map({2: "strong_buy", 1: "hold", 0: "avoid"})
        frame["label_strong_buy"] = (frame["label_class"] == 2).astype(int)
        frame["label_hold"] = (frame["label_class"] == 1).astype(int)
        frame["label_avoid"] = (frame["label_class"] == 0).astype(int)
        frame["label_success"] = frame["label_strong_buy"]
        return self._apply_training_scope(frame, training_scope)

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
                "direction": 1 if mean_ic >= 0 else -1,
                "sign_consistency": float(((series * (1 if mean_ic >= 0 else -1)) > 0).mean()) if not series.empty else 0.0,
            }
        return metrics

    @staticmethod
    def _learned_weights(
        metrics: Mapping[str, Mapping[str, float]], disabled: Iterable[str] = (),
    ) -> Dict[str, float]:
        disabled_set = set(disabled)
        signal: Dict[str, float] = {}
        for factor, row in metrics.items():
            if factor in disabled_set:
                signal[factor] = 0.0
                continue
            raw_ic = float(row.get("ic_mean") or 0.0)
            direction = -1.0 if raw_ic < 0 else 1.0
            ic = abs(raw_ic)
            ir_reliability = min(abs(float(row.get("ic_ir") or 0.0)) / 2.0, 1.0)
            breadth = math.sqrt(max(float(row.get("coverage") or 0.0), 0.0))
            consistency = max(float(row.get("sign_consistency") or 0.0) - 0.45, 0.0) / 0.55
            signal[factor] = direction * ic * (0.5 + 0.5 * ir_reliability) * breadth * (0.5 + 0.5 * consistency)
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
        daily_selected_returns: List[float] = []
        total_days = int(frame["trade_date"].astype(str).nunique()) if not frame.empty else 0
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
                direction_score = percentile if float(weight) >= 0 else 1.0 - percentile
                score += direction_score * abs(float(weight))
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
            daily_selected_returns.append(float(selected.mean()))
        ic = pd.Series(daily_ic, dtype=float)
        excess = pd.Series(daily_excess, dtype=float)
        selected_returns = pd.Series(daily_selected_returns, dtype=float)
        excess_ir = float(excess.mean() / excess.std(ddof=1)) if len(excess) > 1 and excess.std(ddof=1) > 0 else 0.0
        gains = float(selected_returns[selected_returns > 0].sum())
        losses = abs(float(selected_returns[selected_returns < 0].sum()))
        profit_factor = gains / losses if losses > 1e-12 else (10.0 if gains > 0 else 0.0)
        if selected_returns.empty:
            total_return = max_drawdown = calmar_ratio = 0.0
        else:
            equity = (1.0 + selected_returns.clip(lower=-0.95)).cumprod()
            drawdown = equity / equity.cummax() - 1.0
            total_return = float(equity.iloc[-1] - 1.0)
            max_drawdown = float(drawdown.min())
            calmar_ratio = total_return / abs(max_drawdown) if max_drawdown < -1e-12 else min(total_return * 10.0, 10.0)
        coverage = len(selected_returns) / total_days if total_days else 0.0
        objective = (
            0.45 * math.log1p(min(max(profit_factor, 0.0), 10.0))
            + 0.35 * max(min(calmar_ratio, 10.0), -5.0)
            + 0.15 * coverage
            + 0.05 * (float(ic.mean()) if not ic.empty else 0.0)
        )
        return {
            "days": int(len(excess)),
            "rank_ic": float(ic.mean()) if not ic.empty else 0.0,
            "top_excess_return": float(excess.mean()) if not excess.empty else 0.0,
            "top_excess_win_rate": float((excess > 0).mean()) if not excess.empty else 0.0,
            "excess_ir": excess_ir,
            "profit_factor": float(profit_factor),
            "calmar_ratio": float(calmar_ratio),
            "max_drawdown": float(max_drawdown),
            "coverage": float(coverage),
            "objective": float(objective),
        }

    @staticmethod
    def _redundant_factors(
        frame: pd.DataFrame,
        factors: Sequence[str],
        metrics: Mapping[str, Mapping[str, float]],
        threshold: float = 0.85,
    ) -> Dict[str, str]:
        """Drop the weaker member of highly correlated factor pairs."""
        available = [factor for factor in factors if factor in frame.columns]
        if len(available) < 2:
            return {}
        ranked = sorted(
            available,
            key=lambda factor: abs(float((metrics.get(factor) or {}).get("ic_mean") or 0.0)),
            reverse=True,
        )
        kept: List[str] = []
        dropped: Dict[str, str] = {}
        for factor in ranked:
            values = pd.to_numeric(frame[factor], errors="coerce")
            duplicate = ""
            for existing in kept:
                corr = values.rank(method="average").corr(
                    pd.to_numeric(frame[existing], errors="coerce").rank(method="average")
                )
                if pd.notna(corr) and abs(float(corr)) >= threshold:
                    duplicate = existing
                    break
            if duplicate:
                dropped[factor] = duplicate
            else:
                kept.append(factor)
        return dropped

    @staticmethod
    def _score_frame(frame: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
        score = pd.Series(0.0, index=frame.index, dtype=float)
        total = sum(abs(float(weight)) for weight in weights.values())
        if total <= 0:
            return pd.Series(50.0, index=frame.index, dtype=float)
        for factor, weight in weights.items():
            values = pd.to_numeric(frame.get(factor), errors="coerce")
            if values.notna().sum() < 2 or values.nunique(dropna=True) < 2:
                percentile = pd.Series(0.5, index=frame.index, dtype=float)
            else:
                percentile = values.groupby(frame["trade_date"]).rank(method="average", pct=True).fillna(0.5)
            component = percentile if float(weight) >= 0 else 1.0 - percentile
            score += component * abs(float(weight))
        return score / total * 100.0

    @staticmethod
    def _isotonic_increasing(values: Sequence[float], weights: Sequence[int]) -> List[float]:
        """Pool-adjacent-violators calibration without an sklearn dependency."""
        blocks: List[Dict[str, Any]] = []
        for index, (value, weight) in enumerate(zip(values, weights)):
            blocks.append({"start": index, "end": index, "weight": max(int(weight), 1), "value": float(value)})
            while len(blocks) >= 2 and blocks[-2]["value"] > blocks[-1]["value"]:
                right = blocks.pop()
                left = blocks.pop()
                total = left["weight"] + right["weight"]
                blocks.append({
                    "start": left["start"], "end": right["end"], "weight": total,
                    "value": (left["value"] * left["weight"] + right["value"] * right["weight"]) / total,
                })
        fitted = [0.0] * len(values)
        for block in blocks:
            for index in range(block["start"], block["end"] + 1):
                fitted[index] = float(block["value"])
        return fitted

    def _confidence_profile(
        self, frame: pd.DataFrame, weights: Mapping[str, float], bins: int = 100,
    ) -> Dict[str, Any]:
        if frame.empty:
            return {"sample_size": 0, "bins": [], "monotonic_top3": False}
        data = frame.copy()
        data["model_score"] = self._score_frame(data, weights)
        monotonic = False
        try:
            validation_bins = pd.qcut(data["model_score"], q=10, duplicates="drop")
            validation = data.assign(_validation_bin=validation_bins).groupby(
                "_validation_bin", observed=True,
            )["next_3d_excess_return" if "next_3d_excess_return" in data else "target_return"].mean()
            top_returns = list(validation.sort_index(ascending=False).head(3))
            monotonic = len(top_returns) >= 3 and top_returns[0] >= top_returns[1] >= top_returns[2]
        except (ValueError, KeyError):
            monotonic = False
        try:
            bin_count = min(max(int(bins), 10), max(len(data) // 400, 10))
            data["score_bin"] = pd.qcut(data["model_score"], q=bin_count, duplicates="drop")
        except ValueError:
            data["score_bin"] = "all"
        def numeric(name: str, default: float = math.nan) -> pd.Series:
            source = data[name] if name in data.columns else pd.Series(default, index=data.index)
            return pd.to_numeric(source, errors="coerce")

        rows: List[Dict[str, Any]] = []
        prediction_by_index = pd.Series(math.nan, index=data.index, dtype=float)
        return_prediction_by_index = pd.Series(math.nan, index=data.index, dtype=float)
        for _, group in data.groupby("score_bin", observed=True):
            def group_numeric(name: str, fallback: str = "") -> pd.Series:
                if name in group.columns:
                    return pd.to_numeric(group[name], errors="coerce")
                if fallback and fallback in group.columns:
                    return pd.to_numeric(group[fallback], errors="coerce")
                return pd.Series(math.nan, index=group.index, dtype=float)

            success = group_numeric("label_success")
            target = group_numeric("next_3d_excess_return", "target_return")
            stop = group_numeric("stop_before_profit")
            mfe = group_numeric("mfe_3d")
            mae = group_numeric("mae_3d")
            rows.append({
                "score_min": round(float(group["model_score"].min()), 4),
                "score_max": round(float(group["model_score"].max()), 4),
                "score_center": round(float(group["model_score"].mean()), 4),
                "sample_size": int(len(group)),
                "success_probability": float(success.mean()) if success.notna().any() else 0.5,
                "expected_return": float(target.mean()) if target.notna().any() else 0.0,
                "stop_probability": float(stop.mean()) if stop.notna().any() else 0.5,
                "average_mfe": float(mfe.mean()) if mfe.notna().any() else 0.0,
                "average_mae": float(mae.mean()) if mae.notna().any() else 0.0,
                "_index": list(group.index),
                "_returns": [float(value) for value in target.dropna().tolist()],
            })
        rows.sort(key=lambda row: row["score_center"])
        fitted_probability = self._isotonic_increasing(
            [row["success_probability"] for row in rows], [row["sample_size"] for row in rows],
        )
        fitted_return = self._isotonic_increasing(
            [row["expected_return"] for row in rows], [row["sample_size"] for row in rows],
        )
        for row, probability, expected in zip(rows, fitted_probability, fitted_return):
            row["observed_success_probability"] = row["success_probability"]
            row["observed_expected_return"] = row["expected_return"]
            row["success_probability"] = float(probability)
            row["expected_return"] = float(expected)
            posterior = beta_binomial_interval(
                float(row["observed_success_probability"]) * int(row["sample_size"]),
                int(row["sample_size"]),
            )
            returns = row.pop("_returns")
            interval = conformal_residual_interval(
                returns,
                [float(expected)] * len(returns),
                alpha=0.20,
            )
            row["probability_ci_low"] = float(posterior["lower"])
            row["probability_ci_high"] = float(posterior["upper"])
            row["return_interval_low"] = float(expected - interval["radius"])
            row["return_interval_high"] = float(expected + interval["radius"])
            row["conformal_radius"] = float(interval["radius"])
            indices = row.pop("_index")
            prediction_by_index.loc[indices] = float(probability)
            return_prediction_by_index.loc[indices] = float(expected)
        calibration = calibration_metrics(
            numeric("label_success"), prediction_by_index, bins=10,
        )
        conformal = conformal_residual_interval(
            numeric("next_3d_excess_return"), return_prediction_by_index, alpha=0.20,
        )
        bootstrap = block_bootstrap_mean(
            numeric("next_3d_excess_return"), data["trade_date"], simulations=500,
        )
        daily_errors = pd.DataFrame({
            "trade_date": data["trade_date"].astype(str),
            "error": (numeric("label_success") - prediction_by_index) ** 2,
        }).dropna().groupby("trade_date", as_index=False)["error"].mean()
        detector = ADWINDetector(delta=0.01, min_window=10)
        change_dates = [
            str(row.trade_date) for row in daily_errors.itertuples()
            if detector.update(row.error)
        ]
        rows.sort(key=lambda row: row["score_center"], reverse=True)
        return {
            "sample_size": int(len(data)),
            "success_probability": float(numeric("label_success", 0.5).mean()),
            "expected_return": float((numeric("next_3d_excess_return") if "next_3d_excess_return" in data else numeric("target_return", 0.0)).mean()),
            "stop_probability": float(numeric("stop_before_profit", 0.5).mean()),
            "average_mfe": float(numeric("mfe_3d", 0.0).mean()),
            "average_mae": float(numeric("mae_3d", 0.0).mean()),
            "monotonic_top3": monotonic,
            "calibration": calibration,
            "conformal": conformal,
            "bootstrap": bootstrap,
            "adwin": {
                "status": "changed" if change_dates else "stable",
                "change_dates": change_dates,
                "latest_change_date": change_dates[-1] if change_dates else "",
                "daily_samples": int(len(daily_errors)),
            },
            "bins": rows,
        }

    def fit_frame(
        self, frame: pd.DataFrame, prior: Mapping[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, Any]]:
        factors = list(prior)
        market_regime_model = self._fit_market_regime(frame)
        regime_frame = frame
        date_states = market_regime_model.get("date_states") or {}
        if date_states:
            regime_frame = frame.copy()
            hmm_regime = regime_frame["trade_date"].astype(str).map(date_states)
            fallback_regime = (
                regime_frame["market_regime"]
                if "market_regime" in regime_frame.columns
                else pd.Series("neutral", index=regime_frame.index)
            )
            regime_frame["market_regime"] = hmm_regime.fillna(fallback_regime)
        months = frame["trade_date"].astype(str).str.slice(0, 6)
        unique_months = sorted(months.dropna().unique())
        tune_mask = months == unique_months[-1] if len(unique_months) > 1 else pd.Series(False, index=frame.index)
        learn_frame = frame.loc[~tune_mask] if (~tune_mask).any() else frame
        tune_frame = frame.loc[tune_mask] if tune_mask.any() else frame
        train_metrics = self.factor_metrics(learn_frame, factors)
        redundant = self._redundant_factors(learn_frame, factors, train_metrics)
        learned = self._learned_weights(train_metrics, redundant)

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
        full_redundant = self._redundant_factors(frame, factors, full_metrics)
        final_learned = self._learned_weights(full_metrics, full_redundant)
        final_weights = self.blend_weights(
            prior, final_learned, best["prior_blend"], best["max_weight"]
        )
        regime_weights: Dict[str, Dict[str, float]] = {}
        rejected_regimes: Dict[str, str] = {}
        feature_reference = {
            factor: distribution_reference(pd.to_numeric(frame.get(factor), errors="coerce"))
            for factor in factors
        }
        feature_reference_by_regime: Dict[str, Dict[str, Dict[str, Any]]] = {}
        feature_reference_regime_meta: Dict[str, Dict[str, int]] = {}
        confidence_profiles: Dict[str, Dict[str, Any]] = {
            "all": self._confidence_profile(frame, final_weights),
        }
        if "market_regime" in regime_frame.columns:
            for regime in ("strong", "neutral", "weak"):
                subset = regime_frame[regime_frame["market_regime"] == regime]
                regime_days = int(subset["trade_date"].astype(str).nunique())
                if len(subset) >= 200 and regime_days >= 5:
                    feature_reference_by_regime[regime] = {
                        factor: distribution_reference(
                            pd.to_numeric(subset.get(factor), errors="coerce")
                        )
                        for factor in factors
                    }
                    feature_reference_regime_meta[regime] = {
                        "sample_size": int(len(subset)),
                        "trade_days": regime_days,
                    }
                if subset.empty or subset["trade_date"].nunique() < 10:
                    continue
                regime_metrics = self.factor_metrics(subset, factors)
                regime_redundant = self._redundant_factors(subset, factors, regime_metrics)
                regime_learned = self._learned_weights(regime_metrics, regime_redundant)
                regime_weight = self.blend_weights(
                    prior, regime_learned, best["prior_blend"], best["max_weight"]
                )
                regime_profile = self._confidence_profile(subset, regime_weight)
                confidence_profiles[regime] = regime_profile
                if regime_profile.get("monotonic_top3"):
                    regime_weights[regime] = regime_weight
                else:
                    rejected_regimes[regime] = "候选前3个分组的预期收益不单调"
        report = {
            "factor_metrics": full_metrics,
            "inverse_factors": [factor for factor, weight in final_weights.items() if weight < 0],
            "correlation_pruned": full_redundant,
            "regime_weights": regime_weights,
            "rejected_regime_models": rejected_regimes,
            "confidence_profiles": confidence_profiles,
            "feature_reference": feature_reference,
            "feature_reference_by_regime": feature_reference_by_regime,
            "feature_reference_regime_meta": feature_reference_regime_meta,
            "lightgbm": {
                "available": self._lightgbm_available(),
                "status": "optional_next_stage" if self._lightgbm_available() else "dependency_missing_ic_ir_fallback",
            },
            "market_regime_model": market_regime_model,
            "market_regime_assignment": (
                "gaussian_hmm_3_state"
                if date_states else "threshold_fallback"
            ),
            "selected_hyperparameters": {
                "prior_blend": best["prior_blend"],
                "max_weight": best["max_weight"],
            },
            "tuning_evaluation": {key: best[key] for key in (
                "days", "rank_ic", "top_excess_return", "top_excess_win_rate",
                "profit_factor", "calmar_ratio", "max_drawdown", "coverage", "objective"
            )},
            "search_trials": trials,
        }
        return final_weights, report

    @staticmethod
    def _fit_market_regime(frame: pd.DataFrame) -> Dict[str, Any]:
        try:
            from core.models.market_regime import MarketRegimeDetector

            return MarketRegimeDetector.fit(frame)
        except Exception as exc:
            return {"status": "fit_failed", "method": "threshold_fallback", "reason": str(exc)}

    def _fit_candidate_model(
        self,
        frame: pd.DataFrame,
        *,
        features: Sequence[str],
        weights: Mapping[str, float],
        profile: str,
        effective_date: str,
    ) -> Dict[str, Any]:
        try:
            from core.models.candidate_model import CandidateModelTrainer

            return CandidateModelTrainer().fit_and_save(
                frame,
                features=features,
                base_scores=self._score_frame(frame, weights),
                directory=self.repository.profile_dir(profile),
                effective_date=effective_date,
            )
        except Exception as exc:
            logger.warning(f"[FactorLibrary] LightGBM候选模型训练失败，保留IC/IR基线: {exc}")
            return {"status": "fit_failed", "active": False, "reason": str(exc)}

    @staticmethod
    def _lightgbm_available() -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("lightgbm") is not None
        except Exception:
            return False

    def train_and_publish(
        self,
        start_date: str,
        end_date: str,
        *,
        profile: str = "default",
        effective_date: str = "",
    ) -> Dict[str, Any]:
        prior = self.prior_weights(profile)
        training_scope = self.training_scope(profile)
        frame = self.load_training_frame(
            start_date, end_date, list(prior), training_scope=training_scope,
        )
        if frame.empty or frame["trade_date"].nunique() < 20:
            raise RuntimeError("动态权重训练样本不足，至少需要 20 个有效交易日")
        weights, report = self.fit_frame(frame, prior)
        monthly_gate = self._rolling_month_gate(frame, prior)
        gate_passed = bool(
            (report.get("confidence_profiles") or {}).get("all", {}).get("monotonic_top3")
            and monthly_gate.get("passed")
        )
        if not gate_passed:
            weights = prior
            report["confidence_profiles"]["all"] = self._confidence_profile(frame, weights)
        report["publication_gate"] = {
            "passed": gate_passed,
            "reason": (
                "收益分组单调，且最近3个样本外月份至少2个月Rank IC为正"
                if gate_passed else "动态模型未通过收益单调性或连续月样本外闸门，生产权重回退冷启动先验"
            ),
            "monthly_oos": monthly_gate,
        }
        if not effective_date:
            effective_date = (datetime.strptime(str(end_date), "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        candidate_frame = frame.copy()
        candidate_states = (report.get("market_regime_model") or {}).get("date_states") or {}
        if candidate_states:
            candidate_frame["market_regime"] = (
                candidate_frame["trade_date"].astype(str).map(candidate_states)
                .fillna(candidate_frame.get("market_regime", "neutral"))
            )
        candidate_model = self._fit_candidate_model(
            candidate_frame,
            features=list(prior),
            weights=weights,
            profile=profile,
            effective_date=str(effective_date),
        )
        candidate_model["training_scope"] = training_scope
        report["candidate_model"] = candidate_model
        report["lightgbm"] = {
            "available": self._lightgbm_available(),
            "status": candidate_model.get("status", "not_trained"),
            "active": bool(candidate_model.get("active")),
        }
        outcome_rows = self.persist_outcome_labels(frame)
        payload: Dict[str, Any] = {
            "schema_version": 2,
            "model_type": (
                "lightgbm_rank_meta_with_ic_ir_fallback"
                if candidate_model.get("active")
                else "ic_ir_constrained_blend" if gate_passed
                else "prior_fallback_unstable_dynamic_model"
            ),
            "profile": profile,
            "training_scope": training_scope,
            "effective_date": str(effective_date),
            "trained_at": datetime.now().isoformat(timespec="seconds"),
            "train_start": str(frame["trade_date"].min()),
            "train_end": str(frame["trade_date"].max()),
            "horizon_days": self.horizon_days,
            "training_rows": int(len(frame)),
            "training_days": int(frame["trade_date"].nunique()),
            "outcome_rows": outcome_rows,
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

    def persist_outcome_labels(self, frame: pd.DataFrame) -> int:
        """Persist point-in-time candidate outcomes for audit and later calibration."""
        columns = [
            "trade_date", "code", "entry_date", "future_date", "market_regime",
            "primary_sector", "next_3d_excess_return", "mfe_3d", "mae_3d",
            "stop_before_profit", "tradable_next_day", "label_class", "label_name",
            "label_strong_buy", "label_hold", "label_avoid", "label_success",
        ]
        if frame.empty or not self.duckdb_path.exists() or not set(columns).issubset(frame.columns):
            return 0
        labels = frame[columns].copy()
        labels["label_version"] = "candidate_v3_three_tier_3d_stop4_profit6"
        labels["computed_at"] = datetime.now().isoformat(timespec="seconds")
        import duckdb  # type: ignore

        con = duckdb.connect(str(self.duckdb_path))
        try:
            con.register("_signal_outcomes", labels)
            con.execute(
                "CREATE TABLE IF NOT EXISTS signal_outcome_wide AS "
                "SELECT * FROM _signal_outcomes WHERE 1=0"
            )
            source_schema = {
                str(row[0]): str(row[1])
                for row in con.execute("DESCRIBE _signal_outcomes").fetchall()
            }
            target_columns = {
                str(row[1])
                for row in con.execute("PRAGMA table_info('signal_outcome_wide')").fetchall()
            }
            for column, column_type in source_schema.items():
                if column not in target_columns:
                    escaped = column.replace('"', '""')
                    con.execute(
                        f'ALTER TABLE signal_outcome_wide ADD COLUMN "{escaped}" {column_type}'
                    )
            start = str(labels["trade_date"].min())
            end = str(labels["trade_date"].max())
            con.execute(
                "DELETE FROM signal_outcome_wide WHERE CAST(trade_date AS VARCHAR) BETWEEN ? AND ?",
                [start, end],
            )
            quoted_columns = ", ".join(
                f'"{column.replace(chr(34), chr(34) * 2)}"' for column in labels.columns
            )
            con.execute(
                f"INSERT INTO signal_outcome_wide ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM _signal_outcomes"
            )
        finally:
            try:
                con.unregister("_signal_outcomes")
            except Exception:
                pass
            con.close()
        return int(len(labels))

    def walk_forward(
        self,
        start_date: str,
        end_date: str,
        *,
        profile: str = "default",
        train_months: int = 12,
    ) -> Dict[str, Any]:
        prior = self.prior_weights(profile)
        training_scope = self.training_scope(profile)
        frame = self.load_training_frame(
            start_date, end_date, list(prior), training_scope=training_scope,
        )
        if frame.empty:
            return {"folds": [], "summary": {"folds": 0}}
        frame = frame.copy()
        frame["month"] = frame["trade_date"].astype(str).str.slice(0, 6)
        months = sorted(frame["month"].unique())
        folds: List[Dict[str, Any]] = []
        for index in range(max(int(train_months), 1), len(months)):
            train_keys = months[index - train_months:index]
            validation_key = months[index]
            candidate = frame[frame["month"].isin([*train_keys, validation_key])].copy()
            validation_rows = candidate[candidate["month"] == validation_key]
            validation_start = str(validation_rows["trade_date"].min())
            validation_end = str(validation_rows["trade_date"].max())
            train, validation, split_audit = purged_month_split(
                candidate,
                validation_start=validation_start,
                validation_end=validation_end,
                embargo_days=self.horizon_days,
            )
            train = train[train["month"].isin(train_keys)]
            if train["trade_date"].nunique() < 20 or validation.empty:
                continue
            weights, report = self.fit_frame(train, prior)
            evaluation = self.evaluate_weights(validation, weights)
            effective = str(validation["trade_date"].min())
            candidate_model = self._fit_candidate_model(
                train,
                features=list(prior),
                weights=weights,
                profile=profile,
                effective_date=effective,
            )
            candidate_model["training_scope"] = training_scope
            report["candidate_model"] = candidate_model
            payload = {
                "schema_version": 2,
                "model_type": "lightgbm_rank_meta_with_ic_ir_fallback" if candidate_model.get("active") else "ic_ir_constrained_blend",
                "profile": profile,
                "training_scope": training_scope,
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
                "purged_split": split_audit,
            }
            path = self.repository.publish(payload)
            folds.append({
                "effective_date": effective,
                "train_months": train_keys,
                "validation_month": validation_key,
                "path": str(path),
                "purged_rows": split_audit["purged_rows"],
                "embargo_dates": split_audit["embargo_dates"],
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
        lookback_days: int = 300,
    ) -> Optional[Dict[str, Any]]:
        """Publish once per month using data ending at the previous trade date."""
        if not previous_trade_date:
            return None
        current = self.repository.resolve(trade_date, profile)
        if current and current.effective_date[:6] == str(trade_date)[:6]:
            return None
        try:
            from core.utils.date_utils import DateUtils

            start = DateUtils().get_n_trade_dates_before(max(int(lookback_days), 120), str(previous_trade_date))
        except Exception:
            end = datetime.strptime(str(previous_trade_date), "%Y%m%d")
            start = (end - timedelta(days=max(int(lookback_days * 1.5), 180))).strftime("%Y%m%d")
        return self.train_and_publish(
            start, str(previous_trade_date), profile=profile, effective_date=str(trade_date)
        )

    def _rolling_month_gate(
        self, frame: pd.DataFrame, prior: Mapping[str, float], months: int = 3,
    ) -> Dict[str, Any]:
        """Require positive OOS rank IC in at least two of three consecutive months."""
        data = frame.copy()
        data["_month"] = data["trade_date"].astype(str).str.slice(0, 6)
        month_keys = sorted(data["_month"].dropna().unique())[-max(int(months), 1):]
        folds = []
        for month in month_keys:
            validation = data[data["_month"] == month]
            train = data[data["_month"] < month]
            if train["trade_date"].astype(str).nunique() < 40 or validation.empty:
                continue
            metrics = self.factor_metrics(train, list(prior))
            redundant = self._redundant_factors(train, list(prior), metrics)
            learned = self._learned_weights(metrics, redundant)
            weights = self.blend_weights(prior, learned, 0.50, 0.25)
            evaluation = self.evaluate_weights(validation, weights)
            folds.append({"month": str(month), **evaluation})
        positive = sum(float(row.get("rank_ic") or 0.0) > 0 for row in folds)
        return {
            "passed": len(folds) >= 3 and positive >= 2,
            "required_folds": 3,
            "positive_required": 2,
            "positive_folds": int(positive),
            "folds": folds,
        }


__all__ = ["DynamicWeightRepository", "FactorLibraryTrainer", "WeightArtifact"]
