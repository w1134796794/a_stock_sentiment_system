"""Lightweight normalization helpers for Phase 1 ETL.

These functions deliberately avoid business scoring. They only standardize
codes, dates, units and column names so downstream factor jobs can consume a
stable silver schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Optional

import pandas as pd

from core.etl.schemas import (
    INDEX_DAILY_SILVER_COLUMNS,
    LHB_DAILY_SILVER_COLUMNS,
    LHB_HOT_MONEY_SILVER_COLUMNS,
    LHB_INSTITUTION_SILVER_COLUMNS,
    LIMIT_DOWN_POOL_SILVER_COLUMNS,
    LIMIT_UP_POOL_SILVER_COLUMNS,
    SECTOR_DAILY_SILVER_COLUMNS,
    STOCK_DAILY_SILVER_COLUMNS,
)
from core.utils.stock_code_utils import StockCodeUtils


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pick(row: Any, names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        try:
            value = row.get(name)
        except Exception:
            value = None
        if value is None or value == "":
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        return value
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
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


def infer_exchange(code: Any) -> str:
    try:
        return StockCodeUtils.get_exchange(str(code))
    except Exception:
        return ""


def normalize_trade_date(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y%m%d")
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        text = str(value).strip()
        if not text:
            return default
        if text.isdigit() and len(text) >= 8:
            return text[:8]
        return pd.Timestamp(text).strftime("%Y%m%d")
    except Exception:
        return default


def normalize_time(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        if text.isdigit() and len(text) == 6:
            return f"{text[:2]}:{text[2:4]}:{text[4:6]}"
        if " " in text:
            text = text.split()[-1]
        if len(text) == 5:
            return f"{text}:00"
        return text[:8] if len(text) >= 8 else text
    except Exception:
        return default


def normalize_amount_yuan(value: Any, *, unit: str = "yuan") -> float:
    raw = to_float(value)
    unit_key = (unit or "yuan").lower()
    if unit_key in ("yuan", "元"):
        return raw
    if unit_key in ("thousand_yuan", "qianyuan", "千元"):
        return raw * 1000.0
    if unit_key in ("wan_yuan", "wanyuan", "万元"):
        return raw * 10000.0
    if unit_key in ("yi_yuan", "亿元"):
        return raw * 100000000.0
    return raw


def normalize_pct(value: Any, *, ratio: bool = False) -> float:
    raw = to_float(value)
    return raw * 100.0 if ratio else raw


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    text_columns = {"reason", "tag", "side", "actor_name", "seat_name"}
    for col in columns:
        if col not in df.columns:
            if col == "is_institution":
                df[col] = False
            elif col in text_columns or col.endswith(("code", "name", "date", "source", "at", "exchange", "type")):
                df[col] = ""
            else:
                df[col] = 0.0
    return df[columns].copy()


def _rows(df: Optional[pd.DataFrame]):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    return df.to_dict(orient="records")


def standardize_stock_daily_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "",
    default_code: str = "",
    amount_unit: str = "thousand_yuan",
) -> pd.DataFrame:
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        raw_code = pick(row, ("code", "ts_code", "股票代码", "代码"), default_code)
        code = normalize_stock_code(raw_code, add_suffix=False)
        if not code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        amount = pick(row, ("amount_yuan", "成交额", "amount"), 0)
        rows.append({
            "trade_date": td,
            "code": code,
            "ts_code": normalize_stock_code(code, add_suffix=True),
            "name": str(pick(row, ("name", "股票名称", "名称"), "") or ""),
            "exchange": infer_exchange(code),
            "open": to_float(pick(row, ("open", "开盘价", "今开"))),
            "high": to_float(pick(row, ("high", "最高价"))),
            "low": to_float(pick(row, ("low", "最低价"))),
            "close": to_float(pick(row, ("close", "收盘价", "last_price", "最新价"))),
            "pre_close": to_float(pick(row, ("pre_close", "昨收", "prev_close"))),
            "pct_chg": normalize_pct(pick(row, ("pct_chg", "pct_change", "change_pct", "涨跌幅"), 0)),
            "vol_hand": to_float(pick(row, ("vol_hand", "vol", "成交量(手)", "volume_hand"), 0)),
            "amount_yuan": to_float(amount) if "amount_yuan" in row else normalize_amount_yuan(amount, unit=amount_unit),
            "circ_mv": to_float(pick(row, ("circ_mv", "流通市值"), 0)),
            "total_mv": to_float(pick(row, ("total_mv", "总市值"), 0)),
            "source": str(pick(row, ("source", "数据源"), source) or source),
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), STOCK_DAILY_SILVER_COLUMNS)


def standardize_sector_daily_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "",
    sector_type: str = "",
    amount_unit: str = "yuan",
) -> pd.DataFrame:
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        sector_code = str(pick(row, (
            "sector_code", "ts_code", "index_code", "code", "板块代码", "概念代码", "行业代码"
        ), "") or "").strip()
        if not sector_code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        amount = pick(row, ("amount_yuan", "amount", "成交额"), 0)
        close = to_float(pick(row, ("close", "price", "last_price", "收盘价", "最新价")))
        avg_price = to_float(pick(row, ("avg_price", "均价"), close))
        vol = to_float(pick(row, ("vol_hand", "vol", "volume", "成交量"), 0))
        amount_yuan = to_float(amount) if "amount_yuan" in row else normalize_amount_yuan(amount, unit=amount_unit)
        if amount_yuan <= 0 and vol > 0 and avg_price > 0:
            # ths_daily does not expose amount; use price*volume as a stable cross-sector liquidity proxy.
            amount_yuan = vol * avg_price
        rows.append({
            "trade_date": td,
            "sector_code": sector_code,
            "sector_name": str(pick(row, ("sector_name", "name", "index_name", "板块名称", "名称"), "") or ""),
            "sector_type": str(pick(row, ("sector_type", "type", "板块类型"), sector_type) or sector_type),
            "open": to_float(pick(row, ("open", "开盘价", "今开"))),
            "high": to_float(pick(row, ("high", "最高价"))),
            "low": to_float(pick(row, ("low", "最低价"))),
            "close": close,
            "pre_close": to_float(pick(row, ("pre_close", "昨收", "prev_close"))),
            "pct_chg": normalize_pct(pick(row, ("pct_chg", "pct_change", "change_pct", "涨跌幅"), 0)),
            "vol_hand": vol,
            "amount_yuan": amount_yuan,
            "member_count": to_float(pick(row, ("member_count", "成分股数"), 0)),
            "source": str(pick(row, ("source", "数据源"), source) or source),
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), SECTOR_DAILY_SILVER_COLUMNS)


def standardize_limit_up_pool_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "limit_up_pool",
) -> pd.DataFrame:
    """标准化涨停池（limit_list_d）为银层表。

    上游 ``get_limit_up_pool`` 同时保留英文原始列与中文别名，这里两者兼取，
    输出供个股因子任务消费的连板/封板时间/流通市值等字段。仅覆盖当日涨停的票。
    """
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        raw_code = pick(row, ("code", "ts_code", "代码", "股票代码"))
        code = normalize_stock_code(raw_code, add_suffix=False)
        if not code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        rows.append({
            "trade_date": td,
            "code": code,
            "ts_code": normalize_stock_code(code, add_suffix=True),
            "name": str(pick(row, ("name", "名称", "股票名称"), "") or ""),
            "pct_chg": normalize_pct(pick(row, ("pct_chg", "pct_change", "涨跌幅"), 0)),
            "first_time": normalize_time(pick(row, ("first_time", "首次封板时间"), "")),
            "last_time": normalize_time(pick(row, ("last_time", "最后封板时间"), "")),
            "open_times": to_float(pick(row, ("open_times", "炸板次数"), 0)),
            "limit_times": to_float(pick(row, ("limit_times", "连板数"), 1)),
            "fd_amount": to_float(pick(row, ("fd_amount", "封单金额", "涨停封单额"), 0)),
            "float_mv": to_float(pick(row, ("float_mv", "流通市值"), 0)),
            "total_mv": to_float(pick(row, ("total_mv", "总市值"), 0)),
            "turnover_ratio": to_float(pick(row, ("turnover_ratio", "换手率"), 0)),
            "source": str(pick(row, ("source", "数据源"), source) or source),
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), LIMIT_UP_POOL_SILVER_COLUMNS)


def standardize_limit_down_pool_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "limit_down_pool",
) -> pd.DataFrame:
    """标准化跌停池（limit_list_d）为银层表。

    跌停池与涨停池来自同一个 Tushare 接口，字段结构保持一致，供市场情绪
    和后续复盘严格按官方涨跌停列表取数。
    """
    out = standardize_limit_up_pool_frame(
        df,
        trade_date=trade_date,
        as_of_date=as_of_date,
        source=source,
    )
    return _ensure_columns(out, LIMIT_DOWN_POOL_SILVER_COLUMNS)


def standardize_index_daily_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "",
    default_index_code: str = "",
    amount_unit: str = "thousand_yuan",
) -> pd.DataFrame:
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        index_code = str(pick(row, ("index_code", "ts_code", "code"), default_index_code) or "").strip()
        if not index_code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        amount = pick(row, ("amount_yuan", "amount", "成交额"), 0)
        rows.append({
            "trade_date": td,
            "index_code": index_code,
            "index_name": str(pick(row, ("index_name", "name", "名称"), "") or ""),
            "open": to_float(pick(row, ("open", "开盘价", "今开"))),
            "high": to_float(pick(row, ("high", "最高价"))),
            "low": to_float(pick(row, ("low", "最低价"))),
            "close": to_float(pick(row, ("close", "收盘价", "last_price", "最新价"))),
            "pre_close": to_float(pick(row, ("pre_close", "昨收", "prev_close"))),
            "pct_chg": normalize_pct(pick(row, ("pct_chg", "change_pct", "涨跌幅"), 0)),
            "vol_hand": to_float(pick(row, ("vol_hand", "vol", "volume"), 0)),
            "amount_yuan": to_float(amount) if "amount_yuan" in row else normalize_amount_yuan(amount, unit=amount_unit),
            "source": str(pick(row, ("source", "数据源"), source) or source),
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), INDEX_DAILY_SILVER_COLUMNS)


def standardize_lhb_daily_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "top_list",
) -> pd.DataFrame:
    """Normalize the daily Dragon-Tiger list summary; Tushare amounts are yuan."""
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        code = normalize_stock_code(pick(row, ("code", "ts_code", "股票代码", "代码")), add_suffix=False)
        if not code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        rows.append({
            "trade_date": td,
            "code": code,
            "ts_code": normalize_stock_code(code, add_suffix=True),
            "name": str(pick(row, ("name", "ts_name", "股票名称", "名称"), "") or ""),
            "close": to_float(pick(row, ("close", "收盘价"), 0)),
            "pct_chg": normalize_pct(pick(row, ("pct_chg", "pct_change", "涨跌幅"), 0)),
            "turnover_rate": to_float(pick(row, ("turnover_rate", "换手率"), 0)),
            "amount_yuan": normalize_amount_yuan(pick(row, ("amount_yuan", "amount", "成交额"), 0)),
            "listed_sell_yuan": normalize_amount_yuan(pick(row, ("listed_sell_yuan", "l_sell", "龙虎榜卖出额"), 0)),
            "listed_buy_yuan": normalize_amount_yuan(pick(row, ("listed_buy_yuan", "l_buy", "龙虎榜买入额"), 0)),
            "listed_amount_yuan": normalize_amount_yuan(pick(row, ("listed_amount_yuan", "l_amount", "龙虎榜成交额"), 0)),
            "net_buy_yuan": normalize_amount_yuan(pick(row, ("net_buy_yuan", "net_amount", "龙虎榜净买入"), 0)),
            "net_buy_rate": to_float(pick(row, ("net_buy_rate", "net_rate", "净买入占比"), 0)),
            "listed_amount_rate": to_float(pick(row, ("listed_amount_rate", "amount_rate", "龙虎榜成交占比"), 0)),
            "float_mv_yuan": normalize_amount_yuan(pick(row, ("float_mv_yuan", "float_values", "流通市值"), 0)),
            "reason": str(pick(row, ("reason", "上榜原因"), "") or ""),
            "source": source,
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), LHB_DAILY_SILVER_COLUMNS)


def standardize_lhb_institution_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "top_inst",
) -> pd.DataFrame:
    """Normalize LHB seat details and explicitly identify institution-only seats."""
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        code = normalize_stock_code(pick(row, ("code", "ts_code", "股票代码", "代码")), add_suffix=False)
        if not code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        seat_name = str(pick(row, ("seat_name", "exalter", "营业部名称", "机构名称"), "") or "").strip()
        is_institution = "机构专用" in seat_name or seat_name in {"机构", "机构席位"}
        rows.append({
            "trade_date": td,
            "code": code,
            "ts_code": normalize_stock_code(code, add_suffix=True),
            "seat_name": seat_name,
            "seat_type": "institution" if is_institution else "broker_seat",
            "is_institution": bool(is_institution),
            "buy_yuan": normalize_amount_yuan(pick(row, ("buy_yuan", "buy", "买入额"), 0)),
            "sell_yuan": normalize_amount_yuan(pick(row, ("sell_yuan", "sell", "卖出额"), 0)),
            "net_buy_yuan": normalize_amount_yuan(pick(row, ("net_buy_yuan", "net_buy", "净买入额"), 0)),
            "buy_rate": to_float(pick(row, ("buy_rate", "买入占比"), 0)),
            "sell_rate": to_float(pick(row, ("sell_rate", "卖出占比"), 0)),
            "side": str(pick(row, ("side", "方向"), "") or ""),
            "reason": str(pick(row, ("reason", "上榜原因"), "") or ""),
            "source": source,
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), LHB_INSTITUTION_SILVER_COLUMNS)


def standardize_lhb_hot_money_frame(
    df: Optional[pd.DataFrame],
    *,
    trade_date: str = "",
    as_of_date: str = "",
    source: str = "hm_detail",
) -> pd.DataFrame:
    """Normalize named hot-money activity and its associated brokerage seat."""
    rows = []
    ingested_at = now_iso()
    for row in _rows(df):
        code = normalize_stock_code(pick(row, ("code", "ts_code", "股票代码", "代码")), add_suffix=False)
        if not code:
            continue
        td = normalize_trade_date(pick(row, ("trade_date", "date", "日期"), trade_date), trade_date)
        rows.append({
            "trade_date": td,
            "code": code,
            "ts_code": normalize_stock_code(code, add_suffix=True),
            "name": str(pick(row, ("name", "ts_name", "股票名称", "名称"), "") or ""),
            "actor_name": str(pick(row, ("actor_name", "hm_name", "游资名称"), "") or "").strip(),
            "seat_name": str(pick(row, ("seat_name", "hm_orgs", "营业部名称"), "") or "").strip(),
            "tag": str(pick(row, ("tag", "标签"), "") or ""),
            "buy_yuan": normalize_amount_yuan(pick(row, ("buy_yuan", "buy_amount", "买入额"), 0)),
            "sell_yuan": normalize_amount_yuan(pick(row, ("sell_yuan", "sell_amount", "卖出额"), 0)),
            "net_buy_yuan": normalize_amount_yuan(pick(row, ("net_buy_yuan", "net_amount", "净买入额"), 0)),
            "source": source,
            "as_of_date": normalize_trade_date(as_of_date or td, td),
            "ingested_at": ingested_at,
        })
    return _ensure_columns(pd.DataFrame(rows), LHB_HOT_MONEY_SILVER_COLUMNS)
