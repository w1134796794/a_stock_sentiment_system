"""Realtime sector-index direction and constituent breadth confirmation."""
from __future__ import annotations

from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from backtest.trade_calendar import TradeCalendar
from core.realtime.models import normalize_stock_code


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class RealtimeSectorBreadthProvider:
    def __init__(
        self,
        *,
        quote_service: Any = None,
        sector_service: Any = None,
        duckdb_path: Optional[Path] = None,
        ttl_seconds: float = 8.0,
    ) -> None:
        from config.settings import FACTOR_DB_PATH

        self.quote_service = quote_service
        self.sector_service = sector_service
        self.duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
        self.ttl_seconds = max(float(ttl_seconds), 3.0)
        self.calendar = TradeCalendar()
        self._member_cache: Dict[Tuple[str, str], Set[str]] = {}
        self._result_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, Optional[bool], Dict[str, Any]]] = {}
        self._last_sector_quote_detail: Dict[str, Any] = {}
        self._lock = RLock()

    def evaluate(self, sectors: Iterable[str], market_date: str) -> Tuple[Optional[bool], Dict[str, Any]]:
        names = tuple(sorted({str(name or "").strip() for name in sectors if str(name or "").strip()}))
        key = (str(market_date), names)
        now = monotonic()
        with self._lock:
            cached = self._result_cache.get(key)
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1], dict(cached[2])
        if not names:
            return None, {"reason": "候选缺少所属板块", "data_completeness": 0.0}
        members: Set[str] = set()
        for name in names:
            members.update(self._members(name, market_date))
        member_quotes = self._stock_quotes(sorted(members)[:80])
        observed = [row for row in member_quotes.values() if row.get("change_pct") is not None]
        breadth = sum(_float(row.get("change_pct")) > 0 for row in observed) / len(observed) if observed else None
        average_change = sum(_float(row.get("change_pct")) for row in observed) / len(observed) if observed else None

        index_changes = self._sector_changes(names)
        index_positive = (sum(index_changes) / len(index_changes) >= 0) if index_changes else None
        if breadth is None or index_positive is None:
            state: Optional[bool] = None
        else:
            state = bool(breadth >= 0.55 and (average_change or 0.0) >= 0.0 and index_positive)
        completeness = (0.5 if breadth is not None else 0.0) + (0.5 if index_positive is not None else 0.0)
        detail = {
            "sector_names": list(names),
            "member_count": len(members),
            "observed_members": len(observed),
            "breadth": round(breadth, 4) if breadth is not None else None,
            "average_change_pct": round(average_change, 4) if average_change is not None else None,
            "index_change_pct": round(sum(index_changes) / len(index_changes), 4) if index_changes else None,
            "data_completeness": completeness,
            "reason": self._reason(state, breadth, index_positive),
            "sector_quote_detail": dict(self._last_sector_quote_detail),
        }
        with self._lock:
            self._result_cache[key] = (now, state, detail)
        return state, detail

    def _members(self, sector_name: str, market_date: str) -> Set[str]:
        try:
            previous = self.calendar.prev(str(market_date))
        except Exception:
            previous = str(market_date)
        key = (previous, sector_name)
        if key in self._member_cache:
            return self._member_cache[key]
        if not self.duckdb_path.exists():
            return set()
        try:
            import duckdb  # type: ignore

            con = duckdb.connect(str(self.duckdb_path), read_only=True)
            try:
                rows = con.execute(
                    "SELECT code FROM factor_stock_wide "
                    "WHERE CAST(trade_date AS VARCHAR)=? AND resonance_sectors LIKE ?",
                    [previous, f"%{sector_name}%"],
                ).fetchall()
            finally:
                con.close()
            members = {normalize_stock_code(row[0], add_suffix=False) for row in rows if row and row[0]}
        except Exception:
            members = set()
        self._member_cache[key] = members
        return members

    def _stock_quotes(self, codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        service = self._ensure_quote_service()
        if service is None:
            return {}
        try:
            payload = service.get_quotes(list(codes))
        except Exception:
            return {}
        return {
            normalize_stock_code(row.get("code"), add_suffix=False): row
            for row in payload.get("quotes") or [] if row.get("code")
        }

    def _sector_changes(self, names: Tuple[str, ...]) -> list[float]:
        service = self._ensure_sector_service()
        if service is None:
            self._last_sector_quote_detail = {"reason": "板块行情服务不可用"}
            return []
        changes: list[float] = []
        unresolved = set(names)
        attempts = []
        try:
            mapping = service.resolve_codes_by_names(unresolved, source="ths")
            if not mapping:
                attempts.append({"source": "ths", "resolved": 0, "usable": 0})
            else:
                payload = service.get_sector_quotes(
                    mapping.values(), source="ths", limit=len(mapping),
                )
                rows = payload.get("sectors") or []
                usable_codes = {
                    str(row.get("code") or "").split(".")[0]
                    for row in rows if row.get("change_pct") is not None
                }
                usable_names = {
                    name for name, code in mapping.items()
                    if str(code or "").split(".")[0] in usable_codes
                }
                changes.extend(
                    _float(row.get("change_pct"))
                    for row in rows if row.get("change_pct") is not None
                )
                unresolved.difference_update(usable_names)
                attempts.append({
                    "source": "ths",
                    "resolved": len(mapping),
                    "quotes": len(rows),
                    "usable": len(usable_names),
                    "message": str(payload.get("message") or ""),
                    "missing_codes": list(payload.get("missing") or []),
                })
        except Exception as exc:  # noqa: BLE001
            attempts.append({"error": str(exc)})
        self._last_sector_quote_detail = {
            "attempts": attempts,
            "usable_count": len(changes),
            "unresolved_names": sorted(unresolved),
        }
        return changes

    @staticmethod
    def _reason(
        state: Optional[bool], breadth: Optional[float], index_positive: Optional[bool],
    ) -> str:
        if state is True:
            return "板块指数与成分股同步"
        if state is False:
            return "板块同步不足"
        if breadth is None and index_positive is None:
            return "板块成分股与指数实时数据均不足"
        if breadth is None:
            return "板块成分股实时数据不足"
        return "板块指数涨幅不足"

    def _ensure_quote_service(self):
        if self.quote_service is not None:
            return self.quote_service
        try:
            from core.realtime.quote_service import RealtimeQuoteService

            self.quote_service = RealtimeQuoteService()
        except Exception:
            self.quote_service = None
        return self.quote_service

    def _ensure_sector_service(self):
        if self.sector_service is not None:
            return self.sector_service
        try:
            from core.realtime.sector_service import get_realtime_sector_service

            self.sector_service = get_realtime_sector_service()
        except Exception:
            self.sector_service = None
        return self.sector_service


__all__ = ["RealtimeSectorBreadthProvider"]
