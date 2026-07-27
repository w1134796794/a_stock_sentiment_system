"""Train dated dynamic screening weights and optionally run monthly OOS validation."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.factors.factor_library import FactorLibraryTrainer, TrainingPrerequisiteError
from core.factors.strategy_training import STRATEGY_TRAINING_SPECS


def build_child_command(args: argparse.Namespace, profile: str, result_file: Path) -> list[str]:
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--start", args.start, "--end", args.end,
        "--profile", profile, "--result-file", str(result_file),
    ]
    if args.effective_date:
        command.extend(["--effective-date", args.effective_date])
    if args.walk_forward:
        command.extend(["--walk-forward", "--train-months", str(args.train_months)])
    return command


def main() -> None:
    parser = argparse.ArgumentParser(description="训练 IC/IR 动态因子权重")
    parser.add_argument("--start", required=True, help="训练开始日 YYYYMMDD")
    parser.add_argument("--end", required=True, help="训练结束日 YYYYMMDD")
    parser.add_argument("--profile", default="default")
    parser.add_argument(
        "--all-strategies", action="store_true",
        help="依次训练主线龙头、接力、首板、资金、量价、弱转强和趋势模型",
    )
    parser.add_argument("--effective-date", default="")
    parser.add_argument("--walk-forward", action="store_true", help="执行按月滚动样本外验证")
    parser.add_argument("--train-months", type=int, default=12, help="滚动训练月数，默认约1年")
    parser.add_argument("--result-file", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.all_strategies:
        results = {}
        for profile in STRATEGY_TRAINING_SPECS:
            with tempfile.TemporaryDirectory(prefix=f"factor-train-{profile}-") as directory:
                result_file = Path(directory) / "result.json"
                command = build_child_command(args, profile, result_file)
                completed = subprocess.run(command, cwd=str(ROOT), check=False)
                if result_file.exists():
                    payload = json.loads(result_file.read_text(encoding="utf-8"))
                    results[profile] = {
                        "ok": bool(payload.get("ok", completed.returncode == 0)),
                        "status": payload.get("status", "completed" if completed.returncode == 0 else "failed"),
                        "path": payload.get("path"),
                        "model_type": payload.get("model_type"),
                        "training_rows": payload.get("training_rows"),
                        "training_days": payload.get("training_days"),
                        "oos_months": payload.get("oos_calibration_months"),
                        "publication_gate": payload.get("publication_gate"),
                        "summary": payload.get("summary"),
                        "reason_code": payload.get("reason_code"),
                        "audit": payload.get("audit"),
                        "hint": payload.get("hint"),
                        "error": payload.get("error"),
                    }
                else:
                    results[profile] = {
                        "ok": False, "error": f"训练子进程退出码 {completed.returncode}，未生成结果",
                    }
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return
    # Formal strategy models rank a deliberately compact daily candidate pool.
    # Use a three-name cross-section after real minute-entry filtering; the
    # broad default model keeps the stricter 20-name requirement.
    min_daily_samples = 3 if args.profile in STRATEGY_TRAINING_SPECS else 20
    trainer = FactorLibraryTrainer(min_daily_samples=min_daily_samples)
    try:
        if args.walk_forward:
            result = trainer.walk_forward(
                args.start, args.end, profile=args.profile, train_months=args.train_months
            )
        else:
            result = trainer.train_and_publish(
                args.start,
                args.end,
                profile=args.profile,
                effective_date=args.effective_date,
            )
    except TrainingPrerequisiteError as exc:
        result = {
            "ok": False,
            "status": "skipped",
            "profile": args.profile,
            "reason_code": exc.reason_code,
            "message": str(exc),
            "audit": exc.audit,
            "hint": exc.hint,
        }
        if args.result_file:
            args.result_file.parent.mkdir(parents=True, exist_ok=True)
            args.result_file.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    except Exception as exc:
        result = {"ok": False, "profile": args.profile, "error": str(exc)}
        if args.result_file:
            args.result_file.parent.mkdir(parents=True, exist_ok=True)
            args.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    if args.result_file:
        args.result_file.parent.mkdir(parents=True, exist_ok=True)
        args.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
