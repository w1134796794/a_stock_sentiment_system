"""Select strategy factor models on an inner validation month.

The tuner keeps the final replay period untouched.  It trains with data no
later than ``--train-end`` and uses only ``--validation-start`` through
``--validation-end`` to select the estimator, feature set, threshold and
strategy subset.  The selected contract can then be refitted through the end
of validation and replayed on a later period with
``experiment_strategy_factors.py``.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from experiment_strategy_factors import (
    DEFAULT_STRATEGIES,
    _candidate_feature,
    _date_range,
    _fit_models,
    _load_candidates,
    _load_training_frame,
)


def _load_outcomes(
    db_path: Path, start: str, end: str, strategies: tuple[str, ...],
) -> dict[tuple[str, str, str], float]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        frame = con.execute(
            f"""
            SELECT profile, trade_date, code, raw_forward_return
            FROM signal_outcome_wide
            WHERE label_source = 'confirmed_minute_next_open'
              AND trade_date BETWEEN ? AND ?
              AND profile IN ({','.join('?' for _ in strategies)})
            """,
            [start, end, *strategies],
        ).df()
    return {
        (str(row.profile), str(row.trade_date), str(row.code).zfill(6)): float(row.raw_forward_return)
        for row in frame.itertuples(index=False)
        if pd.notna(row.raw_forward_return)
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    confirmed = [row for row in rows if row.get("return") is not None]
    values = np.asarray([float(row["return"]) for row in confirmed], dtype=float)
    by_date: dict[str, list[float]] = defaultdict(list)
    for row in confirmed:
        by_date[str(row["trade_date"])].append(float(row["return"]))
    daily = np.asarray([np.mean(by_date[key]) for key in sorted(by_date)], dtype=float)
    curve = np.cumprod(1.0 + daily) if len(daily) else np.asarray([], dtype=float)
    peaks = np.maximum.accumulate(np.r_[1.0, curve])
    drawdowns = np.r_[1.0, curve] / peaks - 1.0
    gains = float(values[values > 0].sum()) if len(values) else 0.0
    losses = abs(float(values[values < 0].sum())) if len(values) else 0.0
    total_return = float(curve[-1] - 1.0) if len(curve) else 0.0
    max_drawdown = float(drawdowns.min()) if len(drawdowns) else 0.0
    return {
        "selected": len(rows),
        "confirmed": len(confirmed),
        "confirmed_dates": len(by_date),
        "confirmation_rate": round(len(confirmed) / len(rows), 4) if rows else 0.0,
        "win_rate": round(float((values > 0).mean()), 4) if len(values) else 0.0,
        "mean_return": round(float(values.mean()), 6) if len(values) else 0.0,
        "total_return": round(total_return, 6),
        "max_drawdown": round(max_drawdown, 6),
        "profit_factor": round(gains / losses, 4) if losses else (999.0 if gains else 0.0),
    }


def _rank_candidates(
    *, models: dict[str, Any], screening_dir: Path, start: str, end: str,
    strategies: tuple[str, ...],
) -> dict[tuple[str, str], list[tuple[dict[str, Any], float]]]:
    ranked_cache: dict[tuple[str, str], list[tuple[dict[str, Any], float]]] = {}
    for trade_date in _date_range(start, end):
        for strategy_id in strategies:
            spec = models.get(strategy_id)
            if not spec:
                continue
            candidates = _load_candidates(screening_dir, strategy_id, trade_date, scope="final")
            if not candidates:
                continue
            features = tuple(spec["features"])
            matrix = pd.DataFrame(
                [[_candidate_feature(row, feature) for feature in features] for row in candidates],
                columns=features,
            )
            predictions = spec["pipeline"].predict(matrix)
            ranked_cache[(trade_date, strategy_id)] = sorted(
                zip(candidates, predictions), key=lambda item: item[1], reverse=True,
            )
    return ranked_cache


def _score_ranked(
    *, ranked_cache: dict[tuple[str, str], list[tuple[dict[str, Any], float]]],
    outcomes: dict[tuple[str, str, str], float], strategies: tuple[str, ...],
    threshold: float, top_n: int,
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (trade_date, strategy_id), ranked in ranked_cache.items():
        if strategy_id in strategies:
            accepted = [(row, float(pred)) for row, pred in ranked if pred >= threshold][:top_n]
            for candidate, prediction in accepted:
                code = str(candidate.get("code") or "").zfill(6)
                row = {
                    "strategy_id": strategy_id,
                    "trade_date": trade_date,
                    "code": code,
                    "prediction": prediction,
                    "return": outcomes.get((strategy_id, trade_date, code)),
                }
                selected.append(row)
                by_strategy[strategy_id].append(row)
    overall = _metrics(selected)
    # Penalise drawdown and tiny samples.  This is an inner-validation proxy,
    # not the final production backtest objective.
    sample_penalty = max(8 - int(overall["confirmed"]), 0) * 0.01
    objective = float(overall["total_return"]) + 0.5 * float(overall["max_drawdown"]) - sample_penalty
    return {
        **overall,
        "objective": round(objective, 6),
        "by_strategy": {key: _metrics(value) for key, value in sorted(by_strategy.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-end", default="20260430")
    parser.add_argument("--validation-start", default="20260501")
    parser.add_argument("--validation-end", default="20260531")
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    parser.add_argument("--algorithms", default="extra_trees,random_forest")
    parser.add_argument("--feature-sets", default="all,strategy_core")
    parser.add_argument("--thresholds", default="0,0.003,0.006,0.01")
    parser.add_argument("--top-values", default="1,2")
    parser.add_argument("--minimum-confirmed", type=int, default=12)
    parser.add_argument("--minimum-confirmed-dates", type=int, default=8)
    args = parser.parse_args()

    from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

    strategies = tuple(item.strip() for item in args.strategies.split(",") if item.strip())
    algorithms = tuple(item.strip() for item in args.algorithms.split(",") if item.strip())
    feature_sets = tuple(item.strip() for item in args.feature_sets.split(",") if item.strip())
    thresholds = tuple(float(item.strip()) for item in args.thresholds.split(",") if item.strip())
    top_values = tuple(int(item.strip()) for item in args.top_values.split(",") if item.strip())
    db_path = Path(FACTOR_DB_PATH)
    screening_dir = Path(WEB_DATA_DIR) / "screening"
    training = _load_training_frame(db_path, args.train_end, strategies)
    outcomes = _load_outcomes(
        db_path, args.validation_start, args.validation_end, strategies,
    )
    trials: list[dict[str, Any]] = []
    for algorithm in algorithms:
        for feature_set in feature_sets:
            models, diagnostics = _fit_models(training, algorithm, strategies, feature_set)
            ranked_cache = _rank_candidates(
                models=models, screening_dir=screening_dir,
                start=args.validation_start, end=args.validation_end,
                strategies=strategies,
            )
            for threshold in thresholds:
                for top_n in top_values:
                    result = _score_ranked(
                        ranked_cache=ranked_cache, outcomes=outcomes,
                        strategies=strategies, threshold=threshold, top_n=top_n,
                    )
                    trials.append({
                        "algorithm": algorithm,
                        "feature_set": feature_set,
                        "threshold": threshold,
                        "top_n": top_n,
                        "training": diagnostics,
                        **result,
                    })
    eligible = [
        row for row in trials
        if row["confirmed"] >= args.minimum_confirmed
        and row["confirmed_dates"] >= args.minimum_confirmed_dates
    ]
    ranked = sorted(eligible or trials, key=lambda row: row["objective"], reverse=True)
    best = ranked[0]
    active_strategies = [
        strategy_id for strategy_id, metrics in best["by_strategy"].items()
        if metrics["confirmed"] >= 3 and metrics["total_return"] > 0
    ]
    report = {
        "contract": {
            "train_end": args.train_end,
            "validation_start": args.validation_start,
            "validation_end": args.validation_end,
            "label": "confirmed_minute_next_open/raw_forward_return",
            "final_period_used": False,
            "promotion_gate": {
                "minimum_confirmed": args.minimum_confirmed,
                "minimum_confirmed_dates": args.minimum_confirmed_dates,
            },
        },
        "best": {
            **best,
            "recommended_strategies": active_strategies or list(strategies),
            "publishable": bool(eligible),
            "status": "validation_passed" if eligible else "insufficient_validation_sample",
        },
        "leaderboard": ranked[:12],
    }
    output_dir = Path(WEB_DATA_DIR) / "experiments" / "strategy_factor_oos"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"tuning_{args.train_end}_{args.validation_start}_{args.validation_end}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "best": report["best"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
