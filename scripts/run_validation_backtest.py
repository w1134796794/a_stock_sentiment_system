"""Run a reproducible local-cache backtest and save monthly OOS validation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.plan_source import build_backtest_plan_dir
from config.settings import CACHE_DIR, OUTPUT_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
from core.data.data_manager_main import DataManager
from core.screening.enhancements import enhancement_label, normalize_enhancements
from risk.risk_config import RiskConfig
from run_backtest import save_backtest_results


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run baseline validation from generated screening artifacts.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--no-risk", action="store_true")
    parser.add_argument("--enhancements", default="")
    return parser.parse_args()


def main() -> int:
    args = _args()
    selected = normalize_enhancements(args.enhancements)
    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=Path(SNAPSHOT_DIR),
        output_dir=Path(WEB_DATA_DIR),
        screening_dir=Path(WEB_DATA_DIR) / "screening",
        start_date=str(args.start),
        end_date=str(args.end),
        max_rank=0,
        enhancements=selected,
    )
    if file_count <= 0:
        raise SystemExit("No generated plans are available in the requested range")
    print(f"plans={file_count} rows={row_count} dir={plan_dir}")

    risk_control = not args.no_risk
    data_manager = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
    config = BacktestConfig.from_risk_config(
        RiskConfig.load(), initial_capital=args.capital, risk_control=risk_control,
    )
    config.max_plan_rank = 0
    result = BacktestEngine(data_manager, config).run_backtest(
        start_date=str(args.start),
        end_date=str(args.end),
        trade_plans_dir=str(plan_dir),
    )
    run_id = save_backtest_results(result, OUTPUT_DIR, metadata={
        "run_mode": "range",
        "start_date": str(args.start),
        "end_date": str(args.end),
        "risk_control": risk_control,
        "max_plan_rank": 0,
        "enhancements": selected,
        "enhancement_label": enhancement_label(selected),
        "validation_window": "3个月训练 + 1个月样本外验证",
    })
    print(
        f"run_id={run_id} total_return={float(result.get('total_return') or 0):.4%} "
        f"win_rate={float(result.get('win_rate') or 0):.4%} "
        f"max_drawdown={float(result.get('max_drawdown') or 0):.4%} "
        f"closed_trades={int(result.get('closed_trades') or result.get('total_trades') or 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
