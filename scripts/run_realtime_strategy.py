"""Independent strategy owner. No market API connections or input preparation."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from core.realtime.strategy_runtime import StrategyRuntime
    parser = argparse.ArgumentParser(description="独立只读策略进程")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notifications", action="store_true", help="启用真实通知队列消费者；默认仅判定和留档")
    args = parser.parse_args()
    worker = StrategyRuntime(notifications=args.notifications)
    signal.signal(signal.SIGINT, lambda *_: worker.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    worker.run(once=args.once)


if __name__ == "__main__":
    main()
