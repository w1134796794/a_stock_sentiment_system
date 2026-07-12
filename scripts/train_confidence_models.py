"""Train dated confidence models and optionally run purged monthly validation."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main() -> int:
    parser = argparse.ArgumentParser(description="训练候选可信度模型")
    parser.add_argument("--start", default="", help="训练开始日 YYYYMMDD")
    parser.add_argument("--end", required=True, help="训练标签截止日 YYYYMMDD")
    parser.add_argument("--effective-date", default="", help="模型生效日，必须晚于训练标签")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--walk-forward", action="store_true", help="同时执行按月 Purged Walk-Forward")
    parser.add_argument("--train-months", type=int, default=12, help="滚动训练月数，默认约1年")
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y%m%d")
    start = args.start or (end - timedelta(days=540)).strftime("%Y%m%d")
    effective = args.effective_date or (end + timedelta(days=1)).strftime("%Y%m%d")
    if effective <= args.end:
        parser.error("--effective-date 必须晚于 --end，避免模型使用未来信息")

    from core.factors.factor_library import FactorLibraryTrainer

    trainer = FactorLibraryTrainer()
    result = trainer.train_and_publish(
        start, args.end, profile=args.profile, effective_date=effective,
    )
    candidate = result.get("candidate_model") or {}
    summary = {
        "profile": args.profile,
        "train_start": start,
        "train_end": args.end,
        "effective_date": effective,
        "training_rows": result.get("training_rows"),
        "training_days": result.get("training_days"),
        "model_type": result.get("model_type"),
        "publication_gate": result.get("publication_gate"),
        "candidate_model": {
            "active": candidate.get("active"),
            "status": candidate.get("status"),
            "rank_ic": candidate.get("rank_ic"),
            "top_decile_excess_return": candidate.get("top_decile_excess_return"),
            "calibration": candidate.get("calibration"),
            "return_conformal": candidate.get("return_conformal"),
        },
        "market_regime": result.get("market_regime_model"),
    }
    if args.walk_forward:
        summary["walk_forward"] = trainer.walk_forward(
            start, args.end, profile=args.profile, train_months=args.train_months,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
