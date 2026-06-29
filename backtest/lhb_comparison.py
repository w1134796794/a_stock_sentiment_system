"""Run point-in-time LHB screening variants through the same trading engine."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.plan_source import build_backtest_plan_dir


SCENARIOS = {
    "no_lhb": "不使用龙虎榜",
    "net_buy": "仅龙虎榜净买入",
    "institution": "仅机构净买入",
    "lhb_sector": "龙虎榜＋板块共振",
}


def _closed_trades(result: Dict[str, Any]) -> List[Any]:
    return [
        trade for trade in result.get("trade_history") or []
        if str(getattr(trade, "action", "") or "").upper().startswith("SELL")
    ]


def _scenario_row(
    scenario: str,
    result: Dict[str, Any],
    *,
    candidate_rows: int,
    candidate_days: int,
) -> Dict[str, Any]:
    closed = _closed_trades(result)
    buy_count = sum(
        1 for trade in result.get("trade_history") or []
        if str(getattr(trade, "action", "") or "").upper() == "BUY"
    )
    pnl_pcts = [float(getattr(trade, "pnl_pct", 0.0) or 0.0) for trade in closed]
    stop_count = sum(bool(getattr(trade, "stop_loss_triggered", False)) for trade in closed)
    return {
        "scenario": scenario,
        "scenario_name": SCENARIOS[scenario],
        "candidate_days": int(candidate_days),
        "candidate_rows": int(candidate_rows),
        "executed_buys": int(buy_count),
        "coverage_rate": float(buy_count / candidate_rows) if candidate_rows else 0.0,
        "closed_trades": int(len(closed)),
        "win_rate": float(result.get("win_rate") or 0.0),
        "avg_return": float(sum(pnl_pcts) / len(pnl_pcts)) if pnl_pcts else 0.0,
        "total_return": float(result.get("total_return") or 0.0),
        "stop_loss_rate": float(stop_count / len(closed)) if closed else 0.0,
        "max_drawdown": float(result.get("max_drawdown") or 0.0),
    }


def run_lhb_comparison(
    *,
    data_manager: Any,
    config: BacktestConfig,
    start_date: str,
    end_date: str,
    snapshot_dir: Path,
    web_data_dir: Path,
    baseline_result: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Compare four ranking variants; all variants execute T using T-1 plans."""
    rows: List[Dict[str, Any]] = []
    for scenario in SCENARIOS:
        plan_dir, file_count, row_count = build_backtest_plan_dir(
            snapshot_dir=Path(snapshot_dir),
            output_dir=Path(web_data_dir),
            screening_dir=Path(web_data_dir) / "screening",
            start_date=start_date,
            end_date=end_date,
            lhb_scenario=scenario,
        )
        if scenario == "lhb_sector" and baseline_result is not None:
            result = baseline_result
        else:
            engine = BacktestEngine(data_manager, replace(config))
            result = engine.run_backtest(start_date, end_date, str(plan_dir))
        rows.append(_scenario_row(
            scenario, result, candidate_rows=row_count, candidate_days=file_count,
        ))
    return pd.DataFrame(rows)


def save_lhb_comparison(frame: pd.DataFrame, output_dir: Path, run_id: str) -> Path:
    path = Path(output_dir) / "backtest_results" / f"backtest_lhb_comparison_{run_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


__all__ = ["SCENARIOS", "run_lhb_comparison", "save_lhb_comparison"]
