"""Realtime stock quote service."""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from core.realtime.models import QuoteSnapshot, normalize_stock_code


class RealtimeQuoteService:
    """Batch realtime stock quotes through DataManager's provider chain."""

    def __init__(
        self,
        data_manager: Any = None,
        *,
        ttl_seconds: float = 2.0,
        stale_after_seconds: float = 90.0,
    ):
        self.dm = data_manager
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.stale_after_seconds = max(float(stale_after_seconds), 1.0)
        self._cache: Dict[str, Tuple[float, QuoteSnapshot]] = {}
        self._last_error = ""

    def get_quote(self, code: str, *, include_raw: bool = False) -> Dict[str, Any]:
        result = self.get_quotes([code], include_raw=include_raw)
        return (result.get("quotes") or [{}])[0] if result.get("quotes") else {}

    def get_quotes(self, codes: Iterable[str], *, include_raw: bool = False) -> Dict[str, Any]:
        normalized_codes = self._normalize_codes(codes)
        if not normalized_codes:
            return {"ok": False, "message": "codes 不能为空", "quotes": [], "count": 0}

        quotes: Dict[str, QuoteSnapshot] = {}
        missing: List[str] = []
        now = time.monotonic()
        for code in normalized_codes:
            cached = self._cache.get(code)
            if cached and now - cached[0] <= self.ttl_seconds:
                quotes[code] = cached[1]
            else:
                missing.append(code)

        if missing:
            fetched = self._fetch(missing)
            quotes.update(fetched)

        rows = [quotes[c].to_dict(include_raw=include_raw) for c in normalized_codes if c in quotes]
        return {
            "ok": bool(rows),
            "message": "" if rows else (self._last_error or "未获取到实时行情"),
            "quotes": rows,
            "count": len(rows),
            "requested": normalized_codes,
            "missing": [c for c in normalized_codes if c not in quotes],
        }

    def health(self, *, probe: bool = False) -> Dict[str, Any]:
        dm = self._ensure_data_manager()
        data = {
            "available": dm is not None,
            "provider": type(dm).__name__ if dm is not None else "",
            "cache_size": len(self._cache),
            "ttl_seconds": self.ttl_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "last_error": self._last_error,
        }
        if probe and dm is not None:
            sample = self.get_quotes(["000001"])
            data["probe_ok"] = bool(sample.get("ok"))
            data["probe_count"] = sample.get("count", 0)
            data["probe_message"] = sample.get("message", "")
        return data

    def _fetch(self, codes: List[str]) -> Dict[str, QuoteSnapshot]:
        dm = self._ensure_data_manager()
        if dm is None:
            self._last_error = "DataManager 初始化失败，实时行情不可用"
            return {}

        raw_map: Dict[str, Dict] = {}
        try:
            if hasattr(dm, "get_quote_snapshots"):
                raw_map = dm.get_quote_snapshots(codes) or {}
            if not raw_map and hasattr(dm, "get_quote_snapshot"):
                raw_map = {code: dm.get_quote_snapshot(code) or {} for code in codes}
        except Exception as e:  # noqa: BLE001
            self._last_error = f"实时行情获取失败: {e}"
            logger.debug(f"[RealtimeQuoteService] quote fetch failed: {e}")
            return {}

        fetched: Dict[str, QuoteSnapshot] = {}
        now = time.monotonic()
        for raw_key, raw in (raw_map or {}).items():
            if not raw:
                continue
            raw = dict(raw)
            raw.setdefault("code", raw_key)
            item = QuoteSnapshot.from_raw(raw, stale_after_seconds=self.stale_after_seconds)
            if item.code and item.last_price > 0:
                fetched[item.code] = item
                self._cache[item.code] = (now, item)
        self._last_error = "" if fetched else "实时行情源返回空结果"
        return fetched

    def _ensure_data_manager(self):
        if self.dm is not None:
            return self.dm
        try:
            from config.settings import CACHE_DIR, TUSHARE_TOKEN
            from core.data.data_manager_main import DataManager

            self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
            return self.dm
        except Exception as e:  # noqa: BLE001
            self._last_error = f"DataManager 初始化失败: {e}"
            logger.debug(f"[RealtimeQuoteService] DataManager unavailable: {e}")
            return None

    @staticmethod
    def _normalize_codes(codes: Iterable[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for code in codes or []:
            code6 = normalize_stock_code(code, add_suffix=False)
            if code6 and code6 not in seen:
                seen.add(code6)
                out.append(code6)
        return out
