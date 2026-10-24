"""Short-lived storage for high-frequency realtime quote snapshots."""
from __future__ import annotations

import json
from datetime import datetime
from threading import RLock
from typing import Any, Dict, Iterable, List

from loguru import logger

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now


class RealtimeSnapshotRepository:
    """Keep a bounded snapshot window in Redis with an in-memory fallback."""

    def __init__(self, *, max_items: int = 0, ttl_seconds: int = 0, clock=None, backend=None) -> None:
        if max_items <= 0 or ttl_seconds <= 0:
            from config.settings import (
                REALTIME_SNAPSHOT_MAX_ITEMS,
                REALTIME_SNAPSHOT_TTL_SECONDS,
            )

            max_items = max_items or REALTIME_SNAPSHOT_MAX_ITEMS
            ttl_seconds = ttl_seconds or REALTIME_SNAPSHOT_TTL_SECONDS
        self.max_items = max(int(max_items), 20)
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self.backend = backend or get_shared_state_backend()
        self.clock = clock or shanghai_now
        self.window_seconds = 180
        self._known_keys: Dict[str, set[str]] = {}
        self._lock = RLock()

    @property
    def storage(self) -> str:
        return "redis" if bool(getattr(self.backend, "is_shared", False)) else "memory"

    def append_batch(self, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        payloads = []
        for source in rows or []:
            row = self._compact(source)
            code = row.get("code", "")
            if code and row.get("last_price", 0) > 0:
                payloads.append((self._key(trade_date, code), json.dumps(row, ensure_ascii=False)))
        if not payloads:
            return
        if self.storage == "memory":
            self._prune_memory_dates(str(trade_date)[:8], [key for key, _ in payloads])

        client = getattr(self.backend, "client", None)
        key_builder = getattr(self.backend, "_key", None)
        if client is not None and callable(key_builder):
            try:
                pipe = client.pipeline(transaction=False)
                cutoff = shanghai_now(self.clock()).timestamp() - self.window_seconds
                for key, raw in payloads:
                    full = key_builder(key)
                    row = json.loads(raw)
                    score = self._timestamp(row)
                    if score is None or score > shanghai_now(self.clock()).timestamp() + 1:
                        continue
                    pipe.zadd(full, {raw: score})
                    pipe.zremrangebyscore(full, "-inf", cutoff)
                    pipe.expire(full, self.ttl_seconds)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[RealtimeSnapshotRepository] Redis批量写入失败: {exc}")
                raise

        for key, raw in payloads:
            self.backend.append_list(key, raw, 0)
        for key in {key for key, _ in payloads}:
            rows, _ = self.backend.read_list(key, 0)
            retained = [raw for raw in rows if self._in_window(json.loads(raw))]
            self.backend.clear_list(key)
            for raw in retained:
                self.backend.append_list(key, raw, 0)

    def _prune_memory_dates(self, trade_date: str, keys: List[str]) -> None:
        with self._lock:
            self._known_keys.setdefault(trade_date, set()).update(keys)
            dates = sorted(self._known_keys)
            for old_date in dates[:-2]:
                for key in self._known_keys.pop(old_date, set()):
                    self.backend.delete(key)

    def read(self, trade_date: str, code: str) -> List[Dict[str, Any]]:
        try:
            if self.storage == "redis":
                client = self.backend.client
                full = self.backend._key(self._key(trade_date, code))
                now = shanghai_now(self.clock()).timestamp()
                rows = client.zrangebyscore(full, f"({now - self.window_seconds}", now + 1)
            else:
                rows, _ = self.backend.read_list(self._key(trade_date, code), 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[RealtimeSnapshotRepository] 快照读取失败 {code}: {exc}")
            return []
        out: List[Dict[str, Any]] = []
        for raw in rows:
            try:
                item = json.loads(raw)
                if isinstance(item, dict) and self._in_window(item):
                    out.append(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return out

    @staticmethod
    def _key(trade_date: str, code: str) -> str:
        code6 = normalize_stock_code(code, add_suffix=False)
        return f"realtime:snapshots:v2:{str(trade_date)[:8]}:{code6}"

    @staticmethod
    def _timestamp(row):
        try:
            return shanghai_now(datetime.fromisoformat(str(row.get("received_at") or ""))).timestamp()
        except (ValueError, TypeError):
            return None

    def _in_window(self, row):
        stamp = self._timestamp(row)
        age = shanghai_now(self.clock()).timestamp() - stamp if stamp is not None else float("inf")
        return -1 <= age < self.window_seconds

    @staticmethod
    def _compact(source: Dict[str, Any]) -> Dict[str, Any]:
        fields = (
            "code", "date", "time", "last_price", "open_price", "pre_close",
            "high_price", "low_price", "bid1", "ask1", "bid_vol1", "ask_vol1",
            "vol_hand", "amount_yuan", "delta_volume", "delta_amount", "source",
            "received_at", "quality_ok", "collector_id", "schema_version", "depth",
        )
        return {key: source.get(key) for key in fields if source.get(key) is not None}


__all__ = ["RealtimeSnapshotRepository"]
