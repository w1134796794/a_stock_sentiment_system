"""Train dated dynamic screening weights and optionally run monthly OOS validation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.factors.factor_library import FactorLibraryTrainer
from core.factors.strategy_training import STRATEGY_TRAINING_SPECS


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
    args = parser.parse_args()

    trainer = FactorLibraryTrainer()
    if args.all_strategies:
        results = {}
        for profile in STRATEGY_TRAINING_SPECS:
            try:
                results[profile] = trainer.train_and_publish(
                    args.start, args.end, profile=profile, effective_date=args.effective_date,
                )
            except Exception as exc:
                results[profile] = {"ok": False, "error": str(exc)}
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return
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
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
