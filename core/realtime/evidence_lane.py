"""Bounded, optional collector lane. Never executes in the quote request path."""

from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread

from core.realtime.market_evidence import timestamp


class EvidenceLane:
    HEALTH_KEY = "realtime:evidence:health"

    def __init__(
        self,
        store,
        provider_factory,
        *,
        clock,
        owner,
        auctions=False,
        trades=False,
        depth=False,
        interval=5,
        workers=2,
        max_pages=3,
        max_pending=10000,
        min_free_mb=512,
    ):
        self.store, self.provider_factory = store, provider_factory
        self.clock, self.owner = clock, owner
        self.auctions, self.trades, self.depth = auctions, trades, depth
        self.enabled = any((auctions, trades, depth))
        self.interval, self.workers, self.max_pages = max(5, interval), min(2, max(1, workers)), max_pages
        self.max_pending, self.min_free_mb = max_pending, min_free_mb
        self.health = dict(
            enabled=self.enabled, status="not_started" if self.enabled else "disabled", formal_eligible=False
        )
        self._lock, self._thread, self._last = Lock(), None, None

    def active(self):
        return self.owner() and (
            "09:15:00" <= self.clock().strftime("%H:%M:%S") <= "11:30:15"
            or "13:00:00" <= self.clock().strftime("%H:%M:%S") <= "15:05:00"
        )

    def schedule(self, plan):
        if not self.enabled or not self.active():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self._last is not None and time.monotonic() - self._last < self.interval:
                return
            self._last = time.monotonic()
            self._thread = Thread(target=self.collect_once, args=(plan,), daemon=True, name="eltdx-evidence")
            self._thread.start()

    def _collect(self, plan, code):
        provider = self.provider_factory()  # Each worker owns its client/transport.
        date, plan_id = plan["market_date"], plan["plan_id"]
        statuses = []
        if not self.active():
            return statuses

        def save(result):
            if not self.active() or self.clock().strftime("%Y%m%d") != date:
                return False
            self.store.write(result, plan_id=plan_id)
            statuses.append(
                dict(
                    code=code,
                    kind=result.kind,
                    status=result.status,
                    reasons=result.reasons,
                    coverage=result.coverage,
                    rows=len(result.rows),
                )
            )
            return True

        if self.auctions and self.active():
            at = self.clock().strftime("%H:%M:%S")
            # Preserve full auction curve through 09:30, not merely final open amount.
            if at <= "09:30:00":
                if not save(provider.get_auction_evidence(code, date)):
                    return statuses
            if at >= "09:25:00" and self.active():
                prior = (
                    self.store.backend.get_json(self.store.key(date, code, "opening_match"))
                    if self.store.backend
                    else None
                )
                if not prior or prior.get("quality_status") != "ok":
                    if not save(provider.get_opening_match_evidence(code, date)):
                        return statuses
        if self.trades and self.clock().strftime("%H:%M:%S") >= "09:30:00" and self.active():
            cursor = self.store.cursor(date, code, plan_id)
            save(
                provider.get_trade_evidence(
                    code,
                    date,
                    max_pages=self.max_pages,
                    anchor=cursor.get("anchor"),
                    next_sequence=cursor.get("next_sequence", 0),
                )
            )
        if self.depth and self.active():
            # Reuse five levels from the primary quote; only request depth if unavailable.
            raw = self.store.backend.get_json(f"realtime:quotes:latest:{code}") if self.store.backend else None
            book = (raw or {}).get("depth")
            if (
                book
                and raw.get("date") == date
                and book.get("quality_status") == "ok"
                and raw.get("received_at")
                and 0 <= (self.clock() - timestamp(raw["received_at"])).total_seconds() <= 15
            ):
                statuses.append(dict(code=code, kind="depth", status="snapshot_embedded"))
            else:
                save(provider.get_depth_evidence(code, date))
        return statuses

    def collect_once(self, plan):
        started = time.monotonic()
        result = dict(enabled=self.enabled, status="inactive", formal_eligible=False, stocks=[])
        try:
            if not self.enabled or not self.active():
                return result
            if not plan or plan["market_date"] != self.clock().strftime("%Y%m%d"):
                result["status"] = "missing_collection_plan"
            elif (
                self.store.journal.health()["pending"] >= self.max_pending
                or shutil.disk_usage(self.store.journal.root).free < self.min_free_mb * 1024**2
            ):
                result["status"] = "backpressure"
            else:
                # Submit only bounded concurrent work; no unbounded future queue.
                codes = list(dict.fromkeys(row["code"] for row in plan["rows"]))
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    for offset in range(0, len(codes), self.workers):
                        if not self.active():
                            result["status"] = "ownership_or_session_lost"
                            break
                        tasks = [
                            pool.submit(self._collect, plan, code) for code in codes[offset : offset + self.workers]
                        ]
                        for task in as_completed(tasks):
                            result["stocks"].extend(task.result())
                    else:
                        result["status"] = (
                            "ok"
                            if all(r["status"] in {"ok", "empty", "snapshot_embedded"} for r in result["stocks"])
                            else "partial"
                        )
        except Exception as exc:
            result.update(status="failed", category=type(exc).__name__, error=str(exc))
        finally:
            result.update(
                updated_at=self.clock().isoformat(), duration_ms=round((time.monotonic() - started) * 1000, 3)
            )
            self.health = result
            if self.store.backend is not None and self.owner():
                try:
                    self.store.backend.set_json(self.HEALTH_KEY, result, 120)
                except Exception:
                    pass  # Failure remains available in the collector's local health.
        return result
