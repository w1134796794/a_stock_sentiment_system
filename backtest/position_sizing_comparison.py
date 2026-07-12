"""Compare fixed-risk sizing with conservative quarter-Kelly sizing."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine


SIZING_LABELS = {
    "fixed_risk": "固定账户风险",
    "conservative_kelly": "1/4保守凯利",
}


def summarize_sizing_result(mode: str, result: Dict[str, Any]) -> Dict[str, Any]:
    closed = [
        trade for trade in (result.get("trade_history") or [])
        if str(getattr(trade, "action", "")).upper().startswith("SELL")
    ]
    returns = pd.Series([float(getattr(row, "pnl_pct", 0.0)) for row in closed], dtype=float)
    rejected = sum(
        1 for row in (result.get("entry_attempts") or [])
        if row.get("status") == "sizing_rejected"
    )
    return {
        "position_sizing_mode": mode,
        "position_sizing_label": SIZING_LABELS.get(mode, mode),
        "closed_trades": len(closed),
        "win_rate": float(result.get("win_rate") or 0.0),
        "average_return": float(returns.mean()) if not returns.empty else 0.0,
        "total_return": float(result.get("total_return") or 0.0),
        "max_drawdown": float(result.get("max_drawdown") or 0.0),
        "stop_rate": sum(bool(getattr(row, "stop_loss_triggered", False)) for row in closed) / len(closed) if closed else 0.0,
        "rejected_orders": rejected,
    }


def run_position_sizing_comparison(
    *, data_manager: Any, base_config: BacktestConfig,
    start_date: str, end_date: str, trade_plans_dir: Path,
) -> Dict[str, Any]:
    results: Dict[str, Dict[str, Any]] = {}
    engines: Dict[str, BacktestEngine] = {}
    rows = []
    for mode in SIZING_LABELS:
        engine = BacktestEngine(data_manager, replace(base_config, position_sizing_mode=mode))
        result = engine.run_backtest(start_date, end_date, str(trade_plans_dir))
        engines[mode] = engine
        results[mode] = result
        rows.append(summarize_sizing_result(mode, result))
    return {
        "rows": rows,
        "results": results,
        "primary": results["fixed_risk"],
        "primary_engine": engines["fixed_risk"],
    }


def save_position_sizing_comparison(comparison: Dict[str, Any], output_dir: Path, run_id: str) -> Path:
    directory = Path(output_dir) / "backtest_results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"backtest_position_sizing_{run_id}.csv"
    pd.DataFrame(comparison.get("rows") or []).to_csv(path, index=False, encoding="utf-8-sig")
    return path


__all__ = [
    "SIZING_LABELS", "run_position_sizing_comparison",
    "save_position_sizing_comparison", "summarize_sizing_result",
]
