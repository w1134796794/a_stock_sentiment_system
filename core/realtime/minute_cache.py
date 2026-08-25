"""Redis-backed minute bars produced by the Windows market-data collector."""
from __future__ import annotations

from typing import Any

import pandas as pd

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code


class RealtimeMinuteCache:
    def __init__(self, backend: Any = None, *, ttl_seconds: int = 172800) -> None:
        self.backend = backend or get_shared_state_backend()
        self.ttl_seconds = max(int(ttl_seconds), 3600)

    @staticmethod
    def _key(trade_date: str, code: str) -> str:
        code6 = normalize_stock_code(code, add_suffix=False)
        return f"realtime:minutes:{str(trade_date).replace('-', '')[:8]}:{code6}"

    def write(self, trade_date: str, code: str, rows: Any) -> int:
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows or [])
        if frame.empty:
            return 0
        records = frame.where(pd.notna(frame), None).to_dict(orient="records")
        self.backend.set_json(self._key(trade_date, code), records, self.ttl_seconds)
        return len(records)

    def read(self, trade_date: str, code: str) -> pd.DataFrame:
        rows = self.backend.get_json(self._key(trade_date, code)) or []
        return pd.DataFrame(rows if isinstance(rows, list) else [])


__all__ = ["RealtimeMinuteCache"]
