"""Optional isolated board collector; never participates in formal confirmation."""
from __future__ import annotations

import time
from threading import Lock, Thread

from core.realtime.board_quote_store import trading_time


class BoardQuoteLane:
    HEALTH_KEY = "realtime:tdx-board:health"

    def __init__(self, plans, store, provider, *, enabled=False, interval=10, clock, owner=lambda: True):
        self.plans, self.store, self.provider = plans, store, provider
        self.enabled, self.interval, self.clock, self.owner = enabled, interval, clock, owner
        self._lock = Lock()
        self._thread = None
        self._last_started = None
        self.health = {"enabled": enabled, "status": "not_started" if enabled else "disabled", "formal_eligible": False}

    def schedule(self, parent):
        if not self.enabled or not trading_time(self.clock()) or not self.owner():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            now = time.monotonic()
            if self._last_started is not None and now - self._last_started < self.interval:
                return
            self._last_started = now
            self._thread = Thread(target=self.collect_once, args=(parent,), name="eltdx-board-shadow", daemon=True)
            self._thread.start()

    def collect_once(self, parent):
        started = time.monotonic()
        stamp = self.clock()
        result = {"enabled": self.enabled, "formal_eligible": False, "count": 0,
                  "market_date": stamp.strftime("%Y%m%d")}
        try:
            if not self.enabled or not self.owner() or not trading_time(stamp):
                result["status"] = "inactive"
            elif not parent or parent["market_date"] != result["market_date"]:
                result["status"] = "missing_collection_plan"
            else:
                plan = self.plans.load(result["market_date"], parent_id=parent["plan_id"])
                if not plan or not plan["codes"]:
                    result["status"] = "missing_board_plan" if not plan else "no_mapping_candidates"
                else:
                    payload = self.provider.get_board_snapshots(plan["codes"])
                    finished = self.clock()
                    if not self.owner() or finished.strftime("%Y%m%d") != plan["market_date"] or not trading_time(finished):
                        result["status"] = "ownership_or_session_lost"
                    else:
                        result.update(self.store.write(plan, payload["rows"], now=finished))
                        result.update(requested_count=len(plan["codes"]), batches=payload["batches"],
                                      missing_codes=payload["missing_codes"], board_plan_id=plan["plan_id"])
                        result["status"] = "ok" if result["count"] == len(plan["codes"]) else "partial" if result["count"] else "failed"
        except Exception as exc:
            result.update(status="failed", error=str(exc), category=type(exc).__name__)
        result.update(updated_at=self.clock().isoformat(), duration_ms=round((time.monotonic() - started) * 1000, 3))
        self.health = result
        try:
            self.store.backend.set_json(self.HEALTH_KEY, result, 120)
        except Exception as exc:
            from loguru import logger
            logger.warning("TDX board shadow health could not be published: {}", exc)
        return result
