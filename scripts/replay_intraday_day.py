"""CLI equivalent of the isolated intraday diagnostic page."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.infrastructure.atomic_json import atomic_write_json
from core.realtime.day_replay import DayReplayLoader, compare_reports, replay_request, replay_saved_inputs


def main():
    parser = argparse.ArgumentParser(description="真实分钟诊断回放，不交易、不推送")
    parser.add_argument("--date", required=True)
    parser.add_argument("--codes", default="")
    parser.add_argument("--profile", choices=("leader_pool", "decision_pool"), default="leader_pool")
    parser.add_argument("--lookback", type=int, choices=(5, 10, 20), default=10)
    parser.add_argument("--until", default="15:01")
    parser.add_argument("--fetch-missing", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = replay_request({"market_date": args.date, "codes": args.codes, "profile": args.profile,
                              "lookback": args.lookback, "until": args.until, "fetch_missing": args.fetch_missing})
    def progress(value):
        print(f"{value['stage']}: {value.get('completed', 0)}/{value.get('total', 0)} {value.get('code', '')}", flush=True)
    if args.baseline:
        captured = json.loads(args.baseline.with_suffix(".inputs.json").read_text(encoding="utf-8"))
        if captured["request"] != {k:v for k,v in request.items() if k != "fetch_missing"}:
            parser.error("对照日期、范围、股票与结束时间必须与基准一致")
        report = replay_saved_inputs(captured, progress)
        report["comparison"] = compare_reports(json.loads(args.baseline.read_text(encoding="utf-8")), report)
    else:
        report = DayReplayLoader().run(request, progress)
    atomic_write_json(args.output.with_suffix(".inputs.json"), report.pop("replay_inputs"))
    atomic_write_json(args.output, report, indent=2)
    print(json.dumps({"output": str(args.output.resolve()), "confirmed_stocks": report["confirmed_stocks"],
                      "complete": report["complete"], "counts": report["counts"],
                      "live_runtime_verified": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
