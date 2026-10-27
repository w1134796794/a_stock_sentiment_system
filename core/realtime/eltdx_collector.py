"""Self-contained eltdx quote collector shared by Web and the CLI runner."""
from __future__ import annotations

import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock, Thread

from loguru import logger

from backtest.trade_calendar import TradeCalendar
from config.settings import (
    ELTDX_COLLECTOR_ID,
    ELTDX_CONSTITUENT_WORKERS,
    ELTDX_HOST,
    ELTDX_MINUTE_SYNC_SECONDS,
    ELTDX_POLL_INTERVAL_SECONDS,
    ELTDX_RETRY_BACKOFF_SECONDS,
    ELTDX_RETRY_COUNT,
    ELTDX_TIMEOUT_SECONDS,
    MARKET_DATA_NODE_ROLE,
    REALTIME_QUOTE_TTL_SECONDS,
)
from core.data.providers.eltdx_provider import EltdxProvider
from core.infrastructure.shared_state import TaskLease
from core.portfolio.holding_repository import HoldingRepository
from core.realtime.minute_cache import RealtimeMinuteCache
from core.realtime.quote_cache import RealtimeQuoteCache
from core.realtime.watchlist_repository import RealtimeWatchlistRepository
from core.realtime.trading_session import shanghai_now
from core.realtime.collection_plan import DailyCollectionPlan
from core.realtime.market_journal import MarketJournal


