"""Continuously drain the durable ingestion spool into daily SQLite archives."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from loguru import logger
    from core.realtime.market_journal import MarketJournal
    from core.realtime.trading_session import shanghai_now
    from core.infrastructure.shared_state import TaskLease, get_shared_state_backend
    parser = argparse.ArgumentParser(description="行情异步增量归档")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    journal, stop = MarketJournal(args.root), Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    lease = TaskLease.acquire("market_archiver:" + str(journal.path.resolve()), 30)
    if lease is None:
        raise RuntimeError("已有归档进程持有该日志任务锁")
    try:
        while not stop.is_set() and lease.is_owner():
            try:
                count = journal.archive_once()
                status = {"ok": True, "archived_batch": count, **journal.health(), "updated_at": shanghai_now().isoformat()}
                get_shared_state_backend().set_json("realtime:archive:health", status, 60)
                if args.once:
                    print(json.dumps(status))
                    break
                if not count:
                    stop.wait(1)
            except Exception as exc:
                logger.exception("Market archive failed; pending spool is retained")
                get_shared_state_backend().set_json("realtime:archive:health", {"ok": False,
                    "reason": str(exc), "updated_at": shanghai_now().isoformat()}, 60)
                if args.once:
                    raise
                stop.wait(3)
    finally:
        lease.release()


if __name__ == "__main__":
    main()
