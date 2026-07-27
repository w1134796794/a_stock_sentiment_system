"""Rebuild auditable minute-entry outcomes without training or publishing models."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description="按当前策略正式候选重建真实分钟入场标签")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--profiles", default="all")
    args = parser.parse_args()

    profiles = (
        list(STRATEGY_TRAINING_SPECS)
        if args.profiles.strip().lower() == "all"
        else [item.strip() for item in args.profiles.split(",") if item.strip()]
    )
    unknown = [profile for profile in profiles if profile not in STRATEGY_TRAINING_SPECS]
    if unknown:
        raise SystemExit(f"未知策略ID: {unknown}")

    trainer = FactorLibraryTrainer(min_daily_samples=3)
    results = {}
    for profile in profiles:
        spec = STRATEGY_TRAINING_SPECS[profile]
        prior = trainer.prior_weights(profile)
        frame = trainer.load_training_frame(
            args.start,
            args.end,
            list(prior),
            training_scope=trainer.training_scope(profile),
            profile=profile,
            horizon_days=spec.horizon_days,
        )
        persisted = trainer.persist_outcome_labels(frame, profile=profile)
        results[profile] = {
            "candidate_rows": int((trainer._last_strategy_training_audit or {}).get("candidate_rows") or 0),
            "filled_rows": int(len(frame)),
            "persisted_rows": int(persisted),
            "audit": trainer._last_strategy_training_audit,
        }
        print(json.dumps({profile: results[profile]}, ensure_ascii=False), flush=True)
    print(json.dumps({"ok": True, "start": args.start, "end": args.end, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
