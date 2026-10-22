"""Language-neutral realtime quote contract shared by collector and Web."""
from __future__ import annotations

from datetime import datetime
from core.realtime.trading_session import shanghai_now
from typing import Any, Dict, Iterable, List

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import QuoteSnapshot, normalize_stock_code
from core.realtime.snapshot_repository import RealtimeSnapshotRepository
from core.realtime.session_open_repository import SessionOpenRepository


class RealtimeQuoteCache:
    """Store latest normalized quotes and a short rolling snapshot sequence."""

    META_KEY = "realtime:quotes:meta"
    COLLECTOR_HEALTH_KEY = "realtime:collector:health"

    def __init__(self, backend: Any = None, snapshot_repository: Any = None, opening_repository: Any = None, journal=None) -> None:
        from config.settings import REALTIME_QUOTE_STALE_SECONDS, REALTIME_QUOTE_TTL_SECONDS

        self.backend = backend or get_shared_state_backend()
        self.snapshots = snapshot_repository or RealtimeSnapshotRepository()
        self.openings = opening_repository or SessionOpenRepository(backend=self.backend)
        self.journal = journal
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
        snapshot_codes: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        now = shanghai_now()
        values: Dict[str, Dict[str, Any]] = {}
        snapshot_rows: List[Dict[str, Any]] = []
        recorded_codes = set(snapshot_codes) if snapshot_codes is not None else None
        trade_dates = set()
        rejected_codes: List[str] = []
        prepared: List[tuple[QuoteSnapshot, Dict[str, Any]]] = []
        for raw in rows or []:
            payload = dict(raw or {})
            payload.setdefault("source", source)
            payload.setdefault("received_at", now.isoformat(timespec="milliseconds"))
            item = QuoteSnapshot.from_raw(payload, stale_after_seconds=self.stale_after_seconds)
            if not item.code or item.last_price <= 0:
                if item.code:
                    rejected_codes.append(item.code)
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
            old_date = str(old.get("date") or "").replace("-", "")
            incoming_date = str(item.date or "").replace("-", "")
            receipt_regressed = False
            if same_session and old.get("received_at") and item.received_at:
                try:
                    receipt_regressed = (shanghai_now(datetime.fromisoformat(item.received_at))
                                         < shanghai_now(datetime.fromisoformat(old["received_at"])))
                except ValueError:
                    receipt_regressed = True
            if (not source_time_ok or (old_date and incoming_date and incoming_date < old_date)
                    or receipt_regressed):
                rejected_codes.append(item.code)
                continue
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
            if recorded_codes is None or item.code in recorded_codes:
                snapshot_rows.append(normalized)
        if not values:
            return {"ok": False, "count": 0, "rejected_codes": rejected_codes,
                    "message": "收到的报价早于已有报价，保留较新缓存"}
        trade_date = max(trade_dates) if trade_dates else now.strftime("%Y%m%d")
        if self.journal is not None and snapshot_rows:
            self.journal.append("snapshot", trade_date, snapshot_rows)
        self.backend.set_many_json(values, self.ttl_seconds)
        self.snapshots.append_batch(trade_date, snapshot_rows)
        opening_error = ""
        try:
            self.openings.record_batch(values.values(), now=now)
        except Exception as exc:
            from loguru import logger
            opening_error = str(exc)
            logger.warning("Session opening evidence could not be retained: {}", exc)
        meta = {
            "schema_version": 1,
            "source": source,
            "collector_id": collector_id,
            "trade_date": trade_date,
            "quote_count": len(values),
            "updated_at": now.isoformat(timespec="milliseconds"),
            "storage": self.storage,
            "opening_evidence_error": opening_error,
        }
        self.backend.set_json(self.META_KEY, meta, self.ttl_seconds)
        from core.realtime.market_events import publish_revision
        publish_revision(self.backend, trade_date, kind="quotes")
        return {"ok": True, "count": len(values), "rejected_codes": rejected_codes, **meta}

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
