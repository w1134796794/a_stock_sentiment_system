"""Language-neutral realtime quote contract shared by collector and Web."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import QuoteSnapshot, normalize_stock_code
from core.realtime.snapshot_repository import RealtimeSnapshotRepository


class RealtimeQuoteCache:
    """Store latest normalized quotes and a short rolling snapshot sequence."""

    META_KEY = "realtime:quotes:meta"
    COLLECTOR_HEALTH_KEY = "realtime:collector:health"

    def __init__(self, backend: Any = None, snapshot_repository: Any = None) -> None:
        from config.settings import REALTIME_QUOTE_STALE_SECONDS, REALTIME_QUOTE_TTL_SECONDS

        self.backend = backend or get_shared_state_backend()
        self.snapshots = snapshot_repository or RealtimeSnapshotRepository()
        self.stale_after_seconds = float(REALTIME_QUOTE_STALE_SECONDS)
        self.ttl_seconds = int(REALTIME_QUOTE_TTL_SECONDS)

    @property
    def storage(self) -> str:
        return "redis" if bool(getattr(self.backend, "is_shared", False)) else "memory"

    @staticmethod
    def _quote_key(code: str) -> str:
        return f"realtime:quotes:latest:{normalize_stock_code(code, add_suffix=False)}"

    def write_batch(
        self,
        rows: Iterable[Dict[str, Any]],
        *,
        source: str,
        collector_id: str,
    ) -> Dict[str, Any]:
        now = datetime.now()
        values: Dict[str, Dict[str, Any]] = {}
        snapshot_rows: List[Dict[str, Any]] = []
        trade_dates = set()
        prepared: List[tuple[QuoteSnapshot, Dict[str, Any]]] = []
        for raw in rows or []:
            payload = dict(raw or {})
            payload.setdefault("source", source)
            payload.setdefault("received_at", now.isoformat(timespec="milliseconds"))
            item = QuoteSnapshot.from_raw(payload, stale_after_seconds=self.stale_after_seconds)
            if not item.code or item.last_price <= 0:
                continue
            normalized = item.to_dict(include_raw=False)
            normalized["collector_id"] = collector_id
            normalized["schema_version"] = 1
            prepared.append((item, normalized))
            if item.date:
                trade_dates.add(str(item.date).replace("-", "")[:8])

        if not prepared:
            return {"ok": False, "count": 0, "message": "行情源未返回有效快照"}
        keys = [self._quote_key(item.code) for item, _ in prepared]
        previous = self.backend.get_many_json(keys)
        for (item, normalized), key in zip(prepared, keys, strict=True):
            old = previous.get(key) or {}
            same_session = str(old.get("date") or "") == str(item.date or "")
            old_time = str(old.get("time") or "")
            source_time_ok = not same_session or not old_time or str(item.time or "") >= old_time
            delta_volume = (
                max(item.vol_hand - float(old.get("vol_hand") or 0), 0.0)
                if same_session else 0.0
            )
            delta_amount = (
                max(item.amount_yuan - float(old.get("amount_yuan") or 0), 0.0)
                if same_session else 0.0
            )
            normalized.update({
                "delta_volume": delta_volume,
                "delta_amount": delta_amount,
                "quality_ok": bool(
                    source_time_ok and not item.is_stale and item.last_price > 0
                ),
            })
            values[key] = normalized
            snapshot_rows.append(normalized)
        self.backend.set_many_json(values, self.ttl_seconds)
        trade_date = max(trade_dates) if trade_dates else now.strftime("%Y%m%d")
        self.snapshots.append_batch(trade_date, snapshot_rows)
        meta = {
            "schema_version": 1,
            "source": source,
            "collector_id": collector_id,
            "trade_date": trade_date,
            "quote_count": len(values),
            "updated_at": now.isoformat(timespec="milliseconds"),
            "storage": self.storage,
        }
        self.backend.set_json(self.META_KEY, meta, self.ttl_seconds)
        return {"ok": True, "count": len(values), **meta}

    def read_batch(self, codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        code_list = []
        seen = set()
        for code in codes or []:
            code6 = normalize_stock_code(code, add_suffix=False)
            if code6 and code6 not in seen:
                seen.add(code6)
                code_list.append(code6)
        keys = [self._quote_key(code) for code in code_list]
        values = self.backend.get_many_json(keys)
        out: Dict[str, Dict[str, Any]] = {}
        for code, key in zip(code_list, keys, strict=True):
            raw = values.get(key)
            if not isinstance(raw, dict):
                continue
            item = QuoteSnapshot.from_raw(raw, stale_after_seconds=self.stale_after_seconds)
            if item.code and item.last_price > 0:
                out[code] = item.to_dict(include_raw=False) | {
                    "collector_id": raw.get("collector_id", ""),
                    "schema_version": raw.get("schema_version", 1),
                }
        return out

    def read_one(self, code: str) -> Dict[str, Any]:
        code6 = normalize_stock_code(code, add_suffix=False)
        return self.read_batch([code6]).get(code6, {})

    def read_ticks(self, code: str, trade_date: str) -> List[Dict[str, Any]]:
        return self.snapshots.read(str(trade_date)[:8], code)

    def health(self) -> Dict[str, Any]:
        meta = self.backend.get_json(self.META_KEY) or {}
        collector = self.backend.get_json(self.COLLECTOR_HEALTH_KEY) or {}
        return {
            "available": bool(meta),
            "storage": self.storage,
            **meta,
            "collector": collector,
        }


__all__ = ["RealtimeQuoteCache"]
