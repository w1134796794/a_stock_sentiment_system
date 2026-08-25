"""Windows eltdx collector: batch snapshots and minute bars -> Redis."""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.trade_calendar import TradeCalendar  # noqa: E402
from config.settings import (  # noqa: E402
    ELTDX_COLLECTOR_ID,
    ELTDX_HOST,
    ELTDX_MINUTE_SYNC_SECONDS,
    ELTDX_POLL_INTERVAL_SECONDS,
    ELTDX_RETRY_BACKOFF_SECONDS,
    ELTDX_RETRY_COUNT,
    ELTDX_TIMEOUT_SECONDS,
    MARKET_DATA_NODE_ROLE,
    REALTIME_QUOTE_TTL_SECONDS,
    REDIS_URL,
)
from core.data.providers.eltdx_provider import EltdxProvider  # noqa: E402
from core.portfolio.holding_repository import HoldingRepository  # noqa: E402
from core.realtime.minute_cache import RealtimeMinuteCache  # noqa: E402
from core.realtime.quote_cache import RealtimeQuoteCache  # noqa: E402
from core.realtime.watchlist_repository import RealtimeWatchlistRepository  # noqa: E402


class EltdxQuoteCollector:
    HEALTH_KEY = RealtimeQuoteCache.COLLECTOR_HEALTH_KEY

    def __init__(self, *, fixed_codes=(), minute_interval_seconds: int | None = None) -> None:
        if not REDIS_URL:
            raise RuntimeError("eltdx采集器必须配置 REDIS_URL")
        if MARKET_DATA_NODE_ROLE not in {"collector", "workstation"}:
            raise RuntimeError("Windows采集器需要 MARKET_DATA_NODE_ROLE=collector")
        self.fixed_codes = RealtimeWatchlistRepository.normalize(fixed_codes)
        self.watchlists = RealtimeWatchlistRepository()
        self.quote_cache = RealtimeQuoteCache()
        if self.quote_cache.storage != "redis":
            raise RuntimeError("eltdx采集器未连接到 Redis，拒绝写入进程内存")
        self.minute_cache = RealtimeMinuteCache()
        self.provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None)
        self.calendar = TradeCalendar()
        self.minute_interval_seconds = max(
            int(minute_interval_seconds or ELTDX_MINUTE_SYNC_SECONDS), 30
        )
        self._last_minute_sync = 0.0
        self._consecutive_failures = 0
        self._stop = False
        self._cycles = 0

    def stop(self, *_args) -> None:
        self._stop = True

    def targets(self) -> list[str]:
        holding_codes = []
        try:
            holding_codes = [row.get("code", "") for row in HoldingRepository().list_positions(status="open")]
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
                "eltdx批量行情连续失败{}次，保留Redis中上一批行情: {}",
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
                "message": f"Redis写入失败: {exc}",
                "consecutive_failures": self._consecutive_failures,
                "preserved_previous_quotes": True,
            }
            self._record_health(result)
            logger.warning("eltdx行情已取得但Redis写入失败，将继续重试: {}", exc)
            return result
        if result.get("ok"):
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        result["requested_count"] = len(codes)
        result["missing_count"] = max(len(codes) - int(result.get("count") or 0), 0)
        result["consecutive_failures"] = self._consecutive_failures
        self._record_health(result)

        now = time.monotonic()
        if result.get("ok") and now - self._last_minute_sync >= self.minute_interval_seconds:
            self._sync_minutes(codes)
            self._last_minute_sync = now
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
            logger.warning("采集器健康状态写入Redis失败: {}", exc)

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

    def run(self, *, once: bool = False) -> None:
        while not self._stop:
            if not once and not self._is_collection_session():
                time.sleep(15)
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
            delay = max(ELTDX_POLL_INTERVAL_SECONDS - (time.monotonic() - started), 0.2)
            time.sleep(delay)

    def _is_collection_session(self) -> bool:
        now = datetime.now()
        trade_date = now.strftime("%Y%m%d")
        if not self.calendar.is_trade_date(trade_date):
            return False
        return "09:15:00" <= now.strftime("%H:%M:%S") <= "15:05:00"


def main() -> None:
    parser = argparse.ArgumentParser(description="Windows eltdx实时行情采集器")
    parser.add_argument("--codes", default="", help="额外固定代码，逗号分隔")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--minute-interval", type=int, default=ELTDX_MINUTE_SYNC_SECONDS)
    args = parser.parse_args()
    collector = EltdxQuoteCollector(
        fixed_codes=[part.strip() for part in args.codes.split(",") if part.strip()],
        minute_interval_seconds=args.minute_interval,
    )
    signal.signal(signal.SIGINT, collector.stop)
    signal.signal(signal.SIGTERM, collector.stop)
    collector.run(once=args.once)


if __name__ == "__main__":
    main()
