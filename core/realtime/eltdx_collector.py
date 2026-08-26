"""Self-contained eltdx quote collector shared by Web and the CLI runner."""
from __future__ import annotations

import time
from datetime import datetime
from threading import Lock, Thread

from loguru import logger

from backtest.trade_calendar import TradeCalendar
from config.settings import (
    ELTDX_COLLECTOR_ID,
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
    ) -> None:
        self.fixed_codes = RealtimeWatchlistRepository.normalize(fixed_codes)
        self.watchlists = RealtimeWatchlistRepository()
        self.quote_cache = RealtimeQuoteCache()
        if require_shared and self.quote_cache.storage != "redis":
            raise RuntimeError("独立eltdx采集器必须配置可用的 REDIS_URL")
        if MARKET_DATA_NODE_ROLE == "server" and self.quote_cache.storage != "redis":
            raise RuntimeError("服务器内置eltdx采集器需要 Redis；请检查 REDIS_URL")
        self.minute_cache = RealtimeMinuteCache()
        self.provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None)
        self.calendar = TradeCalendar()
        self.minute_interval_seconds = max(
            int(minute_interval_seconds or ELTDX_MINUTE_SYNC_SECONDS), 30
        )
        self._last_minute_sync = 0.0
        self._minute_sync_lock = Lock()
        self._minute_sync_thread: Thread | None = None
        self._consecutive_failures = 0
        self._stop = False
        self._cycles = 0

    def stop(self, *_args) -> None:
        self._stop = True

    def targets(self) -> list[str]:
        holding_codes = []
        try:
            holding_codes = [
                row.get("code", "")
                for row in HoldingRepository().list_positions(status="open")
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取持仓观察列表失败: {}", exc)
        return RealtimeWatchlistRepository.normalize(
            [*self.fixed_codes, *self.watchlists.read_all(), *holding_codes]
        )

    def collect_once(self) -> dict:
        codes = self.targets()
        if not codes:
            result = {"ok": False, "count": 0, "message": "观察列表为空"}
            self._record_health(result)
            return result

        rows_by_code: dict = {}
        last_error = ""
        attempts = max(int(ELTDX_RETRY_COUNT), 1)
        for attempt in range(attempts):
            try:
                rows_by_code = self.provider.get_quote_snapshots(codes) or {}
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
                rows_by_code.values(), source="eltdx_batch", collector_id=ELTDX_COLLECTOR_ID
            )
        except Exception as exc:  # noqa: BLE001
            self._consecutive_failures += 1
            result = {
                "ok": False,
                "count": 0,
                "message": f"实时缓存写入失败: {exc}",
                "consecutive_failures": self._consecutive_failures,
                "preserved_previous_quotes": True,
            }
            self._record_health(result)
            logger.warning("eltdx行情已取得但实时缓存写入失败，将继续重试: {}", exc)
            return result

        self._consecutive_failures = 0 if result.get("ok") else self._consecutive_failures + 1
        result["requested_count"] = len(codes)
        result["missing_count"] = max(len(codes) - int(result.get("count") or 0), 0)
        result["consecutive_failures"] = self._consecutive_failures
        self._record_health(result)

        now = time.monotonic()
        if result.get("ok") and now - self._last_minute_sync >= self.minute_interval_seconds:
            self._last_minute_sync = now
            self._schedule_minute_sync(codes)
        return result

    def _record_health(self, result: dict) -> None:
        payload = {
            "collector_id": ELTDX_COLLECTOR_ID,
            "source": "eltdx_batch",
            "updated_at": datetime.now().isoformat(timespec="milliseconds"),
            **result,
        }
        try:
            self.quote_cache.backend.set_json(
                self.HEALTH_KEY, payload, int(REALTIME_QUOTE_TTL_SECONDS)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("采集器健康状态写入实时缓存失败: {}", exc)

    def _sync_minutes(self, codes: list[str]) -> None:
        trade_date = datetime.now().strftime("%Y%m%d")
        written = 0
        failures = 0
        for code in codes:
            try:
                frame = self.provider.get_minute_bars(code, trade_date)
                written += self.minute_cache.write(trade_date, code, frame)
                failures += int(frame.empty)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.debug("eltdx分钟序列失败 {}/{}: {}", code, trade_date, exc)
        logger.info("eltdx分钟序列同步: 股票={} 行={} 失败={}", len(codes), written, failures)

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
                self._run_as_owner(once=once)
            finally:
                lease.release()
            if once:
                return

    def _run_as_owner(self, *, once: bool) -> None:
        logger.info("eltdx实时采集已取得唯一任务锁")
        while not self._stop:
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
        now = datetime.now()
        trade_date = now.strftime("%Y%m%d")
        if not self.calendar.is_trade_date(trade_date):
            return False
        return "09:15:00" <= now.strftime("%H:%M:%S") <= "15:05:00"


__all__ = ["EltdxQuoteCollector"]
