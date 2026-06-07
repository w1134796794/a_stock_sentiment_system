"""eltdx adapter for intraday, K-line and call-auction data.

This module keeps eltdx optional. Import errors or socket failures are surfaced
as empty DataFrames/dicts so Tushare remains the authoritative daily data source
and the main review pipeline can continue.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from loguru import logger

from core.utils.stock_code_utils import StockCodeUtils


class EltdxProvider:
    """Thin wrapper around ``eltdx.TdxClient`` with project-shaped outputs."""

    def __init__(self, timeout: float = 3.0, host: Optional[str] = None):
        self.timeout = timeout
        self.host = host

    @staticmethod
    def available() -> bool:
        try:
            import eltdx  # noqa: F401
            return True
        except Exception:
            return False

    def get_auction_series(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """Get current call-auction series, usually useful during 09:15-09:25.

        eltdx exposes live auction series rather than arbitrary historical
        series. ``trade_date`` is kept in the signature for DataManager
        consistency and for cache naming.
        """
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                if hasattr(client, "get_call_auction"):
                    series = client.get_call_auction(code)
                else:
                    series = client.auctions.series(code)
            return self._auction_series_to_frame(series, ts_code, trade_date)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] auction series unavailable {ts_code} {trade_date}: {e}")
            return pd.DataFrame()

    def get_auction_0925(self, ts_code: str, trade_date: str) -> dict:
        """Get 09:25 final auction snapshot for a stock and trading date."""
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                if hasattr(client, "get_auction_0925"):
                    result = client.get_auction_0925(code, self._parse_trade_date(trade_date))
                else:
                    helper = getattr(client, "helpers", None)
                    result = getattr(helper, "get_auction_data")(code, self._parse_trade_date(trade_date)).snapshot_0925
            return self._auction_0925_to_dict(result, ts_code, trade_date)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] auction 09:25 unavailable {ts_code} {trade_date}: {e}")
            return {}

    def get_minute_bars(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """Get intraday minute bars/points for the requested trading day."""
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                if hasattr(client, "get_minute"):
                    series = client.get_minute(code, self._parse_trade_date(trade_date))
                else:
                    if self._is_today(trade_date):
                        series = client.minutes.today(code)
                    else:
                        series = client.minutes.history(code, self._parse_trade_date(trade_date))
            return self._minute_series_to_frame(series, ts_code, trade_date)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] minute bars unavailable {ts_code} {trade_date}: {e}")
            return pd.DataFrame()

    def get_kline(self, ts_code: str, period: str = "day", count: int = 120) -> pd.DataFrame:
        """Get K-line bars from eltdx.

        ``period`` accepts eltdx names such as ``day``, ``week``, ``month``,
        ``1min``, ``5min``, etc. Common project aliases are normalized.
        """
        code = self._to_tdx_code(ts_code)
        period = self._normalize_period(period)
        try:
            with self._client() as client:
                if hasattr(client, "get_kline"):
                    series = client.get_kline(period, code, count=count)
                else:
                    series = client.bars.get(code, period=period, count=count)
            return self._kline_series_to_frame(series, ts_code)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] kline unavailable {ts_code} {period}: {e}")
            return pd.DataFrame()

    def _client(self):
        from eltdx import TdxClient

        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self.host:
            kwargs["host"] = self.host
        return TdxClient(**kwargs)

    @staticmethod
    def _to_tdx_code(ts_code: str) -> str:
        code = StockCodeUtils.standardize_code(ts_code, add_suffix=False)
        exchange = StockCodeUtils.get_exchange(ts_code)
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
        if not prefix:
            prefix = "sh" if code.startswith("6") else "bj" if code.startswith(("4", "8", "9")) else "sz"
        return f"{prefix}{code}"

    @staticmethod
    def _parse_trade_date(trade_date: str):
        return datetime.strptime(str(trade_date), "%Y%m%d").date()

    @classmethod
    def _is_today(cls, trade_date: str) -> bool:
        return cls._parse_trade_date(trade_date) == datetime.now().date()

    @staticmethod
    def _normalize_period(period: str) -> str:
        p = str(period or "day").lower()
        mapping = {
            "1d": "day",
            "d": "day",
            "daily": "day",
            "day": "day",
            "1w": "week",
            "w": "week",
            "weekly": "week",
            "week": "week",
            "1m": "1min",
            "1min": "1min",
            "5m": "5min",
            "5min": "5min",
            "15m": "15min",
            "15min": "15min",
            "30m": "30min",
            "30min": "30min",
            "60m": "60min",
            "60min": "60min",
            "month": "month",
            "1mo": "month",
            "1month": "month",
        }
        return mapping.get(p, p)

    @staticmethod
    def _obj_dict(obj: Any) -> dict:
        if obj is None:
            return {}
        if is_dataclass(obj):
            return asdict(obj)
        if isinstance(obj, dict):
            return obj
        return {k: getattr(obj, k) for k in dir(obj) if not k.startswith("_") and not callable(getattr(obj, k))}

    def _auction_series_to_frame(self, series: Any, ts_code: str, trade_date: str) -> pd.DataFrame:
        points = getattr(series, "points", None) or []
        rows = []
        for p in points:
            rows.append({
                "ts_code": StockCodeUtils.standardize_code(ts_code),
                "trade_date": trade_date,
                "time": getattr(p, "time_label", None),
                "time_seconds": getattr(p, "time_seconds", None),
                "price": getattr(p, "price", None),
                "matched_volume": getattr(p, "matched_volume", None),
                "matched_amount": getattr(p, "matched_amount_estimated", None),
                "unmatched_volume": getattr(p, "unmatched_volume", None),
                "unmatched_direction": getattr(p, "unmatched_direction_raw", None),
            })
        return pd.DataFrame(rows)

    def _auction_0925_to_dict(self, result: Any, ts_code: str, trade_date: str) -> dict:
        raw = self._obj_dict(result)
        has_auction = bool(raw.get("has_auction_0925", raw.get("price") is not None))
        if not raw or not has_auction:
            return {}
        price = raw.get("price")
        volume = raw.get("volume")
        amount = raw.get("amount")
        return {
            "ts_code": StockCodeUtils.standardize_code(ts_code),
            "trade_date": trade_date,
            "time": "09:25:00",
            "price": float(price) if price is not None else None,
            "volume": float(volume) if volume is not None else 0,
            "amount": float(amount) if amount is not None else 0,
            "side": raw.get("side"),
            "status": raw.get("status"),
            "pages_used": raw.get("pages_used"),
            "source_mode": raw.get("source_mode"),
            "source": "eltdx",
        }

    def _minute_series_to_frame(self, series: Any, ts_code: str, trade_date: str) -> pd.DataFrame:
        prev_close = getattr(series, "prev_close", None)
        points = getattr(series, "points", None) or []
        rows = []
        for p in points:
            dt = getattr(p, "time", None)
            price = getattr(p, "price", None)
            volume = getattr(p, "volume", None)
            rows.append({
                "ts_code": StockCodeUtils.standardize_code(ts_code),
                "date": trade_date,
                "time": getattr(p, "time_label", None) or (dt.strftime("%H:%M:%S") if dt else None),
                "datetime": dt.isoformat() if hasattr(dt, "isoformat") else None,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "avg_price": getattr(p, "avg_price", None),
                "volume": float(volume) if volume is not None else 0,
                "amount": (float(price) * float(volume) * 100.0) if price is not None and volume is not None else 0,
                "pre_close": prev_close,
                "source": "eltdx",
            })
        return pd.DataFrame(rows)

    def _kline_series_to_frame(self, series: Any, ts_code: str) -> pd.DataFrame:
        bars = getattr(series, "bars", None) or []
        rows = []
        for b in bars:
            dt = getattr(b, "time", None)
            rows.append({
                "ts_code": StockCodeUtils.standardize_code(ts_code),
                "trade_date": dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else None,
                "datetime": dt.isoformat() if hasattr(dt, "isoformat") else None,
                "open": getattr(b, "open", None),
                "high": getattr(b, "high", None),
                "low": getattr(b, "low", None),
                "close": getattr(b, "close", None),
                "vol": getattr(b, "volume_lots", None),
                "amount": getattr(b, "amount", None),
                "source": "eltdx",
            })
        return pd.DataFrame(rows)
