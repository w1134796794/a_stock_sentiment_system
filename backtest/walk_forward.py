"""Walk-forward validation for entry-rule profiles.

Each fold selects a pre-declared profile on the training window, then evaluates it
on the following unseen dates. Validation rows never participate in profile choice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd


@dataclass(frozen=True)
class EntryProfile:
    name: str
    min_market: float
    max_gap: float
    amount_low: float
    amount_high: float
    max_rank: int


PROFILES = (
    EntryProfile("均衡Top1", 50.0, 0.03, 0.8, 1.5, 1),
    EntryProfile("强市Top3", 70.0, 0.03, 0.8, 1.8, 3),
    EntryProfile("强市低吸Top1", 70.0, 0.02, 0.8, 1.5, 1),
    EntryProfile("中性宽量Top1", 50.0, 0.03, 0.7, 2.0, 1),
)


def _closed_frame(trades: Iterable[Any]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for trade in trades or []:
        action = str(getattr(trade, "action", "") or "").upper()
        if not action.startswith("SELL"):
            continue
        rows.append({
            "entry_date": str(getattr(trade, "entry_date", "") or getattr(trade, "date", "")),
            "pnl": float(getattr(trade, "pnl", 0.0) or 0.0),
            "pnl_pct": float(getattr(trade, "pnl_pct", 0.0) or 0.0),
            "stop_loss": bool(getattr(trade, "stop_loss_triggered", False)),
            "market_score": float(getattr(trade, "market_score", 0.0) or 0.0),
            "open_gap_pct": float(getattr(trade, "open_gap_pct", 0.0) or 0.0),
            "amount_ratio": float(getattr(trade, "amount_ratio", 0.0) or 0.0),
            "plan_rank": int(getattr(trade, "plan_rank", 0) or 0),
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    return frame.sort_values("entry_date").reset_index(drop=True)


def _select(frame: pd.DataFrame, profile: EntryProfile) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[
        (frame["market_score"] >= profile.min_market)
        & (frame["open_gap_pct"] > 0)
        & (frame["open_gap_pct"] <= profile.max_gap)
        & frame["amount_ratio"].between(profile.amount_low, profile.amount_high)
        & (frame["plan_rank"] > 0)
        & (frame["plan_rank"] <= profile.max_rank)
    ]


def _metrics(frame: pd.DataFrame) -> Dict[str, float]:
    if frame.empty:
        return {"samples": 0, "win_rate": 0.0, "avg_return": 0.0, "total_pnl": 0.0, "stop_rate": 0.0}
    return {
        "samples": int(len(frame)),
        "win_rate": float((frame["pnl"] > 0).mean()),
        "avg_return": float(frame["pnl_pct"].mean()),
        "total_pnl": float(frame["pnl"].sum()),
        "stop_rate": float(frame["stop_loss"].mean()),
    }


def _objective(frame: pd.DataFrame, min_samples: int) -> float:
    if len(frame) < min_samples:
        return float("-inf")
    downside = frame.loc[frame["pnl_pct"] < 0, "pnl_pct"].std()
    downside = float(downside) if pd.notna(downside) else 0.0
    return float(frame["pnl_pct"].mean() - 0.25 * downside - 0.02 * frame["stop_loss"].mean())


def build_walk_forward_frames(
    result: Dict[str, Any], *, train_days: int = 30, validation_days: int = 10,
    min_train_samples: int = 8,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return fold detail and one-row out-of-sample summary frames."""
    frame = _closed_frame(result.get("trade_history") or [])
    required = {"market_score", "open_gap_pct", "amount_ratio", "plan_rank"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(), pd.DataFrame()
    usable = frame[
        (frame["market_score"] > 0)
        & (frame["open_gap_pct"] > 0)
        & (frame["amount_ratio"] > 0)
    ].copy()
    dates = sorted(usable["entry_date"].dropna().astype(str).unique())
    if len(dates) <= train_days:
        train_days = max(8, int(len(dates) * 0.6))
        validation_days = max(3, int(len(dates) * 0.25))
    if len(dates) <= train_days:
        return pd.DataFrame(), pd.DataFrame()

    folds = []
    validation_parts = []
    start = train_days
    fold_no = 1
    while start < len(dates):
        train_dates = dates[max(0, start - train_days):start]
        valid_dates = dates[start:min(start + validation_days, len(dates))]
        if not valid_dates:
            break
        train = usable[usable["entry_date"].isin(train_dates)]
        valid = usable[usable["entry_date"].isin(valid_dates)]
        selected_train = [(profile, _select(train, profile)) for profile in PROFILES]
        max_available = max((len(selected) for _, selected in selected_train), default=0)
        fold_min_samples = min(min_train_samples, max(3, max_available))
        scored = [
            (_objective(selected, fold_min_samples), profile)
            for profile, selected in selected_train
        ]
        scored.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        best_score, best = scored[0]
        if best_score == float("-inf"):
            start += validation_days
            continue
        train_selected = _select(train, best)
        valid_selected = _select(valid, best)
        train_metrics = _metrics(train_selected)
        valid_metrics = _metrics(valid_selected)
        folds.append({
            "fold": fold_no,
            "train_start": train_dates[0],
            "train_end": train_dates[-1],
            "validation_start": valid_dates[0],
            "validation_end": valid_dates[-1],
            "selected_profile": best.name,
            "min_market": best.min_market,
            "max_gap": best.max_gap,
            "amount_low": best.amount_low,
            "amount_high": best.amount_high,
            "max_rank": best.max_rank,
            **{f"train_{key}": value for key, value in train_metrics.items()},
            **{f"validation_{key}": value for key, value in valid_metrics.items()},
        })
        if not valid_selected.empty:
            selected = valid_selected.copy()
            selected["fold"] = fold_no
            validation_parts.append(selected)
        start += validation_days
        fold_no += 1

    fold_frame = pd.DataFrame(folds)
    if fold_frame.empty:
        return fold_frame, pd.DataFrame()
    oos = pd.concat(validation_parts, ignore_index=True) if validation_parts else pd.DataFrame()
    summary = pd.DataFrame([{
        "folds": len(fold_frame),
        "train_days": train_days,
        "validation_days": validation_days,
        **{f"oos_{key}": value for key, value in _metrics(oos).items()},
    }])
    return fold_frame, summary


__all__ = ["EntryProfile", "PROFILES", "build_walk_forward_frames"]
