"""Short-lived storage for high-frequency realtime quote snapshots."""
from __future__ import annotations

import json
from threading import RLock
from typing import Any, Dict, Iterable, List

from loguru import logger

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code


class RealtimeSnapshotRepository:
    """Keep a bounded snapshot window in Redis with an in-memory fallback."""

    def __init__(self, *, max_items: int = 0, ttl_seconds: int = 0) -> None:
        if max_items <= 0 or ttl_seconds <= 0:
            from config.settings import (
                REALTIME_SNAPSHOT_MAX_ITEMS,
                REALTIME_SNAPSHOT_TTL_SECONDS,
            )

            max_items = max_items or REALTIME_SNAPSHOT_MAX_ITEMS
            ttl_seconds = ttl_seconds or REALTIME_SNAPSHOT_TTL_SECONDS
        self.max_items = max(int(max_items), 20)
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self.backend = get_shared_state_backend()
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
                for key, raw in payloads:
                    full = key_builder(key)
                    pipe.rpush(full, raw)
                    pipe.ltrim(full, -self.max_items, -1)
                    pipe.expire(full, self.ttl_seconds)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[RealtimeSnapshotRepository] Redis批量写入失败: {exc}")

        for key, raw in payloads:
            self.backend.append_list(key, raw, self.max_items)

    def _prune_memory_dates(self, trade_date: str, keys: List[str]) -> None:
        with self._lock:
            self._known_keys.setdefault(trade_date, set()).update(keys)
            dates = sorted(self._known_keys)
            for old_date in dates[:-2]:
                for key in self._known_keys.pop(old_date, set()):
                    self.backend.delete(key)

    def read(self, trade_date: str, code: str) -> List[Dict[str, Any]]:
        try:
            rows, _ = self.backend.read_list(self._key(trade_date, code), 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[RealtimeSnapshotRepository] 快照读取失败 {code}: {exc}")
            return []
        out: List[Dict[str, Any]] = []
        for raw in rows[-self.max_items:]:
            try:
                item = json.loads(raw)
                if isinstance(item, dict):
                    out.append(item)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return out

    @staticmethod
    def _key(trade_date: str, code: str) -> str:
        code6 = normalize_stock_code(code, add_suffix=False)
        return f"realtime:snapshots:{str(trade_date)[:8]}:{code6}"

    @staticmethod
    def _compact(source: Dict[str, Any]) -> Dict[str, Any]:
        fields = (
            "code", "date", "time", "last_price", "open_price", "pre_close",
            "high_price", "low_price", "bid1", "ask1", "bid_vol1", "ask_vol1",
            "vol_hand", "amount_yuan", "delta_volume", "delta_amount", "source",
            "received_at", "quality_ok", "collector_id", "schema_version",
        )
        return {key: source.get(key) for key in fields if source.get(key) is not None}


__all__ = ["RealtimeSnapshotRepository"]
