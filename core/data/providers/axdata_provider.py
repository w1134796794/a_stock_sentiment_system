"""Local AxData SDK adapter for explicit historical acquisition only."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from time import monotonic

import numpy as np
import pandas as pd

from core.realtime.trading_session import shanghai_now
from core.utils.stock_code_utils import StockCodeUtils


class AxDataProvider:
    def __init__(self, *, data_root=None, client=None):
        if client is None:
            from axdata import AxDataClient

            # An ambient AXDATA_API_BASE must not silently turn this into HTTP.
            client = AxDataClient(mode="local", data_root=data_root)
        self.client = client
        self._blocked_until = {}

    @staticmethod
    def available():
        try:
            return version("axdata")
        except PackageNotFoundError:
            return ""

    def _request(self, interface, code, **params):
        code = StockCodeUtils.standardize_code(code, add_suffix=True)
        if self._blocked_until.get(interface, 0) > monotonic():
            raise RuntimeError("AxData source cooldown after previous upstream failure")
        try:
            frame = self.client.call(interface, code=code, **params)
        except Exception as exc:
            if isinstance(exc, (TimeoutError, ConnectionError)) or getattr(exc, "code", "") in {
                "SOURCE_UNAVAILABLE",
                "SOURCE_INTERFACE_NOT_FOUND",
                "SOURCE_ADAPTER_NOT_FOUND",
            }:
                self._blocked_until[interface] = monotonic() + 30
            raise
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("AxData SDK response must be a DataFrame")
        if frame.empty:
            return frame.copy()
        if "instrument_id" not in frame or not frame["instrument_id"].eq(code).all():
            raise ValueError("AxData security identity mismatch")
        frame = frame.copy()
        frame["ts_code"], frame["source"], frame["source_interface"] = code, "axdata", interface
        frame["captured_at"] = shanghai_now().isoformat()
        return frame

    @staticmethod
    def _stamp(frame, column):
        def local(value):
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                raise ValueError("AxData invalid timestamp")
            return stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None else stamp.tz_convert("Asia/Shanghai")

        if column not in frame:
            raise ValueError("AxData missing timestamp")
        return frame[column].map(local)

    @staticmethod
    def _past(date):
        value = pd.to_datetime(date, format="%Y%m%d").strftime("%Y%m%d")
        if value >= shanghai_now().strftime("%Y%m%d"):
            raise ValueError("AxData backfill only accepts completed historical dates")
        return value

    @classmethod
    def _bars(cls, frame, period):
        if frame.empty:
            return frame
        if "period" not in frame or not frame["period"].eq(period).all():
            raise ValueError("AxData bar period mismatch")
        if "adjust" in frame and not frame["adjust"].eq("none").all():
            raise ValueError("AxData adjusted prices cannot enter an unadjusted cache")
        for key in ("open", "high", "low", "close", "volume", "amount"):
            if key not in frame:
                raise ValueError("AxData missing OHLC/volume/amount fields")
            frame[key] = pd.to_numeric(frame[key], errors="coerce")
            if (
                not np.isfinite(frame[key]).all()
                or (frame[key] < (0 if key in {"volume", "amount"} else 0.000001)).any()
            ):
                raise ValueError("AxData invalid OHLC/volume/amount values")
        if (
            frame["high"].lt(frame[["open", "low", "close"]].max(axis=1))
            | frame["low"].gt(frame[["open", "high", "close"]].min(axis=1))
        ).any():
            raise ValueError("AxData invalid OHLC structure")
        shares = frame["volume"] * 100.0
        # Reject unit drift instead of silently mixing yuan and thousands of yuan.
        if (
            (
                shares.gt(0)
                & (frame["amount"].lt(frame["low"] * shares * 0.99) | frame["amount"].gt(frame["high"] * shares * 1.01))
            )
            | (shares.eq(0) & frame["amount"].ne(0))
        ).any():
            raise ValueError("AxData amount/volume units disagree with OHLC")
        frame["volume_unit"], frame["volume_shares"] = "hand", shares
        frame["amount_is_estimated"], frame["price_adjustment"] = False, "none"
        return frame

    def get_minute_bars(self, code, date):
        date = self._past(date)
        frame = self._request("stock_kline_minute_tdx", code, period="1m", adjust="none")
        if frame.empty:
            return frame
        stamps = self._stamp(frame, "trade_time")
        frame["trade_date"], frame["time"] = stamps.dt.strftime("%Y%m%d"), stamps.dt.strftime("%H:%M:%S")
        # This endpoint has no trade_date parameter. Never relabel its recent bars.
        frame = frame.loc[frame["trade_date"].eq(date)].copy()
        return self._bars(frame, "1m").sort_values("time").reset_index(drop=True)

    def get_daily_bars(self, code, date):
        date = self._past(date)
        frame = self._request("stock_kline_daily_tdx", code, adjust="none")
        if frame.empty:
            return frame
        stamps = self._stamp(frame, "trade_time")
        frame["trade_date"] = stamps.dt.strftime("%Y%m%d")
        frame = self._bars(frame.loc[frame["trade_date"].eq(date)].copy(), "day")
        if frame.empty:
            return frame
        if len(frame) != 1:
            raise ValueError("AxData duplicate daily date")
        frame["vol"], frame["amount_yuan"] = frame["volume"], frame["amount"]
        frame["amount"] = frame["amount"] / 1000.0  # Existing daily contract: thousand yuan.
        frame["pre_close"], frame["change"], frame["pct_chg"] = 0.0, 0.0, 0.0
        frame["prior_close_available"] = False  # Not supplied by the documented K-line API.
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
        return frame.reset_index(drop=True)

    def get_auction_0925(self, code, date):
        date = self._past(date)
        frame = self._request("stock_auction_result_history_tdx", code, trade_date=date)
        if frame.empty:
            return {}
        stamps = self._stamp(frame, "auction_datetime")
        if not stamps.dt.strftime("%Y%m%d%H%M%S").eq(date + "092500").all():
            raise ValueError("AxData auction date/time mismatch")
        if (
            "trade_date" not in frame
            or not pd.to_datetime(frame["trade_date"].astype(str)).dt.strftime("%Y%m%d").eq(date).all()
        ):
            raise ValueError("AxData auction trade date mismatch")
        if "trade_index" not in frame or frame["trade_index"].duplicated().any():
            raise ValueError("AxData duplicate or missing auction trade identity")
        for key in ("price", "volume", "amount"):
            frame[key] = pd.to_numeric(frame[key], errors="coerce")
            if not np.isfinite(frame[key]).all() or frame[key].le(0).any():
                raise ValueError("AxData invalid auction values")
        if (
            frame["price"].nunique() != 1
            or not np.isclose(frame["amount"], frame["price"] * frame["volume"] * 100, rtol=0.01).all()
        ):
            raise ValueError("AxData auction price/amount/volume mismatch")
        return dict(
            price=float(frame["price"].iloc[0]),
            volume=float(frame["volume"].sum()),
            amount=float(frame["amount"].sum()),
            source="axdata_0925",
            captured_at=frame["captured_at"].iloc[0],
        )
