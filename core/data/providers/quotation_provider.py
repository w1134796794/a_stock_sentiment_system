"""Optional realtime quote adapter backed by pqquotation/easyquotation.

The strategy layer already consumes project-shaped quote snapshots through
``DataManager.get_quote_snapshot(s)``. This adapter keeps the public quotation
libraries behind that stable interface and normalizes their Sina/Tencent field
names to the fields used by the intraday monitor:

    open_price, pre_close, last_price, high_price, low_price,
    vol_hand, amount_yuan, change_pct, source

Both dependencies are optional; import/network failures return empty results so
the existing eltdx fallback can continue to serve realtime features.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

from core.utils.stock_code_utils import StockCodeUtils


class QuotationProvider:
    """Realtime stock quote provider using pqquotation/easyquotation."""

    def __init__(
        self,
        *,
        source: str = "sina",
        backends: Optional[Iterable[str]] = None,
        timeout_seconds: float = 5.0,
        backend_cooldown_seconds: float = 120.0,
    ):
        self.source = source or "sina"
        self.backends = list(backends or ("easyquotation", "pqquotation"))
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self.backend_cooldown_seconds = max(float(backend_cooldown_seconds), 5.0)
        self._clients: Dict[str, Any] = {}
        self._disabled_until: Dict[str, float] = {}
        self._last_error = ""

    @staticmethod
    def available() -> bool:
        for mod_name in ("pqquotation", "easyquotation"):
            try:
                __import__(mod_name)
                return True
            except Exception:
                continue
        return False

    def get_quote_snapshot(self, ts_code: str) -> dict:
        code = self._code6(ts_code)
        if not code:
            return {}
        return self.get_quote_snapshots([code]).get(code, {})

    def get_quote_snapshots(self, ts_codes: Iterable[str]) -> Dict[str, Dict]:
        codes = [self._code6(c) for c in ts_codes or []]
        codes = [c for c in codes if c]
        if not codes:
            return {}

        for backend in self.backends:
            if self._backend_disabled(backend):
                continue
            client = self._client(backend)
            if client is None:
                continue
            try:
                raw = self._call_with_timeout(client, codes)
                normalized = self._normalize_batch(raw, backend)
                if normalized:
                    self._last_error = ""
                    return normalized
            except FutureTimeoutError:
                self._cooldown_backend(backend, f"获取实时行情超过 {self.timeout_seconds:.0f}s")
            except Exception as e:  # noqa: BLE001
                self._cooldown_backend(backend, str(e))
                continue
        return {}

    def _client(self, backend: str):
        if backend in self._clients:
            return self._clients[backend]
        try:
            mod = __import__(backend)
            client = mod.use(self.source)
            self._clients[backend] = client
            return client
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[QuotationProvider] {backend}/{self.source} unavailable: {e}")
            return None

    def _call_with_timeout(self, client: Any, codes: List[str]) -> Any:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(client.stocks, codes, prefix=False)
        try:
            return future.result(timeout=self.timeout_seconds)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _backend_disabled(self, backend: str) -> bool:
        return time.monotonic() < self._disabled_until.get(backend, 0.0)

    def _cooldown_backend(self, backend: str, reason: str) -> None:
        self._disabled_until[backend] = time.monotonic() + self.backend_cooldown_seconds
        self._last_error = f"{backend}/{self.source} 暂停 {self.backend_cooldown_seconds:.0f}s: {reason}"
        logger.warning(f"[QuotationProvider] {self._last_error}")

    @staticmethod
    def _code6(ts_code: Any) -> str:
        try:
            return StockCodeUtils.standardize_code(str(ts_code), add_suffix=False)
        except Exception:
            s = str(ts_code or "").strip()
            if "." in s:
                s = s.split(".")[0]
            return s.zfill(6) if s.isdigit() else ""

    def _normalize_batch(self, raw: Any, backend: str) -> Dict[str, Dict]:
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, Dict] = {}
        for key, quote in raw.items():
            if not isinstance(quote, dict):
                continue
            code = self._code6(quote.get("code") or key)
            if not code:
                continue
            item = self._normalize_quote(code, quote, backend)
            if item.get("last_price", 0) > 0 or item.get("pre_close", 0) > 0 or item.get("name"):
                out[code] = item
        return out

    def _normalize_quote(self, code: str, quote: Dict[str, Any], backend: str) -> Dict:
        last = self._float(quote.get("now"))
        pre_close = self._float(quote.get("close"))
        open_price = self._float(quote.get("open"))
        amount_yuan = self._amount_yuan(quote)
        vol_hand = self._volume_hand(quote)
        change_pct = quote.get("涨跌(%)")
        if change_pct is None and last > 0 and pre_close > 0:
            change_pct = (last - pre_close) / pre_close * 100.0
        return {
            "code": code,
            "ts_code": StockCodeUtils.standardize_code(code, add_suffix=True),
            "name": quote.get("name", ""),
            "open_price": open_price,
            "pre_close": pre_close,
            "last_price": last,
            "high_price": self._float(quote.get("high") or quote.get("high_2")),
            "low_price": self._float(quote.get("low") or quote.get("low_2")),
            "bid1": self._float(quote.get("bid1")),
            "ask1": self._float(quote.get("ask1")),
            "vol_hand": vol_hand,
            "amount_yuan": amount_yuan,
            "change_pct": self._float(change_pct) if change_pct is not None else None,
            "date": str(quote.get("date") or ""),
            "time": str(quote.get("time") or ""),
            "source": f"{backend}_{self.source}",
        }

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0)
        except Exception:
            return 0.0

    @classmethod
    def _amount_yuan(cls, quote: Dict[str, Any]) -> float:
        # Sina field ``volume`` and Tencent's parsed amount are both yuan in the
        # tested libraries despite some labels saying "万".
        for key in ("volume", "成交额(万)"):
            value = cls._float(quote.get(key))
            if value > 0:
                return value
        packed = str(quote.get("价格/成交量(手)/成交额") or "")
        parts = packed.split("/")
        if len(parts) >= 3:
            return cls._float(parts[2])
        return 0.0

    @classmethod
    def _volume_hand(cls, quote: Dict[str, Any]) -> float:
        # easyquotation/pqquotation expose turnover as shares for Sina. Tencent
        # also returns share-level volume in common parsed fields, while the
        # packed string contains real hands.
        packed = str(quote.get("价格/成交量(手)/成交额") or "")
        parts = packed.split("/")
        if len(parts) >= 2:
            hands = cls._float(parts[1])
            if hands > 0:
                return hands

        for key in ("turnover", "成交量(手)", "volume"):
            raw = cls._float(quote.get(key))
            if raw > 0:
                return raw / 100.0
        return 0.0
