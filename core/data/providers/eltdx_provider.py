"""eltdx adapter for intraday, K-line and call-auction data.

This module keeps eltdx optional. Import errors or socket failures are surfaced
as empty DataFrames/dicts so Tushare remains the authoritative daily data source
and the main review pipeline can continue.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import re
from time import monotonic
from threading import RLock
from collections import Counter
from typing import Any, Optional

import pandas as pd
from loguru import logger

from core.data.providers.eltdx_evidence import EltdxEvidenceMixin
from core.data.providers.eltdx_research import EltdxResearchMixin
from core.realtime.market_evidence import normalize_depth
from core.utils.stock_code_utils import StockCodeUtils
from core.realtime.trading_session import shanghai_now


class QuoteFailureState:
    """Collector-scoped cooldowns shared across short-lived batch providers."""
    def __init__(self, clock=monotonic):
        self.clock, self.lock, self.entries = clock, RLock(), {}

    def active(self, key):
        with self.lock:
            item = self.entries.get(key)
            if item and item[0] > self.clock():
                return item[1:]
            self.entries.pop(key, None)
        return None

    def put(self, key, reason, category, seconds):
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if v[0] > self.clock()}
            self.entries[key] = (self.clock() + seconds, reason, category)


def quote_failure_category(exc):
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return "timeout"
    if "request failed" in text or "0x" in text:
        return "protocol_request_failed"
    return "transport_or_decode_error"


class EltdxProvider(EltdxEvidenceMixin, EltdxResearchMixin):
    """Thin wrapper around ``eltdx.TdxClient`` with project-shaped outputs."""

    def __init__(self, timeout: float = 3.0, host: Optional[str] = None, *, failure_state=None):
        self.timeout = timeout
        self.host = host
        self.failure_state = failure_state or QuoteFailureState()
        self.last_quote_diagnostics: dict = {}
        self.last_minute_diagnostics: dict = {}

    @staticmethod
    def available() -> bool:
        try:
            import eltdx  # noqa: F401
            return True
        except Exception:
            return False

    def get_auction_series(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """Get auction snapshots for the requested date; history requires a date-aware API."""
        code = self._to_tdx_code(ts_code)
        try:
            with self._client() as client:
                if hasattr(client, "auctions"):
                    series = client.auctions.series(code, self._to_dash_date(trade_date))
                    self._validate_evidence_identity(series, ts_code, trade_date)
                elif self._is_today(trade_date):
                    series = client.get_call_auction(code)
                else:
                    raise NotImplementedError("Legacy auction API cannot query historical dates")
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
                    if hasattr(helper, "auction_data"):
                        result = helper.auction_data(code, self._parse_trade_date(trade_date),
                            include_series=False, include_quote=False).snapshot_0925
                    else:
                        result = helper.get_auction_data(
                            code, self._parse_trade_date(trade_date)
                        ).snapshot_0925
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
        started = monotonic()
        try:
            with self._client() as client:
                minutes = getattr(client, "minutes", None)
                if minutes is not None:
                    series = (minutes.today(code) if self._is_today(trade_date)
                              else minutes.history(code, self._to_dash_date(trade_date)))
                elif self._is_today(trade_date):
                    series = client.get_minute(code)
                else:
                    series = client.get_history_minute(code, self._to_dash_date(trade_date))
            frame = self._minute_series_to_frame(series, ts_code, trade_date)
            self.last_minute_diagnostics = {"code": code, "ok": not frame.empty,
                "category": "success" if not frame.empty else "empty_response",
                "duration_ms": round((monotonic() - started) * 1000, 3)}
            return frame
        except Exception as e:  # noqa: BLE001
            self.last_minute_diagnostics = {"code": code, "ok": False, "category": quote_failure_category(e),
                "duration_ms": round((monotonic() - started) * 1000, 3)}
            logger.debug(f"[EltdxProvider] minute bars unavailable {ts_code} {trade_date}: {e}")
            return pd.DataFrame()

    def get_quote_snapshot(self, ts_code: str) -> dict:
        """最新实时行情快照，含开盘集合竞价撮合额。

        ``open_amount_yuan`` 是 09:25 开盘集合竞价的撮合成交额（元），仅对**最近/当前
        交易日**有意义（该接口不接受日期参数，返回的是最新一笔快照）。
        """
        code6 = StockCodeUtils.standardize_code(ts_code, add_suffix=False)
        return self.get_quote_snapshots([ts_code]).get(code6, {})

    def get_minute_ohlcv(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """Collector-only real one-minute candles; never promote minute points."""
        started = monotonic()
        frame = self.get_kline(ts_code, period="1min", count=800)
        if not frame.empty:
            stamp = pd.to_datetime(frame["datetime"], errors="coerce")
            frame = frame[stamp.dt.strftime("%Y%m%d").eq(str(trade_date))].copy()
            frame["date"] = str(trade_date)
            frame["time"] = stamp.loc[frame.index].dt.strftime("%H:%M:%S")
            frame = frame.rename(columns={"vol": "volume"})
        self.last_minute_diagnostics = {"code": self._to_tdx_code(ts_code), "ok": not frame.empty,
            "category": "success" if not frame.empty else "missing_requested_day_ohlc",
            "bar_kind": "minute_ohlcv", "duration_ms": round((monotonic() - started) * 1000, 3)}
        return frame

    def get_quote_snapshots(self, ts_codes) -> dict:
        """**批量**取实时行情快照（一条连接、按 80 只分块）。

        相比逐只 ``get_quote_snapshot``（每只都新建 TCP 连接），批量接口
        ``client.quotes.get_snapshots`` 在单条连接里一次拉多只，数十只仅需
        百毫秒级，适合候选池盘中轮询。

        Returns:
            dict: ``{6位代码: {open_price, pre_close, last_price, high_price,
                   low_price, vol_hand, amount_yuan, bid1, ask1, source}}``；
                   eltdx 不可用或异常时返回空字典。
        """
        codes = list(ts_codes or [])
        if not codes:
            return {}
        tdx_codes = [self._to_tdx_code(c) for c in codes]
        out: dict = {}
        failures = {}
        categories, batches = {}, []
        started = monotonic()
        try:
            with self._client() as client:
                for i in range(0, len(tdx_codes), 80):
                    batch_start = monotonic()
                    batch_started_at = shanghai_now().isoformat(timespec="milliseconds")
                    requested = tdx_codes[i:i + 80]
                    batch_key = ("chunk", tuple(requested))
                    blocked = self.failure_state.active(batch_key)
                    chunk = []
                    for code in requested:
                        unavailable = self.failure_state.active(("symbol", code)) or blocked
                        if unavailable:
                            failures[code], categories[code] = unavailable
                        else:
                            chunk.append(code)
                    snaps = []
                    while chunk:
                        try:
                            snaps = client.quotes.get_snapshots(chunk) or []
                            if not snaps:
                                failures.update({code: "empty_response" for code in chunk})
                                categories.update({code: "empty_response" for code in chunk})
                                self.failure_state.put(batch_key, "empty_response", "empty_response", 2)
                            break
                        except Exception as exc:
                            # eltdx names the missing record; isolate only that exact requested symbol.
                            match = re.fullmatch(r"snapshot record marker not found: ((?:sz|sh|bj)\d{6})", str(exc))
                            if not match or match[1] not in chunk:
                                failures.update({code: str(exc) for code in chunk})
                                category = quote_failure_category(exc)
                                categories.update({code: category for code in chunk})
                                self.failure_state.put(batch_key, str(exc), category, 2 if category == "timeout" else 5)
                                logger.debug("[EltdxProvider] snapshot chunk unavailable: {}", exc)
                                break
                            code = match[1]
                            failures[code] = str(exc)
                            categories[code] = "symbol_record_missing"
                            self.failure_state.put(("symbol", code), str(exc), "symbol_record_missing", 300)
                            chunk.remove(code)
                            logger.warning("[EltdxProvider] temporarily isolate missing snapshot {}: {}", code, exc)
                    received_at = shanghai_now()
                    for s in snaps:
                        code6 = str(getattr(s, "code", "") or "").split(".")[0].zfill(6)
                        if not code6 or code6 == "000000":
                            continue
                        out[code6] = self._normalize_quote_snapshot(s, received_at)
                    batches.append({"requested_codes": requested, "received_count": len(snaps),
                                    "started_at": batch_started_at,
                                    "suppressed_count": len(requested) - len(chunk),
                                    "completed_at": received_at.isoformat(timespec="milliseconds"),
                                    "duration_ms": round((monotonic() - batch_start) * 1000, 3)})
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EltdxProvider] batch quote snapshots unavailable ({len(codes)} codes): {e}")
            failures.update({code: str(e) for code in tdx_codes if code[-6:] not in out})
            categories.update({code: quote_failure_category(e) for code in tdx_codes if code[-6:] not in out})
        for code in tdx_codes:
            if code[-6:] not in out and code not in categories:
                categories[code] = "not_returned"
        self.last_quote_diagnostics = {
            "requested_count": len(codes), "returned_count": len(out),
            "missing_codes": [code for code in codes if StockCodeUtils.standardize_code(code, add_suffix=False) not in out],
            "failures": failures,
            "failure_categories": dict(Counter(categories.values())), "code_failure_categories": categories,
            "batches": batches, "duration_ms": round((monotonic() - started) * 1000, 3),
        }
        return out

    @classmethod
    def _normalize_quote_snapshot(cls, snapshot: Any, received_at: datetime) -> dict:
        """Normalize one eltdx quote without changing its documented units."""
        code6 = str(getattr(snapshot, "code", "") or "").split(".")[0].zfill(6)
        last = cls._float(getattr(snapshot, "last_price", None))
        pre_close = cls._float(getattr(snapshot, "pre_close_price", None))
        bid1, bid_vol1 = cls._first_level(getattr(snapshot, "buy_levels", None))
        ask1, ask_vol1 = cls._first_level(getattr(snapshot, "sell_levels", None))
        return {
            "code": code6,
            "ts_code": StockCodeUtils.standardize_code(code6),
            "open_price": cls._float(getattr(snapshot, "open_price", None)),
            "open_amount": cls._float(getattr(snapshot, "open_amount_yuan", None)),
            "pre_close": pre_close,
            "last_price": last,
            "high_price": cls._float(getattr(snapshot, "high_price", None)),
            "low_price": cls._float(getattr(snapshot, "low_price", None)),
            "bid1": bid1,
            "ask1": ask1,
            "bid_vol1": bid_vol1,
            "ask_vol1": ask_vol1,
            # eltdx total_hand/level volume are hands; amount is yuan.
            "vol_hand": cls._float(getattr(snapshot, "total_hand", None)),
            "amount_yuan": cls._float(getattr(snapshot, "amount", None)),
            "current_hand": cls._float(getattr(snapshot, "current_hand", None)),
            "inside_hand": cls._float(getattr(snapshot, "inside_dish", None)),
            "outside_hand": cls._float(getattr(snapshot, "outer_disc", None)),
            "change_pct": ((last - pre_close) / pre_close * 100.0) if pre_close > 0 else None,
            "date": received_at.strftime("%Y%m%d"),
            # eltdx documents time_raw as an opaque protocol value, not HHMMSS.
            # Freshness therefore uses the successful receive time.
            "time": received_at.strftime("%H:%M:%S"),
            "source_time_raw": getattr(snapshot, "time_raw", None),
            "received_at": received_at.isoformat(timespec="milliseconds"),
            "source": "eltdx_batch",
            "depth": normalize_depth(snapshot),
        }

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _first_level(cls, levels: Any) -> tuple[float, float]:
        level = levels[0] if levels else None
        if level is None:
            return 0.0, 0.0
        if isinstance(level, dict):
            return cls._float(level.get("price")), cls._float(level.get("volume"))
        return cls._float(getattr(level, "price", None)), cls._float(getattr(level, "volume", None))

    def prepare_board_catalogue(self, *, category="全部") -> dict:
        """Preopen only: catalogue rows are definitions, not successful quote counts."""
        with self._client() as client:
            table = client.helpers.board_quotes(category=category)
        return {"prepared_date": table.prepared_date.strftime("%Y%m%d"),
                "rows": [{"code": row.full_code, "name": row.board_name} for row in table.rows]}

    def prepare_board_members(self, full_code: str) -> dict:
        """Preopen membership audit; absence from the SDK list is not delisting proof."""
        with self._client() as client:
            table = client.helpers.board_member_quotes(full_code)
        return {"prepared_date": table.prepared_date.strftime("%Y%m%d"),
                "members": [str(row["code"]).zfill(6) for row in table.display_members],
                "excluded_members": [dict(row) for row in table.excluded_members]}

    def get_board_snapshots(self, full_codes) -> dict:
        """Fetch frozen board identities without stock-code inference or preparation."""
        import re
        codes = list(dict.fromkeys(full_codes))
        if any(not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code) for code in codes):
            raise ValueError("Board quotes require explicit market-prefixed codes")
        rows, batches = {}, []
        with self._client() as client:
            for offset in range(0, len(codes), 80):
                chunk = codes[offset:offset + 80]
                started = monotonic()
                try:
                    snapshots = client.quotes.get_snapshots(chunk) or []
                    received = shanghai_now()
                    for snapshot in snapshots:
                        full_code = str(getattr(snapshot, "full_code", ""))
                        if full_code not in chunk:
                            continue
                        row = self._normalize_quote_snapshot(snapshot, received)
                        row.pop("ts_code", None)
                        row.update(code=full_code, date=received.strftime("%Y%m%d"),
                                   source="eltdx_board_snapshot", exchange_timestamp_verified=False)
                        rows[full_code] = row
                    batches.append({"codes": chunk, "received_count": sum(c in rows for c in chunk),
                                    "duration_ms": round((monotonic() - started) * 1000, 3)})
                except Exception as exc:
                    batches.append({"codes": chunk, "received_count": 0,
                                    "category": quote_failure_category(exc),
                                    "duration_ms": round((monotonic() - started) * 1000, 3)})
        return {"rows": list(rows.values()), "batches": batches,
                "missing_codes": [code for code in codes if code not in rows]}

    def get_kline(self, ts_code: str, period: str = "day", count: int = 120) -> pd.DataFrame:
        """Get K-line bars from eltdx.

        ``period`` accepts eltdx names such as ``day``, ``week``, ``month``,
        ``1min``, ``5min``, etc. Common project aliases are normalized.
        """
        code = self._to_tdx_code(ts_code)
        period = self._normalize_period(period)
        try:
            with self._client() as client:
                native_bars = hasattr(client, "bars")
                if native_bars:
                    # Keep the wire label: eltdx end conversion shifts native 09:31 to 09:32.
                    series = client.bars.get(code, period=period, count=count)
                else:
                    series = client.get_kline(period, code, count=count)
            frame = self._kline_series_to_frame(series, ts_code)
            if not frame.empty:
                frame["bar_label"] = "end" if native_bars and period == "1min" else "unknown"
                frame["timestamp_policy"] = "eltdx_native_v1"
            return frame
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
        return cls._parse_trade_date(trade_date) == shanghai_now().date()

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
        if raw.get("event_kind") is not None and (
                raw["event_kind"] != "opening_match" or raw.get("time_label") != "09:25"):
            return {}
        amount = raw.get("amount")
        if amount is None:
            amount = getattr(result, "trade_amount_yuan", None)
        return {
            "ts_code": StockCodeUtils.standardize_code(ts_code),
            "trade_date": trade_date,
            "time": "09:25:00",
            "price": float(price) if price is not None else None,
            "volume": float(volume) if volume is not None else 0,
            "amount": float(amount) if amount is not None else 0,
            "side": raw.get("side"),
            "status": raw.get("status", raw.get("status_raw")),
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
                "volume_unit": "lots",
                "amount_unit": "yuan",
                "amount_is_estimated": True,
                "bar_kind": "minute_price_point",
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
                "volume_unit": "lots",
                "amount_unit": "yuan",
                "amount_is_estimated": False,
                "bar_kind": "minute_ohlcv",
                "amount": getattr(b, "amount", None),
                "source": "eltdx",
            })
        return pd.DataFrame(rows)
