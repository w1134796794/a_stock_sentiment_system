"""Read-only realtime quote provider used by Web and Linux workers."""
from __future__ import annotations

import pandas as pd

from core.realtime.minute_cache import RealtimeMinuteCache
from core.realtime.quote_cache import RealtimeQuoteCache


class RedisQuoteProvider:
    def __init__(
        self,
        cache: RealtimeQuoteCache | None = None,
        minute_cache: RealtimeMinuteCache | None = None,
    ) -> None:
        self.cache = cache or RealtimeQuoteCache()
        self.minute_cache = minute_cache or RealtimeMinuteCache()
        from config.settings import MARKET_DATA_NODE_ROLE

        if MARKET_DATA_NODE_ROLE == "server" and self.cache.storage != "redis":
            raise RuntimeError("服务器行情只允许读取 Redis；请检查 REDIS_URL 和 Redis 连通性")

    @property
    def available(self) -> bool:
        return self.cache.storage == "redis"

    def get_quote_snapshot(self, ts_code: str) -> dict:
        return self.cache.read_one(ts_code)

    def get_quote_snapshots(self, ts_codes) -> dict:
        return self.cache.read_batch(ts_codes)

    def get_snapshot_ticks(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return pd.DataFrame(self.cache.read_ticks(ts_code, trade_date))

    def get_minute_bars(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        return self.minute_cache.read(trade_date, ts_code)

    def health(self) -> dict:
        return self.cache.health()


__all__ = ["RedisQuoteProvider"]
