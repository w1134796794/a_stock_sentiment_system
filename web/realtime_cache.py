"""Realtime payload cache shared through Redis in production."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from threading import Event, RLock
from time import monotonic, sleep, time
from typing import Any, Callable, Dict, Hashable, Optional

from core.infrastructure.shared_state import get_shared_state_backend


@dataclass
class _Entry:
    value: Any
    stored_at: float
    updated_at: str


class RealtimePayloadCache:
    """Cache API payloads and collapse concurrent loads for the same key."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 15.0,
        wait_timeout_seconds: float = 20.0,
        backend: Any = None,
    ):
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.wait_timeout_seconds = max(float(wait_timeout_seconds), 1.0)
        self.backend = backend or get_shared_state_backend()
        self._entries: Dict[Hashable, _Entry] = {}
        self._inflight: Dict[Hashable, Event] = {}
        self._errors: Dict[Hashable, str] = {}
        self._lock = RLock()

    def get_or_load(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        return self._load(key, loader, force=False)

    def get(self, key: Hashable) -> Any:
        """Return the latest payload regardless of age."""
        if self.backend.is_shared:
            envelope = self.backend.get_json(self._cache_key(key))
            return deepcopy(envelope.get("value")) if envelope else None
        with self._lock:
            entry = self._entries.get(key)
        return deepcopy(entry.value) if entry is not None else None

    def refresh(self, key: Hashable, loader: Callable[[], Any]) -> Any:
        return self._load(key, loader, force=True)

    def _load(self, key: Hashable, loader: Callable[[], Any], *, force: bool) -> Any:
        if self.backend.is_shared:
            return self._load_shared(key, loader, force=force)
        return self._load_local(key, loader, force=force)

    def _load_local(self, key: Hashable, loader: Callable[[], Any], *, force: bool) -> Any:
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

    @staticmethod
    def _cache_key(key: Hashable) -> str:
        raw = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"realtime:payload:{digest}"

    def _load_shared(self, key: Hashable, loader: Callable[[], Any], *, force: bool) -> Any:
        cache_key = self._cache_key(key)
        lock_key = f"realtime:{cache_key.rsplit(':', 1)[-1]}"
        envelope = self.backend.get_json(cache_key)
        stale = envelope.get("value") if envelope else None
        stored_at = float((envelope or {}).get("stored_at") or 0.0)
        if not force and envelope and time() - stored_at <= self.ttl_seconds:
            return deepcopy(stale)

        lock_ttl = max(int(self.wait_timeout_seconds * 2), 30)
        token = self.backend.acquire_lock(lock_key, lock_ttl)
        if not token:
            if envelope is not None:
                return deepcopy(stale)
            deadline = monotonic() + self.wait_timeout_seconds
            while monotonic() < deadline:
                sleep(0.05)
                envelope = self.backend.get_json(cache_key)
                if envelope is not None:
                    return deepcopy(envelope.get("value"))
            raise RuntimeError("实时数据共享缓存刷新超时")

        try:
            # Another worker may have completed between our initial read and lock acquisition.
            latest = self.backend.get_json(cache_key)
            latest_at = float((latest or {}).get("stored_at") or 0.0)
            if not force and latest and time() - latest_at <= self.ttl_seconds:
                return deepcopy(latest.get("value"))
            value = loader()
            now = time()
            self.backend.set_json(
                cache_key,
                {
                    "value": value,
                    "stored_at": now,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                },
                ttl_seconds=max(int(self.ttl_seconds * 20), 300),
            )
            with self._lock:
                self._errors.pop(key, None)
            return deepcopy(value)
        except Exception as exc:
            with self._lock:
                self._errors[key] = str(exc)
            if stale is not None:
                return deepcopy(stale)
            raise
        finally:
            self.backend.release_lock(lock_key, token)

    def stats(self) -> Dict[str, Any]:
        if self.backend.is_shared:
            with self._lock:
                errors = list(self._errors.values())
            return {
                "entries": None,
                "inflight": None,
                "ttl_seconds": self.ttl_seconds,
                "latest_updated_at": "",
                "oldest_age_seconds": 0.0,
                "last_error": errors[-1] if errors else "",
                "storage": "redis",
            }
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
            "storage": "memory",
        }
