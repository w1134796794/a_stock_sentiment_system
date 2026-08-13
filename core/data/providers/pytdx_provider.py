"""High-frequency realtime quotes backed by pytdx.

The provider keeps one TDX connection per process, fetches quotes in batches and
turns cumulative quote snapshots into incremental one-minute bars.  Historical
minute data remains the responsibility of eltdx; pytdx is used only for the
current trading session.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from threading import RLock
from time import monotonic
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.utils.stock_code_utils import StockCodeUtils


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SnapshotMinuteStore:
    """Aggregate cumulative quote snapshots without double-counting volume."""

    def __init__(self, max_days: int = 2) -> None:
        self.max_days = max(int(max_days), 1)
        self._bars: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        self._last_totals: Dict[str, Tuple[str, float, float]] = {}
        self._lock = RLock()

    def update(self, code: str, quote: Dict[str, Any], received_at: datetime) -> None:
        price = _float(quote.get("last_price"))
        if price <= 0:
            return
        trade_date = received_at.strftime("%Y%m%d")
        minute = received_at.strftime("%H:%M:00")
        cumulative_volume = max(_float(quote.get("vol_hand")), 0.0)
        cumulative_amount = max(_float(quote.get("amount_yuan")), 0.0)

        with self._lock:
            previous = self._last_totals.get(code)
            if previous and previous[0] == trade_date:
                volume_delta = max(cumulative_volume - previous[1], 0.0)
                amount_delta = max(cumulative_amount - previous[2], 0.0)
            else:
                # The first observation can occur halfway through the session.
                # Treat it as a baseline instead of assigning the whole day's
                # cumulative turnover to one minute.
                volume_delta = 0.0
                amount_delta = 0.0
            self._last_totals[code] = (trade_date, cumulative_volume, cumulative_amount)

            daily = self._bars.setdefault((trade_date, code), {})
            bar = daily.get(minute)
            if bar is None:
                daily[minute] = {
                    "ts_code": StockCodeUtils.standardize_code(code, add_suffix=True),
                    "trade_date": trade_date,
                    "time": minute,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume_delta,
                    "amount": amount_delta,
                    "source": "pytdx_snapshot_3s",
                }
            else:
                bar["high"] = max(_float(bar.get("high")), price)
                bar["low"] = min(_float(bar.get("low")) or price, price)
                bar["close"] = price
                bar["volume"] = _float(bar.get("volume")) + volume_delta
                bar["amount"] = _float(bar.get("amount")) + amount_delta
            self._prune(trade_date)

    def frame(self, code: str, trade_date: str) -> pd.DataFrame:
        with self._lock:
            rows = list(self._bars.get((trade_date, code), {}).values())
        return pd.DataFrame(rows).sort_values("time").reset_index(drop=True) if rows else pd.DataFrame()

    def seed(self, code: str, trade_date: str, rows: Iterable[Dict[str, Any]]) -> None:
        """Merge already completed minute bars without changing snapshot totals."""
        with self._lock:
            daily = self._bars.setdefault((trade_date, code), {})
            for source in rows:
                minute = str(source.get("time") or "")
                if not minute or minute in daily:
                    continue
                daily[minute] = dict(source)
            self._prune(trade_date)

    def _prune(self, current_date: str) -> None:
        dates = sorted({key[0] for key in self._bars})
        for old_date in dates[:-self.max_days]:
            for key in [key for key in self._bars if key[0] == old_date]:
                self._bars.pop(key, None)


class SnapshotTickStore:
    """Keep a bounded 3-second sequence and calculate cumulative deltas."""

    def __init__(self, max_items: int = 120, max_days: int = 2) -> None:
        self.max_items = max(int(max_items), 20)
        self.max_days = max(int(max_days), 1)
        self._rows: Dict[Tuple[str, str], Deque[Dict[str, Any]]] = {}
        self._last_totals: Dict[str, Tuple[str, float, float]] = {}
        self._lock = RLock()

    def update(self, code: str, quote: Dict[str, Any], received_at: datetime) -> Dict[str, Any]:
        trade_date = received_at.strftime("%Y%m%d")
        volume = max(_float(quote.get("vol_hand")), 0.0)
        amount = max(_float(quote.get("amount_yuan")), 0.0)
        with self._lock:
            previous = self._last_totals.get(code)
            if previous and previous[0] == trade_date:
                delta_volume = max(volume - previous[1], 0.0)
                delta_amount = max(amount - previous[2], 0.0)
            else:
                delta_volume = 0.0
                delta_amount = 0.0
            self._last_totals[code] = (trade_date, volume, amount)
            row = {
                **quote,
                "code": code,
                "date": trade_date,
                "time": received_at.strftime("%H:%M:%S"),
                "delta_volume": delta_volume,
                "delta_amount": delta_amount,
            }
            rows = self._rows.setdefault(
                (trade_date, code), deque(maxlen=self.max_items),
            )
            if rows and rows[-1].get("time") == row["time"]:
                rows[-1] = row
            else:
                rows.append(row)
            dates = sorted({key[0] for key in self._rows})
            for old_date in dates[:-self.max_days]:
                for old_key in [key for key in self._rows if key[0] == old_date]:
                    self._rows.pop(old_key, None)
            return dict(row)

    def frame(self, code: str, trade_date: str) -> pd.DataFrame:
        with self._lock:
            rows = list(self._rows.get((trade_date, code), ()))
        return pd.DataFrame(rows)


class PytdxProvider:
    """Persistent pytdx quote client with current-session minute aggregation."""

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 7709,
        timeout: float = 2.0,
        max_servers: int = 6,
        failure_cooldown_seconds: float = 60.0,
        clock: Any = None,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or 7709)
        self.timeout = max(float(timeout), 0.5)
        self.max_servers = max(int(max_servers), 1)
        self.failure_cooldown_seconds = max(float(failure_cooldown_seconds), 5.0)
        self.clock = clock or datetime.now
        self.minute_store = SnapshotMinuteStore()
        try:
            from core.realtime.snapshot_repository import RealtimeSnapshotRepository

            self.snapshot_repository = RealtimeSnapshotRepository()
        except Exception:  # pragma: no cover - startup fallback
            self.snapshot_repository = None
        self.tick_store = SnapshotTickStore(
            max_items=getattr(self.snapshot_repository, "max_items", 120),
        )
        self._api: Any = None
        self._connected_server: Tuple[str, int] | None = None
        self._seeded: set[Tuple[str, str]] = set()
        self._lock = RLock()
        self._last_error = ""
        self._disabled_until = 0.0
        self._server_cursor = 0

    @staticmethod
    def available() -> bool:
        try:
            import pytdx  # noqa: F401
            return True
        except Exception:
            return False

    def get_quote_snapshot(self, ts_code: str) -> Dict[str, Any]:
        code = self._code6(ts_code)
        return self.get_quote_snapshots([code]).get(code, {}) if code else {}

    def get_quote_snapshots(self, ts_codes: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        codes = list(dict.fromkeys(self._code6(code) for code in (ts_codes or [])))
        codes = [code for code in codes if code]
        if not codes:
            return {}
        if monotonic() < self._disabled_until:
            return {}
        received_at = self.clock()
        out: Dict[str, Dict[str, Any]] = {}
        stored_rows: List[Dict[str, Any]] = []
        try:
            with self._lock:
                api = self._ensure_connection()
                for offset in range(0, len(codes), 80):
                    request = [(self._market(code), code) for code in codes[offset:offset + 80]]
                    rows = api.get_security_quotes(request) or []
                    for raw in rows:
                        item = self._normalize_quote(raw, received_at)
                        code = item.get("code", "")
                        if code and item.get("last_price", 0) > 0:
                            out[code] = item
                            self.minute_store.update(code, item, received_at)
                            stored_rows.append(self.tick_store.update(code, item, received_at))
            if self.snapshot_repository is not None:
                self.snapshot_repository.append_batch(received_at.strftime("%Y%m%d"), stored_rows)
            self._last_error = ""
            return out
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._disabled_until = monotonic() + self.failure_cooldown_seconds
            if not self.host:
                self._server_cursor += self.max_servers
            self._disconnect()
            logger.warning(f"[PytdxProvider] 实时快照失败，等待降级行情源: {exc}")
            return {}

    def get_minute_bars(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """Return current-session minute bars seeded once then updated every snapshot."""
        code = self._code6(ts_code)
        date = str(trade_date or "").replace("-", "")[:8]
        if not code or date != self.clock().strftime("%Y%m%d"):
            return pd.DataFrame()
        key = (date, code)
        if key not in self._seeded:
            self._seed_minute_history(code, date)
        return self.minute_store.frame(code, date)

    def get_snapshot_ticks(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        code = self._code6(ts_code)
        date = str(trade_date or "").replace("-", "")[:8]
        local = self.tick_store.frame(code, date)
        if len(local) >= 2 or self.snapshot_repository is None:
            return local
        return pd.DataFrame(self.snapshot_repository.read(date, code))

    def health(self) -> Dict[str, Any]:
        return {
            "available": self.available(),
            "connected": self._api is not None,
            "server": self._connected_server,
            "last_error": self._last_error,
            "cooldown_remaining_seconds": round(max(self._disabled_until - monotonic(), 0.0), 1),
            "source": "pytdx_snapshot_3s",
        }

    def _seed_minute_history(self, code: str, trade_date: str) -> None:
        key = (trade_date, code)
        try:
            with self._lock:
                api = self._ensure_connection()
                rows = api.get_minute_time_data(self._market(code), code) or []
            times = self._trading_minutes(len(rows))
            seed_rows: List[Dict[str, Any]] = []
            for index, raw in enumerate(rows):
                price = _float(raw.get("price"))
                if price <= 0:
                    continue
                raw_time = str(raw.get("time") or raw.get("datetime") or "")
                minute = self._normalize_time(raw_time) or times[index]
                volume = max(_float(raw.get("vol")), 0.0)
                amount = max(_float(raw.get("amount")), price * volume * 100.0)
                seed_rows.append({
                    "ts_code": StockCodeUtils.standardize_code(code, add_suffix=True),
                    "trade_date": trade_date,
                    "time": minute,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                    "amount": amount,
                    "source": "pytdx_minute_seed",
                })
            self.minute_store.seed(code, trade_date, seed_rows)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self._disconnect()
            logger.debug(f"[PytdxProvider] 分时种子获取失败 {code}: {exc}")
        finally:
            self._seeded.add(key)

    def _ensure_connection(self):
        if self._api is not None:
            return self._api
        from pytdx.hq import TdxHq_API

        errors: List[str] = []
        for host, port in self._server_candidates():
            api = TdxHq_API(heartbeat=True, auto_retry=True, raise_exception=True)
            try:
                if api.connect(host, port, time_out=self.timeout):
                    self._api = api
                    self._connected_server = (host, port)
                    self._disabled_until = 0.0
                    logger.info(f"[PytdxProvider] 已连接行情服务器 {host}:{port}")
                    return api
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{host}:{port} {exc}")
            try:
                api.disconnect()
            except Exception:
                pass
        raise ConnectionError("pytdx 行情服务器连接失败: " + "; ".join(errors[-3:]))

    def _disconnect(self) -> None:
        with self._lock:
            api, self._api = self._api, None
            self._connected_server = None
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass

    def _server_candidates(self) -> List[Tuple[str, int]]:
        if self.host:
            return [(self.host, self.port)]
        from pytdx.config.hosts import hq_hosts

        all_candidates: List[Tuple[str, int]] = []
        for row in hq_hosts:
            if len(row) >= 3:
                all_candidates.append((str(row[1]), int(row[2])))
        if not all_candidates:
            return []
        start = self._server_cursor % len(all_candidates)
        ordered = all_candidates[start:] + all_candidates[:start]
        return ordered[:self.max_servers]

    def _normalize_quote(self, raw: Dict[str, Any], received_at: datetime) -> Dict[str, Any]:
        code = self._code6(raw.get("code"))
        last = _float(raw.get("price"))
        pre_close = _float(raw.get("last_close"))
        return {
            "code": code,
            "ts_code": StockCodeUtils.standardize_code(code, add_suffix=True),
            "name": "",
            "open_price": _float(raw.get("open")),
            "pre_close": pre_close,
            "last_price": last,
            "high_price": _float(raw.get("high")),
            "low_price": _float(raw.get("low")),
            "bid1": _float(raw.get("bid1")),
            "ask1": _float(raw.get("ask1")),
            "bid_vol1": _float(raw.get("bid_vol1")),
            "ask_vol1": _float(raw.get("ask_vol1")),
            "vol_hand": _float(raw.get("vol")),
            "current_volume_hand": _float(raw.get("cur_vol")),
            "amount_yuan": _float(raw.get("amount")),
            "change_pct": ((last / pre_close - 1.0) * 100.0) if last > 0 and pre_close > 0 else None,
            "date": received_at.strftime("%Y%m%d"),
            "time": received_at.strftime("%H:%M:%S"),
            "source": "pytdx_snapshot_3s",
            "server": f"{self._connected_server[0]}:{self._connected_server[1]}" if self._connected_server else "",
        }

    @staticmethod
    def _code6(value: Any) -> str:
        try:
            return StockCodeUtils.standardize_code(str(value), add_suffix=False)
        except Exception:
            return ""

    @staticmethod
    def _market(code: str) -> int:
        return 1 if str(code).startswith(("5", "6", "9")) else 0

    @staticmethod
    def _normalize_time(value: str) -> str:
        text = str(value or "").strip().split(" ")[-1]
        if len(text) == 5 and text[2] == ":":
            return f"{text}:00"
        return text[:8] if len(text) >= 8 and ":" in text else ""

    @staticmethod
    def _trading_minutes(size: int) -> List[str]:
        morning = [datetime(2000, 1, 1, 9, 30) + timedelta(minutes=i) for i in range(120)]
        afternoon = [datetime(2000, 1, 1, 13, 0) + timedelta(minutes=i) for i in range(120)]
        labels = [item.strftime("%H:%M:%S") for item in morning + afternoon]
        return labels[:size] + [labels[-1]] * max(size - len(labels), 0)


_PROVIDER: Optional[PytdxProvider] = None
_PROVIDER_LOCK = RLock()


def get_pytdx_provider(**kwargs: Any) -> PytdxProvider:
    """Return the process-wide provider shared by quote and entry services."""
    global _PROVIDER
    if _PROVIDER is None:
        with _PROVIDER_LOCK:
            if _PROVIDER is None:
                _PROVIDER = PytdxProvider(**kwargs)
    return _PROVIDER


__all__ = [
    "PytdxProvider", "SnapshotMinuteStore", "SnapshotTickStore", "get_pytdx_provider",
]
