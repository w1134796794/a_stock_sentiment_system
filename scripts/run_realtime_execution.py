"""Optional paper/position consumer, separate from market collection and strategy."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def consume_paper_payload(paper, payload):
    # stored_strategy_view demotes expired confirmations individually. A stale
    # unrelated stock must not suppress fresh, executable confirmations.
    rows = [row for row in payload.get("rows") or []
            if (row.get("confirm_status") or row.get("status")) == "confirmed"]
    if not rows:
        return {"opened": [], "skipped": {}, "confirmed_candidates": 0}
    return paper.process_realtime_payload({**payload, "rows": rows})


def main():
    from core.infrastructure.shared_state import TaskLease, get_shared_state_backend
    from core.realtime.strategy_runtime import stored_strategy_view
    from core.realtime.trading_session import realtime_session_status, shanghai_now
    parser = argparse.ArgumentParser(description="独立模拟执行与持仓监控消费者")
    parser.add_argument("--paper", action="store_true", help="启用模拟账户买入与退出执行")
    parser.add_argument("--monitor", action="store_true", help="启用持仓监控及现有退出通知")
    args = parser.parse_args()
    if not args.paper and not args.monitor:
        parser.error("必须显式选择 --paper 或 --monitor")
    backend = get_shared_state_backend()
    if not backend.is_shared:
        raise RuntimeError("独立执行消费者要求共享Redis")
    lease = TaskLease.acquire("realtime_execution", 30)
    if lease is None:
        raise RuntimeError("已有执行消费者持有任务锁")
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    paper = monitor = None
    try:
        if args.paper:
            from core.portfolio.paper_trading_service import PaperTradingService
            paper = PaperTradingService()
        if args.monitor or args.paper:
            from core.portfolio.position_monitor import PositionMonitor
            monitor = PositionMonitor()
        while not stop.is_set() and lease.is_owner():
            try:
                if realtime_session_status()["is_open"]:
                    date = shanghai_now().strftime("%Y%m%d")
                    payload = stored_strategy_view(date)
                    revision = payload.get("market_revision") or ""
                    if paper and lease.is_owner():
                        # Retry while the stored signal is fresh: the first
                        # attempt may precede the next executable quote.
                        result = consume_paper_payload(paper, payload)
                        backend.set_json("realtime:execution:health", {
                            "ok": True, "market_date": date, "updated_at": shanghai_now().isoformat(),
                            "market_revision": revision, "result": result}, 120)
                    if monitor and lease.is_owner():
                        monitor.run_once(account_key="default", signal_date=date, auto_execute=args.paper)
            except Exception:
                backend.set_json("realtime:execution:health", {"ok": False,
                    "updated_at": shanghai_now().isoformat(), "reason": "模拟执行失败，检查执行进程日志"}, 120)
                from loguru import logger
                logger.exception("Independent execution consumer failed; strategy continues independently")
            stop.wait(3)
    finally:
        lease.release()


if __name__ == "__main__":
    main()
