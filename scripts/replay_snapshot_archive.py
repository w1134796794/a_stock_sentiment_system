"""Read-only archived snapshot diagnostics, separate from live strategy processes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import WEB_DATA_DIR
    from core.realtime.day_replay import replay_request
    from core.realtime.snapshot_replay import load_snapshot_inputs, replay_snapshot_inputs

    parser = argparse.ArgumentParser(description="只读归档快照回放，不补取、不发送通知")
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--until", default="15:01:00")
    parser.add_argument("--profile", choices=("leader_pool", "decision_pool"), default="leader_pool")
    parser.add_argument("--codes", default="")
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    args = parser.parse_args()
    request = replay_request(
        {
            "market_date": args.market_date,
            "until": args.until,
            "profile": args.profile,
            "codes": args.codes,
            "replay_mode": "archived_snapshot",
        }
    )
    report = replay_snapshot_inputs(load_snapshot_inputs(args.data_root, request))
    report.pop("replay_inputs", None)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
