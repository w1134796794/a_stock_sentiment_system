"""Small Ashare-compatible fallback provider.

The original mpquant/Ashare project is intentionally tiny. This adapter keeps
the same spirit without adding a vendored copy: it uses Sina/Tencent public
endpoints for no-token minute and daily K-line fallback data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd
import requests
from loguru import logger

from core.utils.stock_code_utils import StockCodeUtils


class AshareProvider:
    """No-token fallback for minute bars and K-line data."""

    TENCENT_DAY_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    TENCENT_MIN_URL = "http://ifzq.gtimg.cn/appstock/app/kline/mkline"
    SINA_KLINE_URL = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def get_minute_bars(self, ts_code: str, trade_date: str, frequency: str = "1m", count: int = 320) -> pd.DataFrame:
        df = self.get_kline(ts_code, period=frequency, count=count)
        if df.empty:
            return df
        return df[df["trade_date"].astype(str) == str(trade_date)].copy()

    def get_kline(self, ts_code: str, period: str = "day", count: int = 120, end_date: Optional[str] = None) -> pd.DataFrame:
        period = self._normalize_period(period)
        code = StockCodeUtils.to_akshare_symbol(ts_code)

        for fetcher in (self._fetch_tencent, self._fetch_sina):
            try:
                df = fetcher(code, period, count, end_date)
                if not df.empty:
                    df["ts_code"] = StockCodeUtils.standardize_code(ts_code)
                    df["source"] = "ashare"
                    return df
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[AshareProvider] {fetcher.__name__} failed {ts_code} {period}: {e}")
        return pd.DataFrame()

    def _fetch_tencent(self, code: str, period: str, count: int, end_date: Optional[str]) -> pd.DataFrame:
        if period in {"day", "week", "month"}:
            unit = {"day": "day", "week": "week", "month": "month"}[period]
            end = self._format_end_date(end_date)
            url = f"{self.TENCENT_DAY_URL}?param={code},{unit},,{end},{int(count)},qfq"
            data = self.session.get(url, timeout=self.timeout).json()
            stock = data.get("data", {}).get(code, {})
            key = f"qfq{unit}" if f"qfq{unit}" in stock else unit
            rows = stock.get(key) or []
            return self._rows_to_frame(rows, daily=True)

        minutes = self._period_minutes(period)
        url = f"{self.TENCENT_MIN_URL}?param={code},m{minutes},,{int(count)}"
        data = self.session.get(url, timeout=self.timeout).json()
        rows = data.get("data", {}).get(code, {}).get(f"m{minutes}") or []
        return self._rows_to_frame(rows, daily=False)

    def _fetch_sina(self, code: str, period: str, count: int, end_date: Optional[str]) -> pd.DataFrame:
        frequency = {
            "day": "240",
            "week": "1200",
            "month": "7200",
            "1m": "1",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "60m": "60",
        }.get(period, "240")
        symbol = code.lower()
        url = f"{self.SINA_KLINE_URL}?symbol={symbol}&scale={frequency}&ma=no&datalen={int(count)}"
        data = self.session.get(url, timeout=self.timeout).json()
        rows = []
        for item in data or []:
            rows.append([
                item.get("day"),
                item.get("open"),
                item.get("close"),
                item.get("high"),
                item.get("low"),
                item.get("volume"),
            ])
        return self._rows_to_frame(rows, daily=period in {"day", "week", "month"})

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
            "week": "week",
            "1m": "1m",
            "1min": "1m",
            "5m": "5m",
            "5min": "5m",
            "15m": "15m",
            "15min": "15m",
            "30m": "30m",
            "30min": "30m",
            "60m": "60m",
            "60min": "60m",
            "1mo": "month",
            "1month": "month",
            "month": "month",
        }
        return mapping.get(p, p)

    @staticmethod
    def _period_minutes(period: str) -> int:
        if period.endswith("m") and period[:-1].isdigit():
            return int(period[:-1])
        return 1

    @staticmethod
    def _format_end_date(end_date: Optional[str]) -> str:
        if not end_date:
            return ""
        s = str(end_date)
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s.split(" ")[0]

    @staticmethod
    def _rows_to_frame(rows: list, daily: bool) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        normalized = []
        for row in rows:
            if not row or len(row) < 6:
                continue
            normalized.append({
                "datetime": str(row[0]),
                "open": AshareProvider._safe_float(row[1]),
                "close": AshareProvider._safe_float(row[2]),
                "high": AshareProvider._safe_float(row[3]),
                "low": AshareProvider._safe_float(row[4]),
                "volume": AshareProvider._safe_float(row[5]),
            })
        df = pd.DataFrame(normalized)
        if df.empty:
            return df
        dt = pd.to_datetime(df["datetime"], errors="coerce")
        df["trade_date"] = dt.dt.strftime("%Y%m%d")
        df["time"] = dt.dt.strftime("%H:%M:%S")
        if daily:
            df["time"] = "15:00:00"
        df["vol"] = df["volume"]
        df["amount"] = 0.0
        return df[["trade_date", "time", "datetime", "open", "high", "low", "close", "vol", "volume", "amount"]]

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

