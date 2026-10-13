"""Create and resume frozen, independent research comparison accounts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.replay_research import ReplayResearch, ResearchSpec


def main():
    parser = argparse.ArgumentParser(description="冻结基准、仓位/择时/选股对照与滚动验证")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--start", required=True)
    freeze.add_argument("--end", required=True)
    freeze.add_argument("--capital", type=float, default=1_000_000)
    freeze.add_argument("--train-days", type=int, default=30)
    freeze.add_argument("--validation-days", type=int, default=10)
    freeze.add_argument("--embargo-days", type=int, default=5)
    freeze.add_argument("--skip-candidates", action="store_true")
    freeze.add_argument("--suite", choices=["legacy", "confirmation_quality"], default="legacy")
    freeze.add_argument("--optimization-config", type=Path, help="预先确定的优化参数JSON，不依据本次结果自动调参")
    freeze.add_argument("--amount-profile", type=Path, help="仅用区间前完整分钟训练且含源文件校验的成交进度模型")
    run = commands.add_parser("run")
    run.add_argument("experiment_id")
    verify = commands.add_parser("verify")
    verify.add_argument("experiment_id")
    args = parser.parse_args()
    service = ReplayResearch()

    def progress(row):
        print(json.dumps(row, ensure_ascii=False), flush=True)

    if args.command == "freeze":
        optimization = json.loads(args.optimization_config.read_text(encoding="utf-8")) if args.optimization_config else {}
        spec = ResearchSpec(train_days=args.train_days, validation_days=args.validation_days,
                            embargo_days=args.embargo_days,
                            suite=args.suite,
                            optimization=optimization,
                            missing_evidence_policy="skip_candidates" if args.skip_candidates else "strict")
        result = service.prepare_current(start=args.start, end=args.end, initial_capital=args.capital,
                                         spec=spec, progress=progress, amount_profile_path=args.amount_profile)
    elif args.command == "run":
        result = service.run(args.experiment_id, progress=progress)
    else:
        result = service.verify(args.experiment_id)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
