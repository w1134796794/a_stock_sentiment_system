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
        """Get intraday minute bars/points for the requested trading day.

        ``get_minute`` 是「当日分时」接口（无 trade_date 入参）；历史交易日必须走
        ``get_history_minute(code, 'YYYY-MM-DD')``（eltdx>=0.4）。
        """
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                if self._is_today(trade_date):
                    series = client.get_minute(code)
                else:
                    series = client.get_history_minute(code, self._to_dash_date(trade_date))
            return self._minute_series_to_frame(series, ts_code, trade_date)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] minute bars unavailable {ts_code} {trade_date}: {e}")
            return pd.DataFrame()

    def get_quote_snapshot(self, ts_code: str) -> dict:
        """最新实时行情快照，含开盘集合竞价撮合额。

        ``open_amount_yuan`` 是 09:25 开盘集合竞价的撮合成交额（元），仅对**最近/当前
        交易日**有意义（该接口不接受日期参数，返回的是最新一笔快照）。
        """
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                q = client.get_quote(code)
            q = q[0] if isinstance(q, (list, tuple)) and q else q
            if q is None:
                return {}
            open_price = getattr(q, "open_price", None)
            open_amount = getattr(q, "open_amount_yuan", None)
            return {
                "open_price": float(open_price) if open_price else 0.0,
                "open_amount": float(open_amount) if open_amount else 0.0,
                "pre_close": float(getattr(q, "pre_close_price", 0) or 0),
                "last_price": float(getattr(q, "last_price", 0) or 0),
                "source": "eltdx_quote",
            }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] quote snapshot unavailable {ts_code}: {e}")
            return {}

    def get_quote_snapshots(self, ts_codes) -> dict:
        """**批量**取实时行情快照（一条连接、按 80 只分块）。

        相比逐只 ``get_quote_snapshot``（每只都新建 TCP 连接），批量接口
        ``client.quotes.get_snapshots`` 在单条连接里一次拉多只，数十只仅需
        百毫秒级，适合候选池盘中轮询。

        Returns:
            dict: ``{6位代码: {open_price, open_amount, pre_close, last_price,
                   change_pct, source}}``；eltdx 不可用或异常时返回空字典。
        """
        codes = list(ts_codes or [])
        if not codes:
            return {}
        tdx_codes = [self._to_tdx_code(c) for c in codes]
        out: dict = {}
        try:
            with self._client() as client:
                for i in range(0, len(tdx_codes), 80):
                    chunk = tdx_codes[i:i + 80]
                    snaps = client.quotes.get_snapshots(chunk) or []
                    for s in snaps:
                        code6 = str(getattr(s, "code", "") or "").split(".")[0].zfill(6)
                        if not code6 or code6 == "000000":
                            continue
                        last = getattr(s, "last_price", None)
                        pre = getattr(s, "pre_close_price", None)
                        op = getattr(s, "open_price", None)
                        oa = getattr(s, "open_amount_yuan", None)
                        chg = getattr(s, "change_pct", None)
                        out[code6] = {
                            "open_price": float(op) if op else 0.0,
                            "open_amount": float(oa) if oa else 0.0,
                            "pre_close": float(pre) if pre else 0.0,
                            "last_price": float(last) if last else 0.0,
                            "change_pct": float(chg) if chg is not None else None,
                            "source": "eltdx_quotes_batch",
                        }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] batch quote snapshots unavailable ({len(codes)} codes): {e}")
            return {}
        return out

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

    @staticmethod
    def _to_dash_date(trade_date: str) -> str:
        """``20260605`` → ``2026-06-05``（eltdx 历史接口要求带连字符的日期串）。"""
        s = str(trade_date).replace("-", "")
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else str(trade_date)

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
