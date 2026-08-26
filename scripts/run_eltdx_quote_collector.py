"""Standalone eltdx quote collector entrypoint."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import ELTDX_MINUTE_SYNC_SECONDS  # noqa: E402
from core.realtime.eltdx_collector import EltdxQuoteCollector  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="独立eltdx实时行情采集器")
    parser.add_argument("--codes", default="", help="额外固定代码，逗号分隔")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--minute-interval", type=int, default=ELTDX_MINUTE_SYNC_SECONDS)
    args = parser.parse_args()
    collector = EltdxQuoteCollector(
        fixed_codes=[part.strip() for part in args.codes.split(",") if part.strip()],
        minute_interval_seconds=args.minute_interval,
        require_shared=True,
    )
    signal.signal(signal.SIGINT, collector.stop)
    signal.signal(signal.SIGTERM, collector.stop)
    collector.run(once=args.once)


if __name__ == "__main__":
    main()
