"""Shared realtime watchlist exchanged between Web and the quote collector."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, List

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code


class RealtimeWatchlistRepository:
    INDEX_KEY = "realtime:watchlists:index"

    def __init__(self, backend: Any = None, *, ttl_seconds: int = 86400) -> None:
        self.backend = backend or get_shared_state_backend()
        self.ttl_seconds = max(int(ttl_seconds), 60)

    @staticmethod
    def _key(source: str) -> str:
        return f"realtime:watchlists:{str(source or 'default').strip()}"

    @staticmethod
    def normalize(codes: Iterable[str]) -> List[str]:
        result: List[str] = []
        seen = set()
        for value in codes or []:
            code = normalize_stock_code(value, add_suffix=False)
            if len(code) == 6 and code.isdigit() and code not in seen:
                seen.add(code)
                result.append(code)
        return result

    def publish(self, source: str, codes: Iterable[str]) -> dict:
        source = str(source or "default").strip()
        normalized = self.normalize(codes)
        index = self.backend.get_json(self.INDEX_KEY) or []
        if source not in index:
            index = [*index, source]
        payload = {
            "source": source,
            "codes": normalized,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.backend.set_json(self._key(source), payload, self.ttl_seconds)
        self.backend.set_json(self.INDEX_KEY, index, self.ttl_seconds)
        return payload

    def read_all(self) -> List[str]:
        sources = self.backend.get_json(self.INDEX_KEY) or []
        codes: List[str] = []
        for source in sources:
            payload = self.backend.get_json(self._key(str(source))) or {}
            codes.extend(payload.get("codes") or [])
        return self.normalize(codes)


__all__ = ["RealtimeWatchlistRepository"]
