"""Run explicit historical backtest stages in a separate process."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from core.portfolio.history_workflow import PHASES, HistoryWorkflows

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--start", required=True)
    create.add_argument("--end", required=True)
    create.add_argument("--capital", type=float, default=1_000_000)
    step = commands.add_parser("step")
    step.add_argument("job_id")
    step.add_argument("phase", choices=PHASES)
    status = commands.add_parser("status")
    status.add_argument("job_id")
    args = parser.parse_args()
    jobs = HistoryWorkflows(args.root)
    if args.command == "create":
        value = jobs.create(args.start, args.end, args.capital)
    elif args.command == "step":
        value = jobs.execute(args.job_id, args.phase)
    else:
        value = jobs.get(args.job_id)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if value["state"] in {"idle", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