class EltdxQuoteCollector:
    """Collect one normalized quote stream for all Web workers and users."""

    HEALTH_KEY = RealtimeQuoteCache.COLLECTOR_HEALTH_KEY
    LEASE_NAME = "eltdx_quote_collector"

    def __init__(
        self,
        *,
        fixed_codes=(),
        minute_interval_seconds: int | None = None,
        require_shared: bool = False,
        clock=None,
    ) -> None:
        self.fixed_codes = RealtimeWatchlistRepository.normalize(fixed_codes)
        self.clock = lambda: shanghai_now(clock() if clock else None)
        self.watchlists = RealtimeWatchlistRepository()
        self.journal = MarketJournal(clock=self.clock)
        self.quote_cache = RealtimeQuoteCache(journal=self.journal)
        self.plans = DailyCollectionPlan(backend=self.quote_cache.backend)
        if require_shared and self.quote_cache.storage != "redis":
            raise RuntimeError("独立eltdx采集器必须配置可用的 REDIS_URL")
        if MARKET_DATA_NODE_ROLE == "server" and self.quote_cache.storage != "redis":
            raise RuntimeError("服务器内置eltdx采集器需要 Redis；请检查 REDIS_URL")
        self.minute_cache = RealtimeMinuteCache()
        self.provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None)
        from core.data.providers.eltdx_provider import QuoteFailureState
        self._constituent_failures = QuoteFailureState()
        self.calendar = TradeCalendar()
        self.minute_interval_seconds = max(
            int(minute_interval_seconds or ELTDX_MINUTE_SYNC_SECONDS), 30
        )
        self._last_minute_sync = 0.0
        self._minute_sync_lock = Lock()
        self._minute_sync_thread: Thread | None = None
        self._constituent_thread: Thread | None = None
        self._constituent_lock = Lock()
        self._constituent_health = {}
        from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
        self.sector_minutes = SectorQuoteMinuteRepository(backend=self.quote_cache.backend,
            journal=self.journal, clock=lambda: self.clock())
        self._consecutive_failures = 0
        self._stop = False
        self._cycles = 0
        self._prepared_day = ""
        self._session_started_at = self.clock().isoformat(timespec="milliseconds")
        self._last_success_at: datetime | None = None
        from config.settings import ELTDX_BOARD_SHADOW_ENABLED, ELTDX_BOARD_POLL_SECONDS, ELTDX_BOARD_STALE_SECONDS
        from core.realtime.board_plan import BoardCollectionPlan
        from core.realtime.board_quote_store import BoardQuoteStore
        from core.realtime.board_quote_lane import BoardQuoteLane
        from risk.risk_config import RiskConfig
        trend_scoring = RiskConfig.load().entry_sector_mode == "trend_score"
        self.board_lane = BoardQuoteLane(BoardCollectionPlan(),
            BoardQuoteStore(backend=self.quote_cache.backend, journal=self.journal,
                            stale_seconds=ELTDX_BOARD_STALE_SECONDS, expected_interval=ELTDX_BOARD_POLL_SECONDS),
            EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None),
            enabled=ELTDX_BOARD_SHADOW_ENABLED or trend_scoring, interval=ELTDX_BOARD_POLL_SECONDS, clock=self.clock,
            owner=lambda: not self._stop and bool(getattr(self, "_active_lease", None))
                          and self._active_lease.is_owner())

        from config import settings
        from core.realtime.evidence_lane import EvidenceLane
        from core.realtime.evidence_store import EvidenceStore
        self.evidence_lane = EvidenceLane(
            EvidenceStore(self.journal, self.quote_cache.backend),
            lambda: EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None),
            clock=self.clock, owner=lambda: not self._stop and bool(getattr(self, "_active_lease", None))
                and self._active_lease.is_owner(),
            auctions=settings.ELTDX_AUCTION_EVIDENCE_ENABLED, trades=settings.ELTDX_TRADE_EVIDENCE_ENABLED,
            depth=settings.ELTDX_DEPTH_EVIDENCE_ENABLED, interval=settings.ELTDX_EVIDENCE_POLL_SECONDS,
            workers=settings.ELTDX_EVIDENCE_WORKERS, max_pages=settings.ELTDX_TRADE_MAX_PAGES,
            max_pending=settings.ELTDX_EVIDENCE_MAX_PENDING, min_free_mb=settings.ELTDX_EVIDENCE_MIN_FREE_MB)

    def stop(self, *_args) -> None:
        self._stop = True

    def targets(self, *, minutes_only: bool = False) -> list[str]:
        date = self.clock().strftime("%Y%m%d")
        plan = self.plans.load(date)
        if not plan:
            return list(self.fixed_codes)  # Explicit CLI diagnostics only; never expand from stale watchlists.
        codes = [row["code"] for row in plan["rows"]]
        if not minutes_only and plan.get("sector_confirmation_mode", "breadth_required") == "breadth_required":
            codes.extend(c for sector in plan["sectors"] for c in sector["members"])
        return RealtimeWatchlistRepository.normalize(codes)

    def collect_once(self) -> dict:
        if isinstance(getattr(self, "journal", None), MarketJournal):
            date = self.clock().strftime("%Y%m%d")
            if getattr(self, "_prepared_day", "") != date:
                workers = [getattr(self, "_minute_sync_thread", None), getattr(self, "_constituent_thread", None),
                           getattr(getattr(self, "board_lane", None), "_thread", None),
                           getattr(getattr(self, "evidence_lane", None), "_thread", None)]
                if any(worker is not None and worker.is_alive() for worker in workers):
                    return {"ok": False, "message": "等待旧日行情任务结束后切换交易日"}
                from core.realtime.day_lifecycle import RealtimeDayLifecycle
                self._day_cleanup = RealtimeDayLifecycle(self.journal, backend=self.quote_cache.backend,
                                                         clock=self.clock).start_day(date)
                self._prepared_day = date
        if getattr(self, "board_lane", None) is not None and self.board_lane.enabled:
            self.board_lane.schedule(self.plans.load(self.clock().strftime("%Y%m%d")))
        if getattr(self, "evidence_lane", None) is not None:
            self.evidence_lane.schedule(self.plans.load(self.clock().strftime("%Y%m%d")))
        all_codes = self.targets()
        # Keep the execution lane independent of the thousands of sector members.
        codes = self.targets(minutes_only=True)
        priority = set(codes)
        self._schedule_constituent_sync([code for code in all_codes if code not in priority])
        if not codes:
            result = {"ok": False, "count": 0, "message": "当日盘前采集名单缺失或为空，禁止扩大采集范围"}
            self._record_health(result)
            return result

        rows_by_code: dict = {}
        last_error = ""
        attempts = max(int(ELTDX_RETRY_COUNT), 1)
        for attempt in range(attempts):
            try:
                rows_by_code = {code: row for code, row in (self.provider.get_quote_snapshots(codes) or {}).items()
                                if code in priority}
                if rows_by_code:
                    break
                last_error = "行情源未返回数据"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(ELTDX_RETRY_BACKOFF_SECONDS * (2**attempt))

        if not rows_by_code:
            self._consecutive_failures += 1
            result = {
                "ok": False,
                "count": 0,
                "message": last_error or "eltdx批量行情失败",
                "consecutive_failures": self._consecutive_failures,
                "preserved_previous_quotes": True,
            }
            self._record_health(result)
            logger.warning(
                "eltdx批量行情连续失败{}次，保留上一批行情: {}",
                self._consecutive_failures,
                result["message"],
            )
            return result

        try:
            result = self.quote_cache.write_batch(
                rows_by_code.values(), source="eltdx_batch", collector_id=ELTDX_COLLECTOR_ID,
                snapshot_codes=self.targets(minutes_only=True),
                publish_event=False,
            )
            self._archive_sector_minutes(rows_by_code.values())
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            result = {
                "ok": False,
                "count": 0,
                "message": f"行情持久化或实时缓存写入失败: {exc}",
                "consecutive_failures": self._consecutive_failures,
                "preserved_previous_quotes": True,
            }
            self._record_health(result)
            logger.warning("eltdx行情已取得但持久化或缓存写入失败，将继续重试: {}", exc)
            return result

        self._consecutive_failures = 0 if result.get("ok") else self._consecutive_failures + 1
        self._capture_auctions(rows_by_code)
        from core.realtime.market_events import publish_revision
        if result.get("ok"):
            publish_revision(self.quote_cache.backend, self.clock().strftime("%Y%m%d"),
                             kind="quotes", codes=list(rows_by_code))
        if result.get("ok"):
            received = self.clock()
            if (self._last_success_at is None or self._last_success_at.date() != received.date()
                    or (received - self._last_success_at).total_seconds() > 24):
                self._session_started_at = received.isoformat(timespec="milliseconds")
            self._last_success_at = received
        result["requested_count"] = len(codes)
        result["missing_count"] = max(len(codes) - int(result.get("count") or 0), 0)
        rejected = set(result.get("rejected_codes") or [])
        result["missing_codes"] = [code for code in codes if code not in rows_by_code or code in rejected]
        result["source_diagnostics"] = getattr(self.provider, "last_quote_diagnostics", {})
        result["quote_lane"] = "execution_priority"
        plan = self.plans.load(self.clock().strftime("%Y%m%d")) if hasattr(self, "plans") else None
        result["collection_plan_id"] = plan["plan_id"] if plan else ""
        result["archive"] = self.journal.health() if hasattr(self, "journal") else {}
        result["day_cleanup"] = getattr(self, "_day_cleanup", {})
        result["constituents"] = dict(self._constituent_health)
        result["tdx_board_shadow"] = dict(self.board_lane.health) if getattr(self, "board_lane", None) else {}
        result["consecutive_failures"] = self._consecutive_failures
        self._record_health(result)

        now = time.monotonic()
        if result.get("ok") and now - self._last_minute_sync >= self.minute_interval_seconds:
            self._last_minute_sync = now
            self._schedule_minute_sync(self.targets(minutes_only=True))
        return result

    def _capture_auctions(self, rows):
        for code, row in rows.items():
            amount, price = float(row.get("open_amount") or 0), float(row.get("open_price") or 0)
            if amount > 0 and price > 0:
                date = self.clock().strftime("%Y%m%d")
                existing = self.quote_cache.backend.get_json(f"realtime:auction:{date}:{code}") or {}
                if existing.get("开盘价") == price and existing.get("竞价成交额") == amount:
                    continue
                value = {"开盘价": price, "竞价成交额": amount, "竞价成交量": amount / price / 100,
                         "数据源": "eltdx_open", "received_at": row.get("received_at"), "code": code}
                self.journal.append("auction", date, [value])
                self.quote_cache.backend.set_json(f"realtime:auction:{date}:{code}", value, 172800)

    def _archive_sector_minutes(self, rows) -> None:
        try:
            self.sector_minutes.record_batch(rows, now=self.clock())
        except Exception as exc:
            logger.warning("板块分钟价格归档失败，实时行情仍继续: {}", exc)

    def _schedule_constituent_sync(self, codes: list[str]) -> None:
        if not codes:
            return
        with self._constituent_lock:
            if self._constituent_thread and self._constituent_thread.is_alive():
                return
            self._constituent_thread = Thread(target=self._sync_constituents, args=(codes,),
                                              name="eltdx-constituent-quotes", daemon=True)
            self._constituent_thread.start()

    def _sync_constituents(self, codes: list[str]) -> None:
        started = time.monotonic()
        count, failures = 0, 0
        batches = []
        date = self.clock().strftime("%Y%m%d")
        lease = getattr(self, "_active_lease", None)

        def fetch(chunk):
            provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None,
                                     failure_state=self._constituent_failures)
            rows = provider.get_quote_snapshots(chunk) or {}
            return rows, getattr(provider, "last_quote_diagnostics", {})

        chunks = [codes[i:i + 80] for i in range(0, len(codes), 80)]
        with ThreadPoolExecutor(max_workers=ELTDX_CONSTITUENT_WORKERS) as workers:
            futures = {workers.submit(fetch, chunk): chunk for chunk in chunks}
            for future in as_completed(futures):
                if self._stop or self.clock().strftime("%Y%m%d") != date or (lease and not lease.is_owner()):
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    rows, diagnostics = future.result()
                    batches.append(diagnostics)
                    priority = set(self.targets(minutes_only=True))
                    rows = {code: row for code, row in rows.items() if code not in priority}
                    # Publish each received chunk immediately, with its original receipt times.
                    result = self.quote_cache.write_batch(rows.values(), source="eltdx_batch",
                                                         collector_id=ELTDX_COLLECTOR_ID, snapshot_codes=[], publish_event=False)
                    count += int(result.get("count") or 0)
                    failures += len(futures[future]) - int(result.get("count") or 0)
                    self._archive_sector_minutes(rows.values())
                    from core.realtime.market_events import publish_revision
                    if result.get("ok"):
                        publish_revision(self.quote_cache.backend, date, kind="quotes", codes=list(rows))
                    logger.debug("板块采集批次: {}", {"market_date": date, "codes": futures[future],
                        "diagnostics": diagnostics, "written_count": result.get("count", 0)})
                except Exception as exc:
                    failures += len(futures[future])
                    logger.warning("板块报价分批采集失败: {}", exc)
        self._constituent_health = {"requested_count": len(codes), "count": count,
                                    "missing_count": failures,
                                    "batches": batches,
                                    "duration_seconds": round(time.monotonic() - started, 3),
                                    "updated_at": self.clock().isoformat(timespec="milliseconds")}

    def _record_health(self, result: dict) -> None:
        payload = {
            "protocol_version": 2,
            "capabilities": ["lease_proof", "continuous_session", "constituent_snapshots",
                             "dated_collection_plan", "durable_market_spool", "snapshot_hot_window_v2", "market_revisions"],
            "collector_id": ELTDX_COLLECTOR_ID,
            "source": "eltdx_batch",
            "evidence": dict(self.evidence_lane.health) if getattr(self, "evidence_lane", None) else {},
            "lease_owned": bool(getattr(self, "_lease_owned", False)),
            "updated_at": self.clock().isoformat(timespec="milliseconds"),
            "session_started_at": self._session_started_at,
            "last_success_at": self._last_success_at.isoformat(timespec="milliseconds") if self._last_success_at else "",
            **result,
        }
        try:
            self.quote_cache.backend.set_json(
                self.HEALTH_KEY, payload, int(REALTIME_QUOTE_TTL_SECONDS)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集器健康状态写入实时缓存失败: {}", exc)

    def _sync_minutes(self, codes: list[str]) -> None:
        trade_date = self.clock().strftime("%Y%m%d")
        written = 0
        failures = 0
        details = []
        started = time.monotonic()
        for code in codes:
            stock_started = time.monotonic()
            try:
                frame = self.provider.get_minute_ohlcv(code, trade_date)
                if not frame.empty:
                    self.journal.append("minutes", trade_date, [{"code": code,
                        "received_at": self.clock().isoformat(), "bars": json.loads(frame.to_json(orient="records", date_format="iso"))}])
                written += self.minute_cache.write(trade_date, code, frame)
                failures += int(frame.empty)
                details.append({"code": code, "rows": len(frame), "ok": not frame.empty,
                                "source_diagnostics": dict(getattr(self.provider, "last_minute_diagnostics", {})),
                                "duration_ms": round((time.monotonic() - stock_started) * 1000, 3)})
            except Exception as exc:  # noqa: BLE001
                failures += 1
                details.append({"code": code, "ok": False, "category": type(exc).__name__,
                                "duration_ms": round((time.monotonic() - stock_started) * 1000, 3)})
                logger.debug("eltdx分钟序列失败 {}/{}: {}", code, trade_date, exc)
        logger.info("eltdx分钟序列同步: 股票={} 行={} 失败={}", len(codes), written, failures)
        self.quote_cache.backend.set_json("realtime:minute-sync:health", {"stocks": details,
            "updated_at": self.clock().isoformat(), "duration_ms": round((time.monotonic() - started) * 1000, 3)}, 120)
        from core.realtime.market_events import publish_revision
        publish_revision(self.quote_cache.backend, trade_date, kind="minutes", codes=codes)

    def _schedule_minute_sync(self, codes: list[str]) -> None:
        """Run minute history separately so snapshots keep their 3-second cadence."""
        with self._minute_sync_lock:
            if self._minute_sync_thread and self._minute_sync_thread.is_alive():
                return
            self._minute_sync_thread = Thread(
                target=self._sync_minutes,
                args=(list(codes),),
                name="eltdx-minute-sync",
                daemon=True,
            )
            self._minute_sync_thread.start()

    def run(self, *, once: bool = False) -> None:
        """Keep trying to own the shared collector lease until stopped."""
        while not self._stop:
            lease = TaskLease.acquire(self.LEASE_NAME, 30)
            if lease is None:
                if once:
                    logger.info("已有eltdx实时采集实例，本次单次任务跳过")
                    return
                self._wait(15.0)
                continue
            try:
                self._lease_owned = True
                self._run_as_owner(lease, once=once)
            finally:
                self._lease_owned = False
                lease.release()
            if once:
                return

    def _run_as_owner(self, lease: TaskLease, *, once: bool) -> None:
        self._active_lease = lease
        logger.info("eltdx实时采集已取得唯一任务锁")
        while not self._stop:
            if not lease.is_owner():
                self._lease_owned = False
                self._record_health({"ok": False, "message": "采集器租约已丢失"})
                break
            if not once and not self._is_collection_session():
                self._wait(15.0)
                continue
            started = time.monotonic()
            try:
                result = self.collect_once()
            except Exception as exc:  # noqa: BLE001
                self._consecutive_failures += 1
                result = {
                    "ok": False,
                    "count": 0,
                    "message": f"采集循环异常: {exc}",
                    "consecutive_failures": self._consecutive_failures,
                }
                self._record_health(result)
                logger.exception("eltdx采集循环异常，进程保持运行并在下一轮重试")
            self._cycles += 1
            if not result.get("ok") or self._cycles % 20 == 1:
                logger.info("eltdx采集: {}", result)
            if once:
                return
            self._wait(max(ELTDX_POLL_INTERVAL_SECONDS - (time.monotonic() - started), 0.2))

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(float(seconds), 0.0)
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.5, max(deadline - time.monotonic(), 0.0)))

    def _is_collection_session(self) -> bool:
        now = self.clock()
        trade_date = now.strftime("%Y%m%d")
        if not self.calendar.is_trade_date(trade_date):
            return False
        at = now.strftime("%H:%M:%S")
        return "09:15:00" <= at <= "11:30:15" or "13:00:00" <= at <= "15:05:00"


__all__ = ["EltdxQuoteCollector"]
