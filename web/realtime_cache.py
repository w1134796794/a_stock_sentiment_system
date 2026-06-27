"""Thread-safe shared payload cache for realtime web endpoints."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from threading import Event, RLock
from time import monotonic
from typing import Any, Callable, Dict, Hashable, Optional


@dataclass
class _Entry:
    value: Any
    stored_at: float
    updated_at: str


class RealtimePayloadCache:
    """Cache API payloads and collapse concurrent loads for the same key."""

    def __init__(self, *, ttl_seconds: float = 15.0, wait_timeout_seconds: float = 20.0):
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.wait_timeout_seconds = max(float(wait_timeout_seconds), 1.0)
        self._entries: Dict[Hashable, _Entry] = {}
        self._inflight: Dict[Hashable, Event] = {}
        self._errors: Dict[Hashable, str] = {}
        self._lock = RLock()

    def get_or_load(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        return self._load(key, loader, force=False)

    def refresh(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        return self._load(key, loader, force=True)

    def _load(self, key: Hashable, loader: Callable[[], Any], *, force: bool) -> Any:
        leader = False
        stale: Optional[_Entry]
        with self._lock:
            stale = self._entries.get(key)
            if not force and stale and monotonic() - stale.stored_at <= self.ttl_seconds:
                return deepcopy(stale.value)

            event = self._inflight.get(key)
            if event is None:
                event = Event()
                self._inflight[key] = event
                leader = True
            elif stale is not None:
                return deepcopy(stale.value)

        if not leader:
            event.wait(self.wait_timeout_seconds)
            with self._lock:
                entry = self._entries.get(key)
                error = self._errors.get(key, "")
            if entry is not None:
                return deepcopy(entry.value)
            raise RuntimeError(error or "实时数据缓存刷新超时")

        try:
            value = loader()
            entry = _Entry(
                value=deepcopy(value),
                stored_at=monotonic(),
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            with self._lock:
                self._entries[key] = entry
                self._errors.pop(key, None)
            return deepcopy(value)
        except Exception as exc:
            with self._lock:
                self._errors[key] = str(exc)
            if stale is not None:
                return deepcopy(stale.value)
            raise
        finally:
            with self._lock:
                current = self._inflight.pop(key, None)
                if current is not None:
                    current.set()

    def stats(self) -> Dict[str, Any]:
        now = monotonic()
        with self._lock:
            entries = list(self._entries.values())
            errors = list(self._errors.values())
            inflight = len(self._inflight)
        latest = max((entry.updated_at for entry in entries), default="")
        oldest_age = max((now - entry.stored_at for entry in entries), default=0.0)
        return {
            "entries": len(entries),
            "inflight": inflight,
            "ttl_seconds": self.ttl_seconds,
            "latest_updated_at": latest,
            "oldest_age_seconds": round(oldest_age, 2),
            "last_error": errors[-1] if errors else "",
        }
