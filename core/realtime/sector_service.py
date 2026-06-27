"""Realtime sector and market quote service backed by optional adata."""
from __future__ import annotations

import importlib
import csv
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger

from core.realtime.models import QuoteSnapshot, SectorSnapshot


class RealtimeSectorService:
    """Realtime sector quotes through adata when it is installed."""

    INVALID_CODE_TEXT = {"", "-", "--", "nan", "none", "null", "nat", "<na>"}

    DEFAULT_SECTOR_NAMES = {
        "886109": "2026一季报预增",
        "886108": "AI应用",
        "886107": "2025年报预增",
    }

    def __init__(
        self,
        adata_module: Any = None,
        *,
        ttl_seconds: float = 15.0,
        request_timeout_seconds: float = 3.0,
    ):
        self.adata = adata_module
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.request_timeout_seconds = max(float(request_timeout_seconds), 0.5)
        self._sector_cache: Dict[Tuple[str, str], Tuple[float, SectorSnapshot]] = {}
        self._sector_names: Dict[str, str] = dict(self.DEFAULT_SECTOR_NAMES)
        self._sector_types: Dict[str, str] = {}
        self._sector_name_sources = set()
        self._last_error = ""

    @staticmethod
    def available() -> bool:
        try:
            importlib.import_module("adata")
            return True
        except Exception:
            return False

    def health(self, *, probe: bool = False) -> Dict[str, Any]:
        adata_mod = self._ensure_adata()
        data = {
            "available": adata_mod is not None,
            "provider": "adata" if adata_mod is not None else "",
            "cache_size": len(self._sector_cache),
            "ttl_seconds": self.ttl_seconds,
            "last_error": self._last_error,
        }
        if probe and adata_mod is not None:
            result = self.get_sector_quotes(codes=None, source="east", limit=3)
            data["probe_ok"] = bool(result.get("ok"))
            data["probe_count"] = result.get("count", 0)
            data["probe_message"] = result.get("message", "")
        return data

    def get_sector_quotes(
        self,
        codes: Optional[Iterable[str]] = None,
        *,
        source: str = "east",
        limit: int = 20,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        adata_mod = self._ensure_adata()
        if adata_mod is None:
            return self._unavailable()

        code_list = self._normalize_codes(codes)
        auto_list = not code_list
        if auto_list:
            code_list = self._list_sector_codes(source=source, limit=limit)
            if not code_list and (source or "").lower() not in ("ths", "tonghuashun", "同花顺"):
                source = "ths"
                code_list = self._list_sector_codes(source=source, limit=limit)
        if not code_list:
            return {
                "ok": False,
                "available": True,
                "message": "未找到板块代码；请传 codes 参数或检查 adata 版本",
                "sectors": [],
                "count": 0,
            }
        rows: List[SectorSnapshot] = []
        errors: List[str] = []
        for code in code_list[: max(int(limit or 20), 1)]:
            item = self._get_sector_quote(code, source=source)
            if item is None:
                errors.append(code)
                continue
            rows.append(item)

        if auto_list:
            rows.sort(key=lambda x: x.change_pct if x.change_pct is not None else -999.0, reverse=True)
        return {
            "ok": bool(rows),
            "available": True,
            "source": source,
            "message": "" if rows else "未获取到板块实时行情",
            "sectors": [r.to_dict(include_raw=include_raw) for r in rows],
            "count": len(rows),
            "missing": errors,
        }

    def get_market_quotes(
        self,
        codes: Optional[Iterable[str]] = None,
        *,
        limit: int = 100,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        adata_mod = self._ensure_adata()
        if adata_mod is None:
            return self._unavailable(rows_key="quotes")

        market = getattr(getattr(adata_mod, "stock", None), "market", None)
        method = getattr(market, "list_market_current", None) if market is not None else None
        if method is None:
            return {
                "ok": False,
                "available": True,
                "message": "当前 adata 版本未提供 stock.market.list_market_current",
                "quotes": [],
                "count": 0,
            }

        code_list = self._normalize_codes(codes)
        try:
            if code_list:
                raw = method(code_list=code_list)
            else:
                raw = method()
        except TypeError:
            raw = method(code_list) if code_list else method()
        except Exception as e:  # noqa: BLE001
            self._last_error = f"全市场实时行情获取失败: {e}"
            logger.debug(f"[RealtimeSectorService] list_market_current failed: {e}")
            return {"ok": False, "available": True, "message": self._last_error, "quotes": [], "count": 0}

        rows = []
        for raw_row in self._records(raw)[: max(int(limit or 100), 1)]:
            item = QuoteSnapshot.from_raw(dict(raw_row), stale_after_seconds=90.0)
            if item.code:
                rows.append(item.to_dict(include_raw=include_raw))
        return {
            "ok": bool(rows),
            "available": True,
            "message": "" if rows else "未获取到全市场实时行情",
            "quotes": rows,
            "count": len(rows),
        }

    def _get_sector_quote(self, code: str, *, source: str) -> Optional[SectorSnapshot]:
        cache_key = (source, code)
        cached = self._sector_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] <= self.ttl_seconds:
            return cached[1]

        adata_mod = self._ensure_adata()
        market = getattr(getattr(adata_mod, "stock", None), "market", None) if adata_mod is not None else None
        if market is None:
            self._last_error = "adata.stock.market 不可用"
            return None

        self._ensure_sector_names(source)
        method_names = self._sector_method_candidates(source)
        for name in method_names:
            method = getattr(market, name, None)
            if method is None:
                continue
            try:
                raw = self._call_code_method_with_timeout(method, code)
                records = self._records(raw)
                if not records:
                    continue
                row = dict(records[0])
                row.setdefault("code", code)
                row.setdefault("name", self._sector_names.get(code, ""))
                row.setdefault("sector_type", self._sector_types.get(code, ""))
                row.setdefault("source", f"adata_{source}")
                item = SectorSnapshot.from_raw(row, source=f"adata_{source}")
                if item.code:
                    self._sector_cache[cache_key] = (now, item)
                    self._last_error = ""
                    return item
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{name}({code}) 失败: {e}"
                logger.debug(f"[RealtimeSectorService] {name}({code}) failed: {e}")
                continue
        return None

    def _call_code_method_with_timeout(self, method, code: str):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._call_code_method, method, code)
        try:
            return future.result(timeout=self.request_timeout_seconds)
        except FutureTimeoutError:
            raise TimeoutError(f"单板块行情超过 {self.request_timeout_seconds:.1f}s")
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _list_sector_codes(self, *, source: str, limit: int) -> List[str]:
        local_records = self._local_sector_records(source)
        if local_records:
            codes: List[str] = []
            seen = set()
            for row in local_records:
                code = self._row_sector_code(row)
                if not code or code in seen:
                    continue
                seen.add(code)
                codes.append(code)
                self._remember_sector_meta(code, self._row_sector_name(row), row.get("sector_type") or "概念")
                if len(codes) >= max(int(limit or 20), 1):
                    return codes
            if codes:
                return codes

        adata_mod = self._ensure_adata()
        info = getattr(getattr(adata_mod, "stock", None), "info", None) if adata_mod is not None else None
        if info is None:
            return []

        source_key = (source or "east").lower()
        if source_key in ("ths", "tonghuashun", "同花顺"):
            candidates = ("all_concept_code_ths", "all_industry_code_ths")
        else:
            candidates = ("all_concept_code_east", "all_industry_code_east")

        codes: List[str] = []
        seen = set()
        for name in candidates:
            method = getattr(info, name, None)
            if method is None:
                continue
            type_hint = "概念" if "concept" in name else ("行业" if "industry" in name else "")
            try:
                records = self._records(method())
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[RealtimeSectorService] {name} failed: {e}")
                continue
            for row in records:
                code = self._clean_code(
                    row.get("code")
                    or row.get("index_code")
                    or row.get("板块代码")
                    or row.get("概念代码")
                    or row.get("行业代码")
                    or ""
                )
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
                    label = self._row_sector_name(row)
                    typ = self._sector_type_label(
                        row.get("sector_type") or row.get("type") or row.get("板块类型") or type_hint
                    )
                    self._remember_sector_meta(code, label, typ)
                if len(codes) >= max(int(limit or 20), 1):
                    return codes
        return codes

    def _ensure_sector_names(self, source: str) -> None:
        source_key = (source or "east").lower()
        if source_key in self._sector_name_sources:
            return
        local_records = self._local_sector_records(source)
        if local_records:
            for row in local_records:
                self._remember_sector_meta(
                    self._row_sector_code(row),
                    self._row_sector_name(row),
                    row.get("sector_type") or "概念",
                )
            self._sector_name_sources.add(source_key)
            return

        adata_mod = self._ensure_adata()
        info = getattr(getattr(adata_mod, "stock", None), "info", None) if adata_mod is not None else None
        if info is None:
            return

        if source_key in ("ths", "tonghuashun", "同花顺"):
            candidates = ("all_concept_code_ths", "all_industry_code_ths")
        else:
            candidates = ("all_concept_code_east", "all_industry_code_east")
        for name in candidates:
            method = getattr(info, name, None)
            if method is None:
                continue
            type_hint = "概念" if "concept" in name else ("行业" if "industry" in name else "")
            try:
                for row in self._records(method()):
                    code = self._clean_code(
                        row.get("code")
                        or row.get("index_code")
                        or row.get("concept_code")
                        or row.get("industry_code")
                        or ""
                    )
                    label = self._row_sector_name(row)
                    typ = self._sector_type_label(
                        row.get("sector_type") or row.get("type") or row.get("板块类型") or type_hint
                    )
                    self._remember_sector_meta(code, label, typ)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[RealtimeSectorService] sector name map failed via {name}: {e}")
        self._sector_name_sources.add(source_key)

    def _local_sector_records(self, source: str) -> List[Dict[str, Any]]:
        source_key = (source or "east").lower()
        if source_key not in ("ths", "tonghuashun", "同花顺"):
            return []
        try:
            from config.settings import CACHE_DIR

            path = Path(CACHE_DIR) / "sector" / "ths_index" / "adata_concept_ths.csv"
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                return [dict(row) for row in csv.DictReader(f)]
        except Exception:
            return []

    @staticmethod
    def _row_sector_code(row: Dict[str, Any]) -> str:
        return RealtimeSectorService._clean_code(
            row.get("code")
            or row.get("index_code")
            or row.get("sector_code")
            or row.get("concept_code")
            or row.get("板块代码")
            or row.get("概念代码")
            or row.get("行业代码")
            or ""
        )

    @staticmethod
    def _clean_code(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text.lower() in RealtimeSectorService.INVALID_CODE_TEXT else text

    def _remember_sector_meta(self, code: str, label: str, typ: str) -> None:
        code = self._clean_code(code)
        if not code:
            return
        keys = [code]
        if "." not in code and code.isdigit():
            keys.append(f"{code}.TI")
        label = str(label or "").strip()
        typ = self._sector_type_label(typ)
        for key in keys:
            if label:
                self._sector_names[key] = label
            if typ:
                self._sector_types[key] = typ

    @staticmethod
    def _row_sector_name(row: Dict[str, Any]) -> str:
        return str(
            row.get("name")
            or row.get("index_name")
            or row.get("concept_name")
            or row.get("industry_name")
            or row.get("板块名称")
            or row.get("概念名称")
            or row.get("行业名称")
            or ""
        ).strip()

    @staticmethod
    def _sector_type_label(value: Any) -> str:
        text = str(value or "").strip()
        return {
            "N": "概念",
            "I": "行业",
            "R": "地域",
            "S": "特色",
        }.get(text, text)

    def _ensure_adata(self):
        if self.adata is not None:
            return self.adata
        try:
            self.adata = importlib.import_module("adata")
            return self.adata
        except Exception as e:  # noqa: BLE001
            self._last_error = f"adata 未安装或无法导入: {e}"
            logger.debug(f"[RealtimeSectorService] adata unavailable: {e}")
            return None

    @staticmethod
    def _sector_method_candidates(source: str) -> Tuple[str, ...]:
        key = (source or "east").lower()
        if key in ("ths", "tonghuashun", "同花顺"):
            return (
                "get_market_concept_current_ths",
                "get_market_industry_current_ths",
                "get_market_index_current",
            )
        return (
            "get_market_concept_current_east",
            "get_market_industry_current_east",
            "get_market_index_current",
        )

    @staticmethod
    def _call_code_method(method, code: str):
        for kwargs in ({"index_code": code}, {"code": code}):
            try:
                return method(**kwargs)
            except TypeError:
                continue
        return method(code)

    @staticmethod
    def _records(raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        if hasattr(raw, "to_dict"):
            try:
                return list(raw.to_dict(orient="records"))
            except TypeError:
                pass
        if isinstance(raw, list):
            return [dict(x) for x in raw if isinstance(x, dict)]
        if isinstance(raw, tuple):
            return [dict(x) for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            for key in ("data", "rows", "items", "result"):
                value = raw.get(key)
                if isinstance(value, list):
                    return [dict(x) for x in value if isinstance(x, dict)]
            return [raw]
        return []

    @staticmethod
    def _normalize_codes(codes: Optional[Iterable[str]]) -> List[str]:
        if codes is None:
            return []
        if isinstance(codes, str):
            parts = [x.strip() for x in codes.replace("，", ",").split(",")]
        else:
            parts = [RealtimeSectorService._clean_code(x) for x in codes]
        out: List[str] = []
        seen = set()
        for code in parts:
            code = RealtimeSectorService._clean_code(code)
            if code and code not in seen:
                seen.add(code)
                out.append(code)
        return out

    def _unavailable(self, *, rows_key: str = "sectors") -> Dict[str, Any]:
        return {
            "ok": False,
            "available": False,
            "message": self._last_error or "adata 未安装，板块实时行情不可用",
            rows_key: [],
            "count": 0,
        }
