"""Offline board shadow replay and fixed synthetic fixture verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def verify_fixture(fixture):
    from core.realtime.board_replay import replay_board_inputs

    if fixture.get("synthetic") is not True:
        raise ValueError("Fixtures must be explicitly synthetic")
    checks = []
    for case in fixture["cases"]:
        result = replay_board_inputs({**fixture["inputs"], "synthetic": True, "until": case["until"]})
        actual = {
            "status": result["status"],
            "observed_points": result.get("observed_points", 0),
            "board_status": result["rows"][0]["status"] if result["rows"] else None,
            "formal_signal": result["formal_signal"],
            "notification_sent": result["notification_sent"],
        }
        ok = actual == case["expected"]
        checks.append({"name": case["name"], "ok": ok, "actual": actual, "expected": case["expected"]})
    return {
        "ok": bool(checks) and all(check["ok"] for check in checks),
        "synthetic": True,
        "live_runtime_verified": False,
        "checks": checks,
    }


def main():
    from core.realtime.board_replay import load_board_inputs, replay_board_inputs

    parser = argparse.ArgumentParser(description="只读板块影子回放，不补取、不发送通知")
    parser.add_argument("--market-date")
    parser.add_argument("--until", default="15:01:00")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--data-root", type=Path)
    args = parser.parse_args()
    if args.fixture:
        result = verify_fixture(json.loads(args.fixture.read_text(encoding="utf-8-sig")))
    else:
        if not args.market_date:
            parser.error("--market-date or --fixture is required")
        from config.settings import WEB_DATA_DIR

        result = replay_board_inputs(
            load_board_inputs(args.data_root or WEB_DATA_DIR, args.market_date, until=args.until)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if result.get("ok") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
