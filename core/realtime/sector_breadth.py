"""Realtime sector-index direction and constituent breadth confirmation."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
from threading import RLock
from time import monotonic
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from backtest.trade_calendar import TradeCalendar
from core.realtime.models import normalize_stock_code, parse_quote_datetime
from core.realtime.sector_service import RealtimeSectorService
from core.realtime.trading_session import shanghai_now
from core.signals.sector_sync import assess_sector_sync, finite_number, TREND_WINDOW_MINUTES


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def closed_quote_checker(*, names, market_date, code, members, previous, points, clock):
    """Shared live/replay interpretation of already-visible closed quote minutes."""
    from core.signals.sector_sync import make_constituent_sync_checker

    boundary = clock.replace(second=0, microsecond=0)
    previous = dict(previous)
    opening = {}
    for member, values in points.items():
        if not values:
            continue
        latest = values[max(values)]
        if latest.get("open_price"):
            opening[member] = latest["open_price"]
        if latest.get("pre_close"):
            previous[member] = {"close": latest["pre_close"]}
    prices = {member: {minute: row["price"] for minute, row in values.items()
                       if (not row.get("open_price") or row["open_price"] == opening.get(member))
                       and (not row.get("pre_close") or row["pre_close"] == (previous.get(member) or {}).get("close"))}
              for member, values in points.items()}
    base, detail = make_constituent_sync_checker(code=code, names=names, members=members,
        frames={}, previous=previous, opening_prices=opening, minute_prices=prices)

    def checker(at_time):
        minute = str(at_time)[:5] + ":00"
        end = shanghai_now(datetime.strptime(market_date + minute, "%Y%m%d%H:%M:%S"))
        if market_date != clock.strftime("%Y%m%d") or end > boundary:
            detail.update({"assessed": True, "data_completeness": 0.0,
                           "reason": "板块分钟尚未闭合或不是当前交易日"})
            return None
        state = base(minute)
        detail.update({"observed_at": end.isoformat(), "sector_confirmation_source": "collector_closed_quote_minutes",
                       "timestamp_semantics": "minute_end", "sampled_at": clock.isoformat()})
        return state
    return checker, detail


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
        self._opening_history = {}
        self._minute_context_cache = {}
        self._batch_frozen_context = None
        self._lock = RLock()

    def minute_checker(self, sectors, market_date, *, exclude_code="", history_minutes=25):
        """Read collector evidence; never reconstruct history from Web polling."""
        from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
        from core.operations.preopen_evidence import PreopenEvidenceStore
        clock = shanghai_now()
        boundary = clock.replace(second=0, microsecond=0)
        names = tuple(sorted(set(sectors)))
        context = self._batch_frozen_context
        frozen = context[1] if context and context[0] == market_date else (
            PreopenEvidenceStore().load(self.calendar.prev(market_date), market_date))
        members = (RealtimeSectorService.confirmation_members(frozen["inputs"]) if frozen else
                   {name: sorted(self._members(name, market_date)) for name in names})
        codes = sorted({c for name in names for c in members.get(name, [])})
        key = (market_date, tuple(codes), boundary.isoformat(), history_minutes)
        with self._lock:
            cached = self._minute_context_cache.get(key)
        if cached is None or monotonic() - cached[0] > self.ttl_seconds:
            minutes = [(boundary - timedelta(minutes=i)).strftime("%H:%M:%S") for i in range(history_minutes)]
            points = SectorQuoteMinuteRepository().read_points(
                market_date, codes, until=boundary.strftime("%H:%M:%S"), minutes=minutes)
            with self._lock:
                self._minute_context_cache = {k: v for k, v in self._minute_context_cache.items()
                                              if k[0] == market_date and k[2] == key[2]}
                self._minute_context_cache[key] = (monotonic(), points)
        else:
            points = cached[1]
        previous = {}
        if frozen:
            previous = {str(r.get("code") or r.get("ts_code") or "").split(".")[0]: dict(r)
                        for r in frozen["inputs"].get("daily_rows") or []}
        return closed_quote_checker(names=names, market_date=market_date, code=exclude_code,
                                    members=members, previous=previous, points=points, clock=clock)

    def evaluate(self, sectors: Iterable[str], market_date: str, *, exclude_code: str = '') -> Tuple[Optional[bool], Dict[str, Any]]:
        names = tuple(sorted({str(name or "").strip() for name in sectors if str(name or "").strip()}))
        key = (str(market_date), names, exclude_code)
        now = monotonic()
        with self._lock:
            cached = self._result_cache.get(key)
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1], dict(cached[2])
        if not names:
            return None, {"reason": "候选缺少所属板块", "data_completeness": 0.0}
        from core.operations.preopen_evidence import PreopenEvidenceStore
        frozen = PreopenEvidenceStore().load(self.calendar.prev(str(market_date)), str(market_date))
        constituent_map = RealtimeSectorService.confirmation_members(frozen["inputs"]) if frozen else {}
        if not frozen:
            constituent_map = {name: sorted(self._members(name, market_date)) for name in names}
        constituent_map = {name: [c for c in codes if c != exclude_code] for name, codes in constituent_map.items()}
        members: Set[str] = set()
        for name in names:
            members.update(constituent_map.get(name, []))
        member_quotes = self._stock_quotes(sorted(members)[:80])
        observed = [
            row for row in member_quotes.values()
            if row.get("change_pct") is not None and not row.get("is_stale")
        ]
        breadth = sum(_float(row.get("change_pct")) > 0 for row in observed) / len(observed) if observed else None
        average_change = sum(_float(row.get("change_pct")) for row in observed) / len(observed) if observed else None

        if constituent_map:
            index_quotes = self._stock_quotes(sorted(members))
            index_changes = []
            for name in names:
                codes = constituent_map.get(name, [])
                usable = [index_quotes[code] for code in codes if code in index_quotes
                          and not index_quotes[code].get("is_stale")
                          and index_quotes[code].get("change_pct") is not None]
                if codes and len(usable) / len(codes) >= 0.6:
                    index_changes.append(sum(_float(row["change_pct"]) for row in usable) / len(usable))
            self._last_sector_quote_detail = {"source": "ths_frozen_constituents", "usable_count": len(index_changes)}
        else:
            index_changes = self._sector_changes(names)
            index_quotes = self._stock_quotes(sorted(members))
        index_positive = (sum(index_changes) / len(index_changes) >= 0) if index_changes else None
        coverage = len(observed) / min(len(members), 80) if members else 0.0
        opening, earlier = self._opening_curve(key, index_quotes)
        state, sync_detail = assess_sector_sync(
            complete=coverage >= 0.6 and len(observed) >= 2 and index_positive is not None,
            breadth=breadth, average_change=average_change, index_positive=index_positive,
            opening_changes=opening, earlier_changes=earlier, expected_members=len(members))
        completeness = 1.0 if state is not None else (0.5 if breadth is not None and coverage >= 0.6 else 0.0)
        detail = {
            "observed_at": shanghai_now().isoformat(timespec="seconds"),
            "sector_names": list(names),
            "unresolved_sector_names": [name for name in names if not constituent_map.get(name)] if frozen else [],
            "member_count": len(members),
            "observed_members": len(observed),
            "coverage": round(coverage, 4),
            "breadth": round(breadth, 4) if breadth is not None else None,
            "average_change_pct": round(average_change, 4) if average_change is not None else None,
            "index_change_pct": round(sum(index_changes) / len(index_changes), 4) if index_changes else None,
            "data_completeness": completeness,
            **sync_detail,
            "sector_quote_detail": dict(self._last_sector_quote_detail),
        }
        with self._lock:
            self._result_cache[key] = (now, state, detail)
        return state, detail

    def _opening_curve(self, key, quotes):
        current = shanghai_now()
        values = {}
        stamps = []
        for code, row in quotes.items():
            opening, price = finite_number(row.get("open_price")), finite_number(row.get("last_price"))
            stamp = parse_quote_datetime(row.get("date"), row.get("time")) if row.get("time") else None
            if (row.get("is_stale") or opening is None or price is None or opening <= 0 or price <= 0
                    or stamp is None or stamp.strftime("%Y%m%d") != key[0]):
                continue
            stamp = stamp.replace(tzinfo=current.tzinfo)
            if not 0 <= (current - stamp).total_seconds() <= 90 or not "09:30" <= stamp.strftime("%H:%M") <= "15:00":
                continue
            values[code] = (opening, (price / opening - 1) * 100)
            stamps.append(stamp)
        if not stamps:
            return {}, {}
        observed = min(stamps)
        target = observed - timedelta(minutes=TREND_WINDOW_MINUTES)
        with self._lock:
            self._opening_history = {k: v for k, v in self._opening_history.items() if k[0] == key[0]}
            history = self._opening_history.setdefault(key, [])
            if not history or observed > history[-1][0]:
                history.append((observed, values))
            history[:] = [(stamp, rows) for stamp, rows in history if (observed - stamp).total_seconds() <= 300]
            known = [(stamp, rows) for stamp, rows in history if 0 <= (target - stamp).total_seconds() <= 30]
            before = known[-1][1] if known else {}
        earlier = {code: value for code, (opening, value) in before.items()
                   if code in values and values[code][0] == opening}
        return {code: value for code, (_, value) in values.items()}, earlier

    def _members(self, sector_name: str, market_date: str) -> Set[str]:
        try:
            previous = self.calendar.prev(str(market_date))
        except Exception:
            previous = str(market_date)
        key = (previous, sector_name)
        from core.operations.preopen_evidence import PreopenEvidenceStore
        frozen = PreopenEvidenceStore().load(previous, market_date)
        if frozen:
            members = set(RealtimeSectorService.confirmation_members(frozen["inputs"]).get(sector_name, []))
        else:
            if key in self._member_cache:
                return self._member_cache[key]
            service = self._ensure_sector_service()
            mapping = service.resolve_codes_by_names([sector_name], source="ths") if service else {}
            members = set(service.member_codes(mapping[sector_name])) if mapping.get(sector_name) else set()
        if members:
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
                    mapping.values(), source="ths", limit=len(mapping), include_raw=True,
                )
                rows = payload.get("sectors") or []
                rows = [row for row in rows if row.get("source") != "ths_constituent_aggregation"
                        or _float((row.get("raw") or {}).get("coverage")) >= 0.6]
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
