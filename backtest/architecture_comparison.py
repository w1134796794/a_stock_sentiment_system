"""Compare the slim production rule chain with the legacy default plan chain."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine


def _closed_trades(result: Dict[str, Any]) -> List[Any]:
    return [
        trade
        for trade in (result.get("trade_history") or [])
        if str(getattr(trade, "action", "")).upper().startswith("SELL")
    ]


def summarize_architecture_result(label: str, result: Dict[str, Any]) -> Dict[str, Any]:
    closed = _closed_trades(result)
    profits = [float(getattr(trade, "pnl", 0.0) or 0.0) for trade in closed]
    gross_profit = sum(value for value in profits if value > 0)
    gross_loss = abs(sum(value for value in profits if value < 0))
    candidate_count = int(result.get("entry_candidate_count") or 0)
    buy_count = int(result.get("buy_trades") or 0)
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 1e-12
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    return {
        "architecture": label,
        "candidate_count": candidate_count,
        "buy_count": buy_count,
        "coverage_rate": buy_count / candidate_count if candidate_count else 0.0,
        "closed_trades": len(closed),
        "win_rate": float(result.get("win_rate") or 0.0),
        "profit_factor": profit_factor,
        "total_return": float(result.get("total_return") or 0.0),
        "max_drawdown": float(result.get("max_drawdown") or 0.0),
    }


def run_architecture_comparison(
    *,
    data_manager: Any,
    base_config: BacktestConfig,
    start_date: str,
    end_date: str,
    slim_result: Dict[str, Any],
    legacy_trade_plans_dir: Path,
) -> Dict[str, Any]:
    legacy_engine = BacktestEngine(data_manager, replace(base_config))
    legacy_result = legacy_engine.run_backtest(
        start_date, end_date, str(legacy_trade_plans_dir),
    )
    return {
        "rows": [
            summarize_architecture_result("旧默认链路", legacy_result),
            summarize_architecture_result("精简三策略规则链路", slim_result),
        ],
        "legacy_result": legacy_result,
    }


def save_architecture_comparison(
    comparison: Dict[str, Any], output_dir: Path, run_id: str,
) -> Path:
    directory = Path(output_dir) / "backtest_results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"backtest_architecture_{run_id}.csv"
    pd.DataFrame(comparison.get("rows") or []).to_csv(
        path, index=False, encoding="utf-8-sig",
    )
    return path


__all__ = [
    "run_architecture_comparison",
    "save_architecture_comparison",
    "summarize_architecture_result",
]
