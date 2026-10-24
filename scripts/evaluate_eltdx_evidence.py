"""Freeze archived inputs and policy into an offline A/B/C comparison artifact."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import WEB_DATA_DIR
    from core.realtime.day_replay import replay_request
    from core.realtime.market_evidence import fingerprint
    from core.realtime.snapshot_replay import load_snapshot_inputs
    from core.realtime.trading_session import shanghai_now

    parser = argparse.ArgumentParser(description="离线A/B/C证据对照，不执行交易或改变生产参数")
    parser.add_argument("--dates", help="逗号分隔YYYYMMDD")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--until", default="15:01:00")
    parser.add_argument("--profile", choices=["leader_pool", "decision_pool"], default="decision_pool")
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--saved-inputs", type=Path, help="重用既有实验工件中的冻结输入")
    parser.add_argument("--outcomes", type=Path, help="可选、精确匹配确认时点的执行回测结果JSON")
    parser.add_argument("--output", type=Path, help="可选JSON工件路径，包含输入与策略")
    args = parser.parse_args()
    from uuid import uuid4

    from core.infrastructure.atomic_json import atomic_write_json
    from core.realtime.evidence_experiment import compare_day, summarize_days, validate_policy

    policy = validate_policy(json.loads(args.policy.read_text(encoding="utf-8")))
    output = args.output or args.data_root / "experiments" / "eltdx_evidence" / f"{uuid4().hex}.json"
    input_folder = output.parent / ".inputs"
    references, reports = [], []
    prior = None
    if args.saved_inputs:
        prior = json.loads(args.saved_inputs.read_text(encoding="utf-8"))
        references_from_prior = prior["frozen_inputs"]
        if fingerprint([ref["hash"] for ref in references_from_prior]) != prior["frozen_inputs_hash"]:
            raise ValueError("Saved experiment manifest changed")
        source_items = references_from_prior
    else:
        if not args.dates:
            parser.error("--dates or --saved-inputs is required")
        source_items = list(dict.fromkeys(args.dates.split(",")))
    for item in source_items:
        if prior is not None:
            path = (args.saved_inputs.parent / item["file"]).resolve()
            if not path.is_relative_to(args.saved_inputs.parent.resolve() / ".inputs"):
                raise ValueError("Saved input path escapes the frozen-input directory")
            inputs = json.loads(path.read_text(encoding="utf-8"))
            if fingerprint(inputs) != item["hash"]:
                raise ValueError("Saved experiment inputs changed")
        else:
            request = replay_request(
                dict(
                    market_date=item,
                    until=args.until,
                    profile=args.profile,
                    replay_mode="archived_snapshot",
                    fetch_missing=False,
                )
            )
            inputs = load_snapshot_inputs(args.data_root, request)
        identity = fingerprint(inputs)
        target = input_folder / f"{identity}.json"
        if target.exists():
            if fingerprint(json.loads(target.read_text(encoding="utf-8"))) != identity:
                raise ValueError("Frozen input artifact is corrupt")
        else:
            atomic_write_json(target, inputs, indent=0)
        references.append(
            dict(file=f".inputs/{identity}.json", hash=identity, market_date=inputs["request"]["market_date"])
        )
        reports.append(compare_day(inputs, policy))
        del inputs  # Keep only one day's archive in memory.
    report = summarize_days(reports, policy)
    report["frozen_inputs"] = references
    report["frozen_inputs_hash"] = fingerprint([ref["hash"] for ref in references])
    if args.outcomes:
        from core.realtime.evidence_outcomes import attach_outcomes

        attach_outcomes(report, json.loads(args.outcomes.read_text(encoding="utf-8")))
    report["generated_at"] = shanghai_now().isoformat()
    from hashlib import sha256

    source_files = [
        path
        for folder in ("core", "backtest", "risk", "config", "snapshot")
        for path in (ROOT / folder).rglob("*")
        if path.suffix in {".py", ".yaml"} and path.is_file()
    ]
    source_files.append(Path(__file__).resolve())
    report["implementation_hash"] = fingerprint(
        {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest() for path in source_files}
    )
    try:
        report["code_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["code_dirty"] = bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        report["code_commit"] = None
        report["code_dirty"] = None
    from core.infrastructure.atomic_json import atomic_write_json

    atomic_write_json(output, report, indent=2)
    report["artifact_name"] = output.name
    summary = {k: v for k, v in report.items() if k not in {"frozen_inputs"}}
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if all(day["baseline_complete"] for day in report["days"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
