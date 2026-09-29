"""Freeze a five-strategy contract, then evaluate rolling future windows.

Examples:
    python scripts/strategy_experiment_ledger.py freeze
    python scripts/strategy_experiment_ledger.py evaluate ID --start 20260801 --end 20261031
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.experiments import StrategyExperimentLedger


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--label", default="production-five")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("experiment_id")
    evaluate.add_argument("--start", required=True)
    evaluate.add_argument("--end", required=True)
    evaluate.add_argument("--train-days", type=int, default=60)
    evaluate.add_argument("--validation-days", type=int, default=20)
    evaluate.add_argument("--account", default="default")
    args = parser.parse_args()
    service = StrategyExperimentLedger()
    if args.command == "freeze":
        result = service.freeze(label=args.label)
    else:
        result = service.evaluate(args.experiment_id, start=args.start, end=args.end,
                                  train_days=args.train_days, validation_days=args.validation_days,
                                  account_key=args.account)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
