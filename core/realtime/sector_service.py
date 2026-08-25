"""THS sector semantics with realtime strength aggregated from stock snapshots."""
from __future__ import annotations

import csv
import statistics
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.realtime.models import SectorSnapshot, normalize_stock_code


class RealtimeSectorService:
    """Use local THS identifiers/members and one Redis stock-quote batch read."""

    INVALID_CODE_TEXT = {"", "-", "--", "nan", "none", "null", "nat", "<na>"}

    def __init__(
        self,
        adata_module: Any = None,
        *,
        quote_service: Any = None,
        cache_dir: Path | str | None = None,
        ttl_seconds: float = 8.0,
        request_timeout_seconds: float = 3.0,
        failure_ttl_seconds: float = 120.0,
    ) -> None:
        del adata_module, request_timeout_seconds, failure_ttl_seconds
        if cache_dir is None:
            from config.settings import CACHE_DIR

            cache_dir = CACHE_DIR
        self.cache_dir = Path(cache_dir)
        self.quote_service = quote_service
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self._sector_cache: Dict[str, Tuple[float, SectorSnapshot]] = {}
        self._member_cache: Dict[str, List[str]] = {}
        self._sector_names: Dict[str, str] = {}
        self._sector_types: Dict[str, str] = {}
        self._metadata_loaded = False
        self._last_error = ""

    @staticmethod
    def available() -> bool:
        from config.settings import REDIS_URL

        return bool(REDIS_URL)

    def health(self, *, probe: bool = False) -> Dict[str, Any]:
        service = self._ensure_quote_service()
        quote_health = service.health() if service is not None else {}
        data = {
            "available": bool(service is not None and quote_health.get("available", True)),
            "provider": "ths_constituents_from_redis",
            "cache_size": len(self._sector_cache),
            "member_cache_size": len(self._member_cache),
            "ttl_seconds": self.ttl_seconds,
            "last_error": self._last_error,
            "quote_health": quote_health,
        }
        if probe:
            result = self.get_sector_quotes(limit=3)
            data.update(probe_ok=bool(result.get("ok")), probe_count=result.get("count", 0))
        return data

    def get_sector_quotes(
        self,
        codes: Optional[Iterable[str]] = None,
        *,
        source: str = "ths",
        limit: int = 20,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        del source
        self._ensure_metadata()
        requested = self._normalize_codes(codes)
        invalid = [code for code in requested if not self._valid_code_for_source(code, "ths")]
        code_list = [code for code in requested if self._valid_code_for_source(code, "ths")]
        auto_list = not requested
        if auto_list:
            code_list = self._list_sector_codes(limit=limit)
        code_list = code_list[: max(int(limit or 20), 1)]
        if not code_list:
            return self._empty("未找到本地同花顺板块元数据或成分股", invalid)

        now = time.monotonic()
        result: Dict[str, SectorSnapshot] = {}
        pending: List[str] = []
        for code in code_list:
            cached = self._sector_cache.get(code)
            if cached and now - cached[0] <= self.ttl_seconds:
                result[code] = cached[1]
            else:
                pending.append(code)

        members_by_sector = {code: self._members(code) for code in pending}
        all_members = list(dict.fromkeys(
            member for members in members_by_sector.values() for member in members
        ))
        quote_map = self._stock_quotes(all_members)
        for code, members in members_by_sector.items():
            item = self._aggregate(code, members, quote_map)
            if item is not None:
                result[code] = item
                self._sector_cache[code] = (now, item)

        rows = [result[code] for code in code_list if code in result]
        if auto_list:
            rows.sort(key=lambda row: row.change_pct if row.change_pct is not None else -999.0, reverse=True)
        missing = [*invalid, *[code for code in code_list if code not in result]]
        self._last_error = "" if rows else "板块成分股实时快照不足"
        return {
            "ok": bool(rows),
            "available": True,
            "source": "ths_constituent_aggregation",
            "message": self._last_error,
            "sectors": [row.to_dict(include_raw=include_raw) for row in rows],
            "count": len(rows),
            "missing": missing,
        }

    def resolve_codes_by_names(
        self, names: Iterable[str], *, source: str = "ths",
    ) -> Dict[str, str]:
        del source
        self._ensure_metadata()
        requested = {str(name or "").strip() for name in names if str(name or "").strip()}
        result: Dict[str, str] = {}
        for code, label in self._sector_names.items():
            for name in requested:
                if name not in result and (
                    label == name or (len(name) >= 3 and (name in label or label in name))
                ):
                    result[name] = code
        return result

    def get_market_quotes(
        self,
        codes: Optional[Iterable[str]] = None,
        *,
        limit: int = 100,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        service = self._ensure_quote_service()
        code_list = self._normalize_codes(codes)[: max(int(limit or 100), 1)]
        if service is None or not code_list:
            return {"ok": False, "available": service is not None, "quotes": [], "count": 0}
        return service.get_quotes(code_list, include_raw=include_raw)

    def _aggregate(
        self,
        code: str,
        members: List[str],
        quote_map: Dict[str, Dict[str, Any]],
    ) -> Optional[SectorSnapshot]:
        observed = [quote_map[member] for member in members if member in quote_map]
        usable = [
            row for row in observed
            if not row.get("is_stale") and row.get("change_pct") is not None
        ]
        if not usable:
            return None
        changes = [float(row["change_pct"]) for row in usable]
        mean_change = sum(changes) / len(changes)
        coverage = len(usable) / len(members) if members else 0.0
        latest = max(usable, key=lambda row: (str(row.get("date") or ""), str(row.get("time") or "")))
        raw = {
            "aggregation": "equal_weight_constituent_snapshot",
            "member_count": len(members),
            "quote_count": len(usable),
            "coverage": round(coverage, 4),
            "up_ratio": round(sum(value > 0 for value in changes) / len(changes), 4),
            "strong_ratio": round(sum(value >= 3 for value in changes) / len(changes), 4),
            "median_change_pct": round(statistics.median(changes), 4),
            "stale_excluded": len(observed) - len(usable),
        }
        return SectorSnapshot.from_raw({
            "code": code,
            "name": self._sector_names.get(code, ""),
            "sector_type": self._sector_types.get(code, ""),
            "last_price": 100.0 + mean_change,
            "pre_close": 100.0,
            "change_pct": mean_change,
            "amount_yuan": sum(float(row.get("amount_yuan") or 0) for row in usable),
            "volume": sum(float(row.get("vol_hand") or 0) for row in usable),
            "date": latest.get("date", ""),
            "time": latest.get("time", ""),
            "source": "ths_constituent_aggregation",
            **raw,
        }, source="ths_constituent_aggregation")

    def _stock_quotes(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        service = self._ensure_quote_service()
        if service is None or not codes:
            return {}
        payload = service.get_quotes(codes)
        return {
            normalize_stock_code(row.get("code"), add_suffix=False): row
            for row in payload.get("quotes") or [] if row.get("code")
        }

    def _members(self, code: str) -> List[str]:
        code6 = self._clean_code(code).split(".")[0]
        if code6 in self._member_cache:
            return self._member_cache[code6]
        root = self.cache_dir / "sector" / "ths_member"
        members: List[str] = []
        for path in (root / f"{code6}.TI.csv", root / f"{code6}.csv"):
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                members = [
                    normalize_stock_code(row.get("con_code") or row.get("code"), add_suffix=False)
                    for row in csv.DictReader(handle)
                ]
            break
        members = [member for member in dict.fromkeys(members) if member]
        self._member_cache[code6] = members
        return members

    def _ensure_quote_service(self):
        if self.quote_service is not None:
            return self.quote_service
        try:
            from core.realtime.quote_service import RealtimeQuoteService

            self.quote_service = RealtimeQuoteService(stale_after_seconds=12.0)
        except Exception:
            self.quote_service = None
        return self.quote_service

    def _ensure_metadata(self) -> None:
        if self._metadata_loaded:
            return
        root = self.cache_dir / "sector" / "ths_index"
        for filename, type_hint in (
            ("adata_concept_ths.csv", "概念"),
            ("index_I.csv", "行业"),
            ("index_N.csv", "概念"),
        ):
            path = root / filename
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    code = self._row_sector_code(row).split(".")[0]
                    if not self._valid_code_for_source(code, "ths"):
                        continue
                    name = self._row_sector_name(row)
                    if name:
                        self._sector_names[code] = name
                    self._sector_types[code] = self._sector_type_label(
                        row.get("sector_type") or row.get("type") or type_hint,
                    )
        self._metadata_loaded = True

    def _list_sector_codes(self, *, limit: int) -> List[str]:
        self._ensure_metadata()
        return list(self._sector_names)[: max(int(limit or 20), 1)]

    @classmethod
    def _valid_code_for_source(cls, code: Any, source: str) -> bool:
        del source
        clean = cls._clean_code(code).split(".")[0]
        return len(clean) == 6 and clean.isdigit() and clean.startswith("8")

    @staticmethod
    def _row_sector_code(row: Dict[str, Any]) -> str:
        return RealtimeSectorService._clean_code(
            row.get("code") or row.get("index_code") or row.get("ts_code")
            or row.get("sector_code") or row.get("concept_code") or ""
        )

    @staticmethod
    def _row_sector_name(row: Dict[str, Any]) -> str:
        return str(
            row.get("name") or row.get("index_name") or row.get("concept_name")
            or row.get("industry_name") or row.get("板块名称") or ""
        ).strip()

    @staticmethod
    def _sector_type_label(value: Any) -> str:
        text = str(value or "").strip()
        return {"N": "概念", "I": "行业", "R": "地域", "S": "特色"}.get(text, text)

    @staticmethod
    def _clean_code(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text.lower() in RealtimeSectorService.INVALID_CODE_TEXT else text

    @staticmethod
    def _normalize_codes(codes: Optional[Iterable[str]]) -> List[str]:
        if codes is None:
            return []
        values = codes.replace("，", ",").split(",") if isinstance(codes, str) else codes
        result: List[str] = []
        for value in values:
            code = RealtimeSectorService._clean_code(value).split(".")[0]
            if code and code not in result:
                result.append(code)
        return result

    @staticmethod
    def _empty(message: str, missing: List[str]) -> Dict[str, Any]:
        return {
            "ok": False,
            "available": True,
            "source": "ths_constituent_aggregation",
            "message": message,
            "sectors": [],
            "count": 0,
            "missing": missing,
        }


_SERVICE: Optional[RealtimeSectorService] = None
_SERVICE_LOCK = RLock()


def get_realtime_sector_service() -> RealtimeSectorService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = RealtimeSectorService()
    return _SERVICE


__all__ = ["RealtimeSectorService", "get_realtime_sector_service"]
