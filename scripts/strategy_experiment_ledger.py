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
    analog_freeze = commands.add_parser("analog-freeze")
    analog_freeze.add_argument("--label", default="analog")
    analog_freeze.add_argument("--train-days", type=int, default=60)
    analog_freeze.add_argument("--calibration-days", type=int, default=20)
    analog_freeze.add_argument("--test-days", type=int, default=20)
    analog_freeze.add_argument("--gap-days", type=int, default=5)
    analog = commands.add_parser("analog-evaluate")
    analog.add_argument("contract_id")
    analog.add_argument("--start", required=True)
    analog.add_argument("--end", required=True)
    analog.add_argument("--db", type=Path)
    analog.add_argument("--research", action="store_true")
    analog.add_argument("--account")
    analog.add_argument("--output", type=Path)
    score = commands.add_parser("analog-score")
    score.add_argument("case_id")
    score.add_argument("--as-of", required=True)
    score.add_argument("--index", type=Path)
    score.add_argument("--research", action="store_true")
    score.add_argument("--shadow", action="store_true", help="Record forward-only research predictions, never a trading gate")
    score.add_argument("--model", type=Path, help="One frozen model JSON object, not the models list")
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
    elif args.command == "analog-freeze":
        from core.signals.similarity_validation import ValidationConfig
        result = service.freeze_analog(label=args.label, config=ValidationConfig(train_days=args.train_days,
            calibration_days=args.calibration_days, test_days=args.test_days, gap_days=args.gap_days))
    elif args.command == "analog-score":
        from config.settings import WEB_DATA_DIR
        from core.signals.similar_case_service import SimilarCaseService, load_case_index
        from core.signals.similarity_validation import evaluate_frozen_model
        index = load_case_index(args.index or WEB_DATA_DIR / "signal_cases" / "matching_index.json")
        query = next((r for r in index["records"] if r["case"]["case_id"] == args.case_id), None)
        if query is None:
            parser.error("Case absent from bounded index; republish the required date range")
        if args.model:
            if args.research:
                parser.error("Frozen forward models cannot be scored in research mode")
            result = evaluate_frozen_model(json.loads(args.model.read_text(encoding="utf-8")), query,
                                           as_of=args.as_of, shadow=args.shadow)
        else:
            if args.shadow:
                parser.error("--shadow requires a frozen --model")
            result = SimilarCaseService(index["records"]).fit(as_of=args.as_of, research=args.research).evaluate(query, as_of=args.as_of)
        if result.get("forecast_id"):
            result = service.ledger.save_case_forecast(result)
    elif args.command == "analog-evaluate":
        from config.settings import WEB_DATA_DIR
        from core.signals.case_repository import SignalCaseRepository
        from core.signals.case_features import clean
        report, models, predictions = service.evaluate_analog(args.contract_id, repository=SignalCaseRepository(args.db),
            start=args.start, end=args.end, research=args.research, account_key=args.account)
        directory = args.output or WEB_DATA_DIR / "signal_cases" / "validation" / report["experiment_id"]
        directory.mkdir(parents=True, exist_ok=True)
        for name, payload in (("report.json", report), ("models.json", models), ("predictions.json", predictions)):
            path = directory / name
            pending = path.with_suffix(".json.tmp")
            pending.write_text(json.dumps(clean(payload), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            pending.replace(path)
        for model in models:
            path = directory / (model["model_id"] + ".json")
            pending = path.with_suffix(".json.tmp")
            pending.write_text(json.dumps(model, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            pending.replace(path)
        result = {**report, "artifact_directory": str(directory)}
    else:
        result = service.evaluate(args.experiment_id, start=args.start, end=args.end,
                                  train_days=args.train_days, validation_days=args.validation_days,
                                  account_key=args.account)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
