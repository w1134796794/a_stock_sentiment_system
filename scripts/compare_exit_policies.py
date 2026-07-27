"""Compare exit policies on the same candidates, data and transaction costs."""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--strategies", default="default")
    parser.add_argument("--policies", default="fixed_stop,atr_stop,structure_stop,staged_trailing")
    args = parser.parse_args()

    from backtest.backtest_engine import BacktestConfig, BacktestEngine
    from backtest.plan_source import build_backtest_plan_dir
    from config.settings import CACHE_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
    from core.data.data_manager_main import DataManager
    from risk.risk_config import RiskConfig

    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    plan_dir, _, _ = build_backtest_plan_dir(
        snapshot_dir=Path(SNAPSHOT_DIR), output_dir=Path(WEB_DATA_DIR),
        screening_dir=Path(WEB_DATA_DIR) / "screening",
        start_date=args.start, end_date=args.end, strategy_ids=strategies,
    )
    base = BacktestConfig.from_risk_config(RiskConfig.load(), initial_capital=args.capital)
    rows = []
    for policy in [item.strip() for item in args.policies.split(",") if item.strip()]:
        config = deepcopy(base)
        config.exit_policy_mode = policy
        result = BacktestEngine(
            DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False), config,
        ).run_backtest(args.start, args.end, str(plan_dir))
        rows.append({
            "policy": policy,
            "total_return": result.get("total_return", 0.0),
            "max_drawdown": result.get("max_drawdown", 0.0),
            "win_rate": result.get("win_rate", 0.0),
            "profit_loss_ratio": result.get("profit_loss_ratio", 0.0),
            "closed_trades": result.get("closed_trades", 0),
            "exit_execution_audit": result.get("exit_execution_audit") or {},
        })
    output = Path(WEB_DATA_DIR) / "experiments" / f"exit_policy_{args.start}_{args.end}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "rows": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
