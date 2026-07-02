"""Run and summarize the four entry-mode backtest groups."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import ENTRY_CONTINUATION, ENTRY_FIXED, ENTRY_HYBRID, ENTRY_WEAK


ENTRY_MODE_LABELS = {
    ENTRY_FIXED: "原固定开盘区间",
    ENTRY_WEAK: "只做弱转强",
    ENTRY_CONTINUATION: "只做强势延续",
    ENTRY_HYBRID: "弱转强+强势延续",
}


def summarize_entry_result(mode: str, result: Dict[str, Any]) -> Dict[str, Any]:
    closed = [
        trade for trade in (result.get("trade_history") or [])
        if str(getattr(trade, "action", "")).upper().startswith("SELL")
    ]
    attempts = list(result.get("entry_attempts") or [])
    candidate_count = int(result.get("entry_candidate_count") or 0)
    filled_count = int(result.get("buy_trades") or 0)
    signal_count = sum(1 for row in attempts if row.get("status") in {"filled", "signal_unfilled"})
    unfilled_count = sum(1 for row in attempts if row.get("status") == "signal_unfilled")
    # 旧固定区间没有独立的分钟信号层，可成交的竞价条件即视为信号。
    if mode == ENTRY_FIXED and not attempts:
        signal_count = filled_count
    return {
        "entry_mode": mode,
        "entry_mode_label": ENTRY_MODE_LABELS.get(mode, mode),
        "candidate_count": candidate_count,
        "signal_count": signal_count,
        "filled_count": filled_count,
        "signal_unfilled_count": unfilled_count,
        "fill_coverage": filled_count / candidate_count if candidate_count else 0.0,
        "signal_fill_rate": filled_count / signal_count if signal_count else 0.0,
        "closed_trades": len(closed),
        "win_rate": float(result.get("win_rate") or 0.0),
        "average_return": float(pd.Series([getattr(t, "pnl_pct", 0.0) for t in closed], dtype=float).mean()) if closed else 0.0,
        "total_return": float(result.get("total_return") or 0.0),
        "stop_rate": sum(bool(getattr(t, "stop_loss_triggered", False)) for t in closed) / len(closed) if closed else 0.0,
        "max_drawdown": float(result.get("max_drawdown") or 0.0),
        "average_mfe": float(pd.Series([getattr(t, "mfe_pct", 0.0) for t in closed], dtype=float).mean()) if closed else 0.0,
        "average_mae": float(pd.Series([getattr(t, "mae_pct", 0.0) for t in closed], dtype=float).mean()) if closed else 0.0,
    }


def run_entry_mode_comparison(
    *,
    data_manager: Any,
    base_config: BacktestConfig,
    start_date: str,
    end_date: str,
    trade_plans_dir: Path,
) -> Dict[str, Any]:
    results: Dict[str, Dict[str, Any]] = {}
    engines: Dict[str, BacktestEngine] = {}
    rows = []
    for mode in (ENTRY_FIXED, ENTRY_WEAK, ENTRY_CONTINUATION, ENTRY_HYBRID):
        engine = BacktestEngine(data_manager, replace(base_config, entry_mode=mode))
        result = engine.run_backtest(start_date, end_date, str(trade_plans_dir))
        engines[mode] = engine
        results[mode] = result
        rows.append(summarize_entry_result(mode, result))
    return {
        "rows": rows,
        "results": results,
        "primary": results[ENTRY_HYBRID],
        "primary_engine": engines[ENTRY_HYBRID],
    }


def save_entry_mode_comparison(comparison: Dict[str, Any], output_dir: Path, run_id: str) -> Path:
    directory = Path(output_dir) / "backtest_results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"backtest_entry_modes_{run_id}.csv"
    pd.DataFrame(comparison.get("rows") or []).to_csv(path, index=False, encoding="utf-8-sig")
    return path


__all__ = [
    "ENTRY_MODE_LABELS", "run_entry_mode_comparison", "save_entry_mode_comparison",
    "summarize_entry_result",
]
