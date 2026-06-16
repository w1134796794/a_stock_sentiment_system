"""Run Phase 5 realtime overlay for screened candidates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.realtime.overlay_service import RealtimeOverlayService


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run realtime confirm/cancel overlay.")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD；缺省读取最新快照")
    parser.add_argument("--profile", default="", help="screening profile 名称，缺省读取 screening_YYYYMMDD.json")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-persist", action="store_true", help="只打印结果，不写 webdata/realtime")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = RealtimeOverlayService().build_overlay(
        args.date or None,
        profile=args.profile,
        limit=args.limit,
        persist=not args.no_persist,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
