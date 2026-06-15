"""Realtime quote models and normalization helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from core.utils.stock_code_utils import StockCodeUtils


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if text in ("", "-", "--", "None", "nan"):
                return default
            if text.endswith("%"):
                text = text[:-1]
            return float(text)
        return float(value)
    except Exception:
        return default


def pick(raw: Dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in raw and raw.get(name) not in (None, ""):
            return raw.get(name)
    return default


def normalize_stock_code(value: Any, *, add_suffix: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if len(text) >= 8 and lower[:2] in ("sh", "sz", "bj") and text[2:8].isdigit():
        text = text[2:8]
    try:
        return StockCodeUtils.standardize_code(text, add_suffix=add_suffix)
    except Exception:
        if "." in text:
            text = text.split(".")[0]
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits.zfill(6) if digits else ""


def parse_quote_datetime(date_value: Any, time_value: Any) -> Optional[datetime]:
    date_text = str(date_value or "").strip().replace("/", "-")
    time_text = str(time_value or "").strip()
    if not date_text:
        return None
    if date_text.isdigit() and len(date_text) == 8:
        date_text = f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}"
    if time_text.isdigit() and len(time_text) == 6:
        time_text = f"{time_text[:2]}:{time_text[2:4]}:{time_text[4:6]}"
    if not time_text:
        time_text = "15:00:00"
    if len(time_text) == 5:
        time_text += ":00"
    try:
        return datetime.strptime(f"{date_text} {time_text[:8]}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


@dataclass
class QuoteSnapshot:
    code: str
    ts_code: str
    name: str = ""
    open_price: float = 0.0
    pre_close: float = 0.0
    last_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    bid1: float = 0.0
    ask1: float = 0.0
    vol_hand: float = 0.0
    amount_yuan: float = 0.0
    change_pct: Optional[float] = None
    date: str = ""
    time: str = ""
    source: str = ""
    received_at: str = field(default_factory=now_iso)
    stale_seconds: Optional[float] = None
    is_stale: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], *, stale_after_seconds: float = 90.0) -> "QuoteSnapshot":
        raw = dict(raw or {})
        code = normalize_stock_code(pick(raw, ("code", "ts_code", "symbol", "股票代码")), add_suffix=False)
        ts_code = normalize_stock_code(code, add_suffix=True) if code else str(raw.get("ts_code") or "")
        pre_close = to_float(pick(raw, ("pre_close", "昨收", "close_yesterday", "prev_close")))
        last_price = to_float(pick(raw, ("last_price", "now", "最新价", "price")))
        change_pct_value = pick(raw, ("change_pct", "pct_chg", "涨跌幅", "涨跌(%)"))
        change_pct = None if change_pct_value in (None, "") else to_float(change_pct_value)
        if change_pct is None and last_price > 0 and pre_close > 0:
            change_pct = (last_price - pre_close) / pre_close * 100.0

        date = str(pick(raw, ("date", "trade_date", "日期"), "") or "")
        time = str(pick(raw, ("time", "时间"), "") or "")
        quote_dt = parse_quote_datetime(date, time)
        stale_seconds = None
        if quote_dt is not None:
            stale_seconds = max(0.0, (datetime.now() - quote_dt).total_seconds())

        return cls(
            code=code,
            ts_code=ts_code,
            name=str(pick(raw, ("name", "股票名称", "名称"), "") or ""),
            open_price=to_float(pick(raw, ("open_price", "open", "开盘价", "今开"))),
            pre_close=pre_close,
            last_price=last_price,
            high_price=to_float(pick(raw, ("high_price", "high", "最高价"))),
            low_price=to_float(pick(raw, ("low_price", "low", "最低价"))),
            bid1=to_float(pick(raw, ("bid1", "买一"))),
            ask1=to_float(pick(raw, ("ask1", "卖一"))),
            vol_hand=to_float(pick(raw, ("vol_hand", "volume_hand", "成交量(手)", "vol"))),
            amount_yuan=to_float(pick(raw, ("amount_yuan", "amount", "成交额"))),
            change_pct=change_pct,
            date=date,
            time=time,
            source=str(raw.get("source") or ""),
            stale_seconds=stale_seconds,
            is_stale=bool(stale_seconds is not None and stale_seconds > stale_after_seconds),
            raw=raw,
        )

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        data = {
            "code": self.code,
            "ts_code": self.ts_code,
            "name": self.name,
            "open_price": self.open_price,
            "pre_close": self.pre_close,
            "last_price": self.last_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "bid1": self.bid1,
            "ask1": self.ask1,
            "vol_hand": self.vol_hand,
            "amount_yuan": self.amount_yuan,
            "change_pct": self.change_pct,
            "date": self.date,
            "time": self.time,
            "source": self.source,
            "received_at": self.received_at,
            "stale_seconds": self.stale_seconds,
            "is_stale": self.is_stale,
        }
        if include_raw:
            data["raw"] = self.raw
        return data


@dataclass
class SectorSnapshot:
    code: str
    name: str = ""
    last_price: float = 0.0
    change_pct: Optional[float] = None
    change: float = 0.0
    open_price: float = 0.0
    pre_close: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    volume: float = 0.0
    amount_yuan: float = 0.0
    turnover_rate: float = 0.0
    source: str = ""
    date: str = ""
    time: str = ""
    received_at: str = field(default_factory=now_iso)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], *, source: str = "") -> "SectorSnapshot":
        raw = dict(raw or {})
        code = str(pick(raw, (
            "code", "index_code", "板块代码", "概念代码", "行业代码", "symbol", "ts_code"
        ), "") or "").strip()
        last_price = to_float(pick(raw, (
            "last_price", "price", "now", "close", "最新价", "指数", "收盘价"
        )))
        pre_close = to_float(pick(raw, ("pre_close", "prev_close", "昨收")))
        change_pct_value = pick(raw, ("change_pct", "pct_chg", "涨跌幅", "涨幅"))
        change_pct = None if change_pct_value in (None, "") else to_float(change_pct_value)
        if change_pct is None and last_price > 0 and pre_close > 0:
            change_pct = (last_price - pre_close) / pre_close * 100.0

        return cls(
            code=code,
            name=str(pick(raw, ("name", "index_name", "板块名称", "概念名称", "行业名称", "名称"), "") or ""),
            last_price=last_price,
            change_pct=change_pct,
            change=to_float(pick(raw, ("change", "涨跌额", "涨跌"))),
            open_price=to_float(pick(raw, ("open_price", "open", "开盘价", "今开"))),
            pre_close=pre_close,
            high_price=to_float(pick(raw, ("high_price", "high", "最高价"))),
            low_price=to_float(pick(raw, ("low_price", "low", "最低价"))),
            volume=to_float(pick(raw, ("volume", "vol", "成交量"))),
            amount_yuan=to_float(pick(raw, ("amount_yuan", "amount", "成交额"))),
            turnover_rate=to_float(pick(raw, ("turnover_rate", "换手率"))),
            source=str(raw.get("source") or source),
            date=str(pick(raw, ("date", "trade_date", "日期"), "") or ""),
            time=str(pick(raw, ("time", "trade_time", "时间"), "") or ""),
            raw=raw,
        )

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        data = {
            "code": self.code,
            "name": self.name,
            "last_price": self.last_price,
            "change_pct": self.change_pct,
            "change": self.change,
            "open_price": self.open_price,
            "pre_close": self.pre_close,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "volume": self.volume,
            "amount_yuan": self.amount_yuan,
            "turnover_rate": self.turnover_rate,
            "source": self.source,
            "date": self.date,
            "time": self.time,
            "received_at": self.received_at,
        }
        if include_raw:
            data["raw"] = self.raw
        return data
