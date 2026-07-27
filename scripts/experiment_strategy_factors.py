"""Run reproducible, out-of-sample strategy-factor experiments.

The command deliberately keeps experimental plans outside the production
screening directories.  Models are fitted only with confirmed minute-entry
samples whose plan date is no later than ``--train-end``; later dates are
used solely for replay validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor, VotingRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_STRATEGIES = (
    "first_board_launch",
    "mainline_leader",
    "weak_to_strong",
    "momentum_repair",
    "capital_resonance",
    "ultra_short_board",
)
ENTRY_MODES = {
    "first_board_launch": ["weak_to_strong", "continuation"],
    "mainline_leader": ["weak_to_strong", "continuation", "acceleration"],
    "weak_to_strong": ["weak_to_strong"],
    "momentum_repair": ["weak_to_strong", "continuation"],
    "capital_resonance": ["weak_to_strong", "continuation"],
    "ultra_short_board": ["continuation", "acceleration"],
}
STRATEGY_CORE_FEATURES = {
    "first_board_launch": (
        "board_height_score", "behavior_repair_score", "crowding_decay_5d_score",
        "seal_time_score", "behavior_acceleration_score", "sector_lhb_resonance_score",
        "attention_score", "float_mv_fit_score", "sector_rotation_momentum_score",
        "intraday_seal_quality_score", "sector_mainline_score", "behavior_decay_score",
    ),
    "mainline_leader": (
        "sector_heat_score", "seal_time_score", "crowding_penalty_score",
        "institution_consensus_score", "sector_lhb_resonance_score",
        "capital_flow_consensus_score", "intraday_seal_quality_score",
        "behavior_attention_score", "sector_rotation_momentum_score",
        "sector_mainline_score", "behavior_repair_score", "behavior_decay_score",
    ),
    "weak_to_strong": (
        "sector_resonance_score", "tech_score", "seal_time_score",
        "sector_mainline_score", "behavior_repair_score", "liquidity_score",
        "float_mv_fit_score", "sector_rotation_momentum_score", "board_height_score",
        "behavior_attention_score", "behavior_acceleration_score", "behavior_decay_score",
    ),
    "momentum_repair": (
        "institution_consensus_score", "lhb_net_buy_score", "crowding_decay_5d_score",
        "sector_mainline_score", "behavior_acceleration_score",
        "sector_rotation_momentum_score", "sector_lhb_resonance_score",
        "intraday_seal_quality_score", "float_mv_fit_score",
        "relative_strength_sector_score", "tech_score", "liquidity_score",
    ),
    "capital_resonance": (
        "crowding_decay_5d_score", "behavior_decay_score", "behavior_repair_score",
        "margin_score", "float_mv_fit_score", "sector_persistence_score",
        "sector_mainline_score", "behavior_attention_score",
        "behavior_divergence_score", "attention_score", "liquidity_score",
        "behavior_acceleration_score",
    ),
    "ultra_short_board": (
        "tech_score", "behavior_acceleration_score", "float_mv_fit_score",
        "sector_lhb_resonance_score", "seal_time_score", "sector_resonance_score",
        "intraday_seal_quality_score", "behavior_decay_score",
        "sector_rotation_momentum_score", "lhb_net_buy_score",
        "behavior_attention_score", "sector_persistence_score",
    ),
}
FEATURES = (
    "tech_score",
    "liquidity_score",
    "sector_heat_score",
    "sector_persistence_score",
    "sector_mainline_score",
    "sector_resonance_score",
    "board_height_score",
    "seal_time_score",
    "float_mv_fit_score",
    "lhb_net_buy_score",
    "institution_net_buy_score",
    "institution_consensus_score",
    "sector_lhb_resonance_score",
    "crowding_penalty_score",
    "capital_flow_consensus_score",
    "capital_flow_persistence_score",
    "attention_score",
    "leader_quality_score",
    "margin_score",
    "event_risk_score",
    "sector_rotation_momentum_score",
    "crowding_decay_5d_score",
    "relative_strength_sector_score",
    "intraday_seal_quality_score",
    "behavior_attention_score",
    "behavior_acceleration_score",
    "behavior_divergence_score",
    "behavior_repair_score",
    "behavior_decay_score",
)

METRIC_TO_FACTOR = {
    "tech_score": "tech_score",
    "liquidity_score": "stk_liquidity_percentile",
    "sector_persistence_score": "stk_sector_persistence_score",
    "sector_mainline_score": "stk_sector_mainline_score",
    "sector_resonance_score": "stk_sector_resonance_score",
    "board_height_score": "stk_board_position",
    "lhb_net_buy_score": "stk_lhb_net_buy_score",
    "institution_net_buy_score": "stk_lhb_institution_score",
    "institution_consensus_score": "stk_lhb_institution_consensus",
    "sector_lhb_resonance_score": "stk_lhb_sector_resonance",
    "capital_flow_consensus_score": "stk_capital_flow_consensus",
    "capital_flow_persistence_score": "stk_capital_flow_persistence",
    "attention_score": "stk_attention_consensus",
    "leader_quality_score": "stk_kpl_leader_quality",
    "margin_score": "stk_margin_acceleration",
    "sector_rotation_momentum_score": "stk_sector_rotation_momentum",
    "crowding_decay_5d_score": "stk_crowding_decay_5d",
    "relative_strength_sector_score": "stk_relative_strength_sector",
    "intraday_seal_quality_score": "stk_intraday_seal_quality",
    "behavior_attention_score": "stk_behavior_attention",
    "behavior_acceleration_score": "stk_behavior_acceleration",
    "behavior_divergence_score": "stk_behavior_divergence",
    "behavior_repair_score": "stk_behavior_repair",
    "behavior_decay_score": "stk_behavior_decay",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _candidate_feature(candidate: dict[str, Any], feature: str) -> float:
    metrics = candidate.get("metrics") or {}
    context = candidate.get("context") or {}
    metric_name = METRIC_TO_FACTOR.get(feature, feature)
    if metric_name in metrics:
        return _float(metrics.get(metric_name), np.nan)
    return _float(context.get(feature), np.nan)


def _load_training_frame(
    db_path: Path, train_end: str, strategies: tuple[str, ...],
) -> pd.DataFrame:
    columns = ",".join(f'f."{name}"' for name in FEATURES)
    with duckdb.connect(str(db_path), read_only=True) as con:
        frame = con.execute(
            f"""
            SELECT o.profile, o.entry_signal, o.trade_date, o.code,
                   o.raw_forward_return AS target_return, {columns}
            FROM signal_outcome_wide o
            JOIN factor_stock_wide f USING (trade_date, code)
            WHERE o.profile IN ({','.join('?' for _ in strategies)})
              AND o.label_source = 'confirmed_minute_next_open'
              AND o.trade_date <= ?
            """,
            [*strategies, str(train_end)],
        ).df()
    frame["target_return"] = pd.to_numeric(frame["target_return"], errors="coerce")
    frame = frame.dropna(subset=["target_return"])
    frame["target_return"] = frame["target_return"].clip(-0.15, 0.20)
    for feature in FEATURES:
        frame[feature] = pd.to_numeric(frame[feature], errors="coerce")
    return frame


def _fit_models(
    frame: pd.DataFrame, algorithm: str, strategies: tuple[str, ...], feature_set: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    models: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    for strategy_id in strategies:
        sample = frame[frame["profile"] == strategy_id].copy()
        if len(sample) < 20:
            diagnostics[strategy_id] = {"samples": len(sample), "status": "insufficient"}
            continue
        extra_trees = ExtraTreesRegressor(
                n_estimators=300, max_depth=5, min_samples_leaf=8,
                max_features=0.75, random_state=20260720, n_jobs=-1,
            )
        random_forest = RandomForestRegressor(
                n_estimators=300, max_depth=5, min_samples_leaf=8,
                max_features=0.75, random_state=20260720, n_jobs=-1,
            )
        estimator = (
            extra_trees if algorithm == "extra_trees"
            else random_forest if algorithm == "random_forest"
            else VotingRegressor([
                ("extra_trees", extra_trees),
                ("random_forest", random_forest),
            ])
        )
        model_features = (
            tuple(STRATEGY_CORE_FEATURES.get(strategy_id) or FEATURES)
            if feature_set == "strategy_core" else FEATURES
        )
        model = make_pipeline(SimpleImputer(strategy="median"), estimator)
        model.fit(sample[list(model_features)], sample["target_return"])
        models[strategy_id] = {"pipeline": model, "features": model_features}
        predictions = model.predict(sample[list(model_features)])
        fitted_estimator = model.named_steps[estimator.__class__.__name__.lower()]
        if isinstance(fitted_estimator, VotingRegressor):
            importances = np.mean(
                [item.feature_importances_ for item in fitted_estimator.estimators_], axis=0,
            )
        else:
            importances = fitted_estimator.feature_importances_
        feature_importance = sorted(
            zip(model_features, importances),
            key=lambda item: item[1], reverse=True,
        )
        diagnostics[strategy_id] = {
            "samples": len(sample),
            "status": "fitted",
            "target_mean_pct": round(float(sample["target_return"].mean()) * 100, 3),
            "prediction_mean_pct": round(float(np.mean(predictions)) * 100, 3),
            "prediction_std_pct": round(float(np.std(predictions)) * 100, 3),
            "feature_importance": [
                {"factor": factor, "importance": round(float(importance), 6)}
                for factor, importance in feature_importance[:12]
            ],
        }
    return models, diagnostics


def _date_range(start: str, end: str) -> Iterable[str]:
    for value in pd.date_range(pd.to_datetime(start), pd.to_datetime(end), freq="D"):
        yield value.strftime("%Y%m%d")


def _load_candidates(
    screening_dir: Path, strategy_id: str, trade_date: str, *, scope: str = "final",
) -> list[dict[str, Any]]:
    path = screening_dir / "combinations" / strategy_id / f"screening_{trade_date}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    source_key = "candidate_pool" if scope == "pool" else "final"
    return [dict(row) for row in (payload.get(source_key) or []) if isinstance(row, dict)]


def _passes_focus_rule(strategy_id: str, candidate: dict[str, Any]) -> bool:
    metrics = candidate.get("metrics") or {}
    context = candidate.get("context") or {}
    if strategy_id == "first_board_launch":
        return (
            _float(metrics.get("stk_behavior_acceleration"), 100.0) <= 66.8
            and _float(context.get("seal_time_score"), 0.0) >= 38.0
        )
    if strategy_id == "weak_to_strong":
        return (
            _float(metrics.get("stk_behavior_repair"), 0.0) >= 83.3
            and _float(metrics.get("tech_score"), 100.0) <= 89.1
        )
    if strategy_id == "mainline_leader":
        return (
            _float(context.get("sector_heat_score"), 100.0) <= 58.7
            and _float(metrics.get("stk_attention_consensus"), 0.0) >= 50.0
        )
    return True


def _plan_row(candidate: dict[str, Any], priority: int, predicted_return: float,
              allowed_modes: list[str], position_cap: float,
              strategy_max_positions: int) -> dict[str, Any]:
    metrics = candidate.get("metrics") or {}
    context = candidate.get("context") or {}
    execution = deepcopy(candidate.get("strategy_execution") or {})
    execution["allowed_entry_modes"] = list(allowed_modes)
    execution["max_positions"] = max(int(strategy_max_positions), 1)
    strategy_id = str(candidate.get("strategy_id") or "default")
    score = float(np.clip(80.0 + predicted_return * 300.0, 80.0, 99.0))
    position_cap = position_cap or _float(candidate.get("position_cap_pct"), 20.0)
    row: dict[str, Any] = {
        "模式": f"指标筛选/{strategy_id}",
        "代码": str(candidate.get("code") or "").zfill(6),
        "名称": str(candidate.get("name") or ""),
        "动作": "买入",
        "介入时机": "按策略分钟条件确认",
        "目标价": 0.0,
        "止损价": 0.0,
        "止盈价": 0.0,
        "仓位": "heavy",
        "计划基础仓位%": position_cap,
        "前置条件": "仅使用训练截止日以前的真实成交样本排序，次日按分钟确认",
        "取消条件": "未确认、封板不可成交或数据不足则取消",
        "置信度": round(score / 100.0, 4),
        "理由": f"样本外实验预测未来收益 {predicted_return * 100:+.2f}%",
        "加入观察池": True,
        "热点共振": False,
        "共振板块": str(candidate.get("resonance_sectors") or ""),
        "综合评分": round(score, 4),
        "优先级": priority,
        "所属板块": str(candidate.get("resonance_sectors") or ""),
        "惩罚理由": "; ".join(candidate.get("penalty_reasons") or []),
        "因子指标": json.dumps(metrics, ensure_ascii=False),
        "原始指标": json.dumps(context, ensure_ascii=False),
        "策略ID": strategy_id,
        "策略名称": str(candidate.get("strategy_name") or strategy_id),
        "策略版本": f"oos_factor_experiment_{strategy_id}",
        "策略执行": json.dumps(execution, ensure_ascii=False),
        "策略单票仓位上限%": position_cap,
        "策略来源": strategy_id,
        "组合建议仓位%": position_cap,
    }
    for name, value in metrics.items():
        row[f"因子_{name}"] = value
    for name, value in context.items():
        row[f"原始_{name}"] = value
    return row


def _generate_plans(*, models: dict[str, Any], screening_dir: Path, output_dir: Path,
                     start: str, end: str, variant: str, top_n: int,
                    min_prediction: float, strategies: tuple[str, ...],
                    position_cap: float, strategy_max_positions: int,
                    candidate_scope: str, entry_policy: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_plan in output_dir.glob("交易计划_*.csv"):
        stale_plan.unlink()
    selected = 0
    selected_dates = 0
    strategy_counts = {key: 0 for key in strategies}
    for trade_date in _date_range(start, end):
        rows: list[dict[str, Any]] = []
        for strategy_id in strategies:
            if strategy_id not in models:
                continue
            if variant == "first_continuation" and strategy_id != "first_board_launch":
                continue
            candidates = _load_candidates(
                screening_dir, strategy_id, trade_date, scope=candidate_scope,
            )
            if variant == "focus_rules":
                candidates = [row for row in candidates if _passes_focus_rule(strategy_id, row)]
            if not candidates:
                continue
            model_spec = models[strategy_id]
            model_features = tuple(model_spec["features"])
            matrix = pd.DataFrame([
                [_candidate_feature(row, feature) for feature in model_features]
                for row in candidates
            ], columns=model_features)
            predictions = model_spec["pipeline"].predict(matrix)
            ranked = sorted(zip(candidates, predictions), key=lambda item: item[1], reverse=True)
            accepted = [(row, float(pred)) for row, pred in ranked if pred >= min_prediction][:top_n]
            for priority, (candidate, prediction) in enumerate(accepted, start=1):
                allowed = list(ENTRY_MODES.get(strategy_id) or ["weak_to_strong"])
                if entry_policy == "adaptive_all":
                    allowed = ["weak_to_strong", "continuation", "acceleration"]
                if variant == "first_continuation":
                    allowed = ["continuation"]
                rows.append(_plan_row(
                    candidate, priority, prediction, allowed,
                    position_cap, strategy_max_positions,
                ))
                strategy_counts[strategy_id] += 1
        if rows:
            rows.sort(key=lambda row: (row["综合评分"], -row["优先级"]), reverse=True)
            pd.DataFrame(rows).to_csv(output_dir / f"交易计划_{trade_date}.csv", index=False, encoding="utf-8-sig")
            selected += len(rows)
            selected_dates += 1
    return {"plans": selected, "dates": selected_dates, "strategy_counts": strategy_counts}


def _run_backtest(
    plan_dir: Path, start: str, end: str, *, aggressive: bool,
    risk_per_trade: float, max_position: float, max_total_position: float,
    max_sector_concentration: float,
) -> dict[str, Any]:
    from backtest.backtest_engine import BacktestConfig, BacktestEngine
    from config.settings import CACHE_DIR, TUSHARE_TOKEN
    from core.data.data_manager_main import DataManager
    from risk.risk_config import RiskConfig

    config = BacktestConfig.from_risk_config(RiskConfig.load(), initial_capital=100_000.0)
    if aggressive:
        config.fixed_risk_per_trade = 0.015
        config.max_position_per_stock = 0.35
        config.max_total_position = 0.90
        config.max_positions = 3
        config.max_sector_concentration = 0.70
    if risk_per_trade > 0:
        config.fixed_risk_per_trade = float(risk_per_trade)
    if max_position > 0:
        config.max_position_per_stock = float(max_position)
        config.kelly_max_position = float(max_position)
    if max_total_position > 0:
        config.max_total_position = float(max_total_position)
    if max_sector_concentration > 0:
        config.max_sector_concentration = float(max_sector_concentration)
    result = BacktestEngine(
        DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False), config,
    ).run_backtest(start, end, str(plan_dir))
    keys = (
        "total_return", "max_drawdown", "win_rate", "profit_loss_ratio",
        "closed_trades", "open_positions", "final_capital", "entry_candidate_count",
    )
    summary = {key: result.get(key) for key in keys}
    summary["aggressive"] = aggressive
    summary["entry_funnel"] = result.get("entry_funnel") or result.get("candidate_funnel") or {}
    summary["entry_opportunity_summary"] = result.get("entry_opportunity_summary") or {}
    summary["trade_history"] = [
        asdict(item) if is_dataclass(item) else dict(item)
        for item in (result.get("trade_history") or [])
    ]
    return summary


def _promotion_check(backtest: dict[str, Any], *, target_return: float = 0.20) -> dict[str, Any]:
    """Keep attractive but statistically thin runs out of production."""
    closed_trades = int(backtest.get("closed_trades") or 0)
    total_return = _float(backtest.get("total_return"))
    max_drawdown = _float(backtest.get("max_drawdown"))
    win_rate = _float(backtest.get("win_rate"))
    checks = {
        "target_return": total_return >= target_return,
        "minimum_trades": closed_trades >= 20,
        "drawdown_limit": max_drawdown >= -0.15,
        "minimum_win_rate": win_rate >= 0.45,
    }
    return {
        "publishable": all(checks.values()),
        "status": "publishable" if all(checks.values()) else "challenge_only",
        "checks": checks,
        "thresholds": {
            "target_return": target_return,
            "minimum_trades": 20,
            "maximum_drawdown": -0.15,
            "minimum_win_rate": 0.45,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-end", default="20260531")
    parser.add_argument("--start", default="20260601")
    parser.add_argument("--end", default="20260720")
    parser.add_argument(
        "--algorithm", choices=("random_forest", "extra_trees", "tree_ensemble"),
        default="extra_trees",
    )
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    parser.add_argument("--feature-set", choices=("all", "strategy_core"), default="all")
    parser.add_argument("--candidate-scope", choices=("final", "pool"), default="final")
    parser.add_argument(
        "--entry-policy", choices=("strategy", "adaptive_all"), default="strategy",
        help="strategy keeps each template's modes; adaptive_all lets the opening shape select the mode",
    )
    parser.add_argument("--variants", default="model_top1,first_continuation,focus_rules")
    parser.add_argument("--top-n", type=int, default=1)
    parser.add_argument("--min-prediction", type=float, default=0.003)
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--position-cap", type=float, default=0.0, help="Plan cap in percent; 0 keeps strategy cap")
    parser.add_argument("--strategy-max-positions", type=int, default=1)
    parser.add_argument("--risk-per-trade", type=float, default=0.0)
    parser.add_argument("--max-position", type=float, default=0.0)
    parser.add_argument("--max-total-position", type=float, default=0.0)
    parser.add_argument("--max-sector-concentration", type=float, default=0.0)
    parser.add_argument("--skip-backtest", action="store_true")
    args = parser.parse_args()

    from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

    experiment_root = Path(WEB_DATA_DIR) / "experiments" / "strategy_factor_oos"
    experiment_root.mkdir(parents=True, exist_ok=True)
    strategies = tuple(
        item.strip() for item in str(args.strategies).split(",")
        if item.strip() in ENTRY_MODES
    )
    if not strategies:
        raise SystemExit("No valid strategies selected")
    training = _load_training_frame(Path(FACTOR_DB_PATH), args.train_end, strategies)
    models, diagnostics = _fit_models(training, args.algorithm, strategies, args.feature_set)
    report: dict[str, Any] = {
        "contract": {
            "train_end": args.train_end,
            "validation_start": args.start,
            "validation_end": args.end,
            "label": "confirmed_minute_next_open/raw_forward_return",
            "algorithm": args.algorithm,
            "target_clipping": [-0.15, 0.20],
            "min_prediction": args.min_prediction,
            "top_n_per_strategy_day": args.top_n,
            "strategies": list(strategies),
            "feature_set": args.feature_set,
            "candidate_scope": args.candidate_scope,
            "entry_policy": args.entry_policy,
            "position_cap_pct": args.position_cap,
            "strategy_max_positions": args.strategy_max_positions,
            "risk_per_trade": args.risk_per_trade,
            "max_position": args.max_position,
            "max_total_position": args.max_total_position,
            "max_sector_concentration": args.max_sector_concentration,
        },
        "training": diagnostics,
        "variants": [],
    }
    screening_dir = Path(WEB_DATA_DIR) / "screening"
    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    strategy_tag = "-".join(item[:4] for item in strategies)
    for variant in variants:
        run_tag = (
            f"{args.algorithm}_{args.feature_set}_{args.candidate_scope}_{args.entry_policy}_{strategy_tag}_{variant}_top{args.top_n}_"
            f"min{args.min_prediction:.4f}_{'aggressive' if args.aggressive else 'standard'}_"
            f"cap{args.position_cap:.0f}_risk{args.risk_per_trade:.3f}_"
            f"{args.start}_{args.end}"
        )
        plan_dir = experiment_root / run_tag
        generation = _generate_plans(
            models=models, screening_dir=screening_dir, output_dir=plan_dir,
            start=args.start, end=args.end, variant=variant, top_n=args.top_n,
            min_prediction=args.min_prediction, strategies=strategies,
            position_cap=args.position_cap,
            strategy_max_positions=args.strategy_max_positions,
            candidate_scope=args.candidate_scope,
            entry_policy=args.entry_policy,
        )
        row: dict[str, Any] = {"variant": variant, "plan_dir": str(plan_dir), **generation}
        if not args.skip_backtest:
            row["backtest"] = _run_backtest(
                plan_dir, args.start, args.end, aggressive=args.aggressive,
                risk_per_trade=args.risk_per_trade, max_position=args.max_position,
                max_total_position=args.max_total_position,
                max_sector_concentration=args.max_sector_concentration,
            )
            row["promotion"] = _promotion_check(row["backtest"])
        report["variants"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    output = experiment_root / (
        f"report_{args.algorithm}_{args.feature_set}_{args.candidate_scope}_{args.entry_policy}_{strategy_tag}_top{args.top_n}_min{args.min_prediction:.4f}_"
        f"{'aggressive' if args.aggressive else 'standard'}_"
        f"cap{args.position_cap:.0f}_risk{args.risk_per_trade:.3f}_{args.start}_{args.end}.json"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
