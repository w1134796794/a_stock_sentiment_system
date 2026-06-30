"""Read-only Dragon-Tiger list page data assembled from local DuckDB tables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from config.settings import FACTOR_DB_PATH


_TABLES = (
    "lhb_daily_silver",
    "lhb_institution_silver",
    "lhb_hot_money_silver",
    "factor_lhb_stock_wide",
)


def build_lhb_view(trade_date: str, db_path: Path | str = FACTOR_DB_PATH) -> Dict[str, Any]:
    path = Path(db_path)
    signature = _db_signature(path)
    return _build_lhb_view_cached(str(trade_date), str(path), *signature)


def list_lhb_dates(db_path: Path | str = FACTOR_DB_PATH) -> List[str]:
    path = Path(db_path)
    signature = _db_signature(path)
    return list(_list_lhb_dates_cached(str(path), *signature))


def _db_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return int(stat.st_mtime_ns), int(stat.st_size)
    except OSError:
        return 0, 0


def _table_exists(con: Any, table: str) -> bool:
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0])


def _read_date(con: Any, table: str, trade_date: str) -> pd.DataFrame:
    if not _table_exists(con, table):
        return pd.DataFrame()
    return con.execute(
        f'SELECT * FROM "{table}" WHERE CAST(trade_date AS VARCHAR) = ?',
        [str(trade_date)],
    ).fetchdf()


@lru_cache(maxsize=8)
def _list_lhb_dates_cached(db_path: str, _mtime_ns: int, _size: int) -> tuple[str, ...]:
    path = Path(db_path)
    if not path.exists():
        return ()
    try:
        import duckdb  # type: ignore

        values = set()
        with duckdb.connect(str(path), read_only=True) as con:
            for table in _TABLES:
                if not _table_exists(con, table):
                    continue
                rows = con.execute(
                    f'SELECT DISTINCT CAST(trade_date AS VARCHAR) FROM "{table}" '
                    "WHERE trade_date IS NOT NULL"
                ).fetchall()
                values.update(str(row[0]) for row in rows if row and row[0])
        return tuple(sorted(values, reverse=True))
    except Exception:
        return ()


@lru_cache(maxsize=32)
def _build_lhb_view_cached(
    trade_date: str,
    db_path: str,
    _mtime_ns: int,
    _size: int,
) -> Dict[str, Any]:
    empty = _empty_payload(trade_date)
    path = Path(db_path)
    if not path.exists():
        return empty

    try:
        import duckdb  # type: ignore

        with duckdb.connect(str(path), read_only=True) as con:
            daily = _read_date(con, "lhb_daily_silver", trade_date)
            institution = _read_date(con, "lhb_institution_silver", trade_date)
            hot_money = _read_date(con, "lhb_hot_money_silver", trade_date)
            factors = _read_date(con, "factor_lhb_stock_wide", trade_date)
            stock_daily = _read_date(con, "stock_daily_silver", trade_date)
    except Exception as exc:  # noqa: BLE001
        empty["error"] = str(exc)
        return empty

    daily_rows, reasons = _primary_daily_rows(daily)
    factor_rows = _rows_by_code(factors)
    stock_names = _stock_name_map(stock_daily)
    seats_by_code = _aggregate_seats(institution)
    actor_events: List[Dict[str, Any]] = []
    explicit_events = _merge_hot_money(hot_money, seats_by_code, actor_events)

    codes = set(daily_rows) | set(factor_rows) | set(seats_by_code)
    stocks = []
    for code in codes:
        daily_row = daily_rows.get(code, {})
        factor_row = factor_rows.get(code, {})
        name = _text(factor_row.get("name") or daily_row.get("name") or stock_names.get(code) or code)
        seats = sorted(
            seats_by_code.get(code, []),
            key=lambda row: (abs(_number(row.get("net_buy_yuan"))), _number(row.get("buy_yuan"))),
            reverse=True,
        )
        inst_net = _number(factor_row.get("institution_net_buy_yuan"))
        if not factor_row:
            inst_net = sum(_number(row.get("net_buy_yuan")) for row in seats if row.get("is_institution"))
        buy = _number(factor_row.get("lhb_buy_yuan"), daily_row.get("listed_buy_yuan"))
        sell = _number(factor_row.get("lhb_sell_yuan"), daily_row.get("listed_sell_yuan"))
        net = _number(factor_row.get("lhb_net_buy_yuan"), daily_row.get("net_buy_yuan", buy - sell))
        stock_reasons = reasons.get(code, [])
        stocks.append({
            "code": code,
            "ts_code": _text(factor_row.get("ts_code") or daily_row.get("ts_code")),
            "name": name,
            "close": _number(daily_row.get("close")),
            "pct_chg": _number(daily_row.get("pct_chg")),
            "buy_yuan": buy,
            "sell_yuan": sell,
            "net_buy_yuan": net,
            "institution_net_yuan": inst_net,
            "composite_score": _number(factor_row.get("lhb_composite_score"), 50.0),
            "effective_date": _text(factor_row.get("effective_date")),
            "reasons": stock_reasons,
            "reason_text": "；".join(stock_reasons),
            "seats": seats,
            "search_text": " ".join([
                name, code, " ".join(stock_reasons),
                " ".join(_text(row.get("seat_name")) for row in seats),
                " ".join(_text(row.get("actor_name")) for row in seats),
            ]).lower(),
        })

    stocks.sort(key=lambda row: (
        _number(row.get("composite_score")),
        _number(row.get("net_buy_yuan")),
    ), reverse=True)
    actors = _build_actor_groups(actor_events, daily_rows, factor_rows, stock_names)
    total_net = sum(_number(row.get("net_buy_yuan")) for row in stocks)
    institution_net = sum(_number(row.get("institution_net_yuan")) for row in stocks)
    return {
        "date": trade_date,
        "has_data": bool(stocks),
        "error": "",
        "summary": {
            "stock_count": len(stocks),
            "actor_count": len(actors),
            "positive_count": sum(1 for row in stocks if _number(row.get("net_buy_yuan")) > 0),
            "total_net_yuan": total_net,
            "institution_net_yuan": institution_net,
            "official_actor_events": explicit_events,
        },
        "stocks": stocks,
        "actors": actors,
    }


def _empty_payload(trade_date: str) -> Dict[str, Any]:
    return {
        "date": trade_date,
        "has_data": False,
        "error": "",
        "summary": {
            "stock_count": 0,
            "actor_count": 0,
            "positive_count": 0,
            "total_net_yuan": 0.0,
            "institution_net_yuan": 0.0,
            "official_actor_events": 0,
        },
        "stocks": [],
        "actors": [],
    }


def _primary_daily_rows(frame: pd.DataFrame) -> tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    if frame.empty:
        return {}, {}
    work = frame.copy()
    work["code"] = work["code"].map(_code)
    work["listed_amount_yuan"] = pd.to_numeric(work.get("listed_amount_yuan"), errors="coerce").fillna(0.0)
    primary = work.sort_values("listed_amount_yuan", ascending=False).drop_duplicates("code")
    rows = {str(row["code"]): row for row in primary.to_dict("records")}
    reasons: Dict[str, List[str]] = {}
    for code, group in work.groupby("code"):
        reasons[str(code)] = list(dict.fromkeys(
            _text(value) for value in group.get("reason", pd.Series(dtype=str)).tolist() if _text(value)
        ))
    return rows, reasons


def _rows_by_code(frame: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    if frame.empty or "code" not in frame.columns:
        return {}
    return {
        _code(row.get("code")): row
        for row in frame.drop_duplicates("code").to_dict("records")
        if _code(row.get("code"))
    }


def _stock_name_map(frame: pd.DataFrame) -> Dict[str, str]:
    if frame.empty or "code" not in frame.columns:
        return {}
    return {
        _code(row.get("code")): _text(row.get("name"))
        for row in frame.drop_duplicates("code").to_dict("records")
        if _code(row.get("code"))
    }


def _aggregate_seats(frame: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    if frame.empty:
        return {}
    work = frame.copy()
    for col in ("code", "seat_name", "buy_yuan", "sell_yuan", "net_buy_yuan"):
        if col not in work.columns:
            work[col] = "" if col in {"code", "seat_name"} else 0.0
    work["code"] = work["code"].map(_code)
    work = work.drop_duplicates(["code", "seat_name", "buy_yuan", "sell_yuan"])
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in work.to_dict("records"):
        code = _code(row.get("code"))
        seat = _text(row.get("seat_name")) or "未命名席位"
        key = (code, seat)
        item = grouped.setdefault(key, {
            "code": code,
            "seat_name": seat,
            "is_institution": False,
            "buy_yuan": 0.0,
            "sell_yuan": 0.0,
            "net_buy_yuan": 0.0,
            "actor_name": "",
            "source_label": "营业部明细",
        })
        item["buy_yuan"] += _number(row.get("buy_yuan"))
        item["sell_yuan"] += _number(row.get("sell_yuan"))
        item["net_buy_yuan"] += _number(row.get("net_buy_yuan"), _number(row.get("buy_yuan")) - _number(row.get("sell_yuan")))
        item["is_institution"] = bool(item["is_institution"] or _truthy(row.get("is_institution")) or "机构专用" in seat)

    result: Dict[str, List[Dict[str, Any]]] = {}
    for item in grouped.values():
        item["seat_type_label"] = "机构" if item["is_institution"] else "营业部"
        result.setdefault(item["code"], []).append(item)
    return result


def _merge_hot_money(
    frame: pd.DataFrame,
    seats_by_code: Dict[str, List[Dict[str, Any]]],
    actor_events: List[Dict[str, Any]],
) -> int:
    if frame.empty:
        return 0
    work = frame.copy()
    for col in ("code", "actor_name", "seat_name", "buy_yuan", "sell_yuan", "net_buy_yuan"):
        if col not in work.columns:
            work[col] = "" if col in {"code", "actor_name", "seat_name"} else 0.0
    work["code"] = work["code"].map(_code)
    work = work.drop_duplicates(["code", "actor_name", "seat_name", "buy_yuan", "sell_yuan"])
    explicit_count = 0
    for row in work.to_dict("records"):
        code = _code(row.get("code"))
        seat_name = _text(row.get("seat_name")) or "未披露席位"
        actor_name = _text(row.get("actor_name")) or "未识别游资"
        if actor_name == "机构专用":
            continue
        event = {
            "code": code,
            "seat_name": seat_name,
            "is_institution": False,
            "buy_yuan": _number(row.get("buy_yuan")),
            "sell_yuan": _number(row.get("sell_yuan")),
            "net_buy_yuan": _number(row.get("net_buy_yuan"), _number(row.get("buy_yuan")) - _number(row.get("sell_yuan"))),
            "seat_type_label": "知名游资",
            "source_label": "官方游资明细",
            "actor_name": actor_name,
        }
        key = (code, actor_name, seat_name)
        actor_events[:] = [old for old in actor_events if (old.get("code"), old.get("actor_name"), old.get("seat_name")) != key]
        actor_events.append(event)
        explicit_count += 1

        seats = seats_by_code.setdefault(code, [])
        existing = next((item for item in seats if item.get("seat_name") == seat_name), None)
        if existing:
            existing.update({"actor_name": actor_name, "source_label": "官方游资明细"})
            existing["seat_type_label"] = "知名游资"
        else:
            seats.append(dict(event))
    return explicit_count


def _build_actor_groups(
    events: Iterable[Dict[str, Any]],
    daily_rows: Dict[str, Dict[str, Any]],
    factor_rows: Dict[str, Dict[str, Any]],
    stock_names: Dict[str, str],
) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for event in events:
        actor_name = _text(event.get("actor_name"))
        if not actor_name:
            continue
        actor = grouped.setdefault(actor_name, {
            "name": actor_name,
            "buy_yuan": 0.0,
            "sell_yuan": 0.0,
            "net_buy_yuan": 0.0,
            "sources": set(),
            "stock_map": {},
        })
        actor["buy_yuan"] += _number(event.get("buy_yuan"))
        actor["sell_yuan"] += _number(event.get("sell_yuan"))
        actor["net_buy_yuan"] += _number(event.get("net_buy_yuan"))
        actor["sources"].add(_text(event.get("source_label")))
        code = _code(event.get("code"))
        daily = daily_rows.get(code, {})
        factor = factor_rows.get(code, {})
        stock = actor["stock_map"].setdefault(code, {
            "code": code,
            "name": _text(factor.get("name") or daily.get("name") or stock_names.get(code) or code),
            "pct_chg": _number(daily.get("pct_chg")),
            "buy_yuan": 0.0,
            "sell_yuan": 0.0,
            "net_buy_yuan": 0.0,
            "seats": [],
        })
        stock["buy_yuan"] += _number(event.get("buy_yuan"))
        stock["sell_yuan"] += _number(event.get("sell_yuan"))
        stock["net_buy_yuan"] += _number(event.get("net_buy_yuan"))
        stock["seats"].append({
            "seat_name": _text(event.get("seat_name")),
            "net_buy_yuan": _number(event.get("net_buy_yuan")),
            "source_label": _text(event.get("source_label")),
        })

    actors = []
    for actor in grouped.values():
        stocks = list(actor.pop("stock_map").values())
        stocks.sort(key=lambda row: abs(_number(row.get("net_buy_yuan"))), reverse=True)
        actor["stocks"] = stocks
        actor["sources"] = " / ".join(sorted(value for value in actor["sources"] if value))
        actor["search_text"] = " ".join([
            actor["name"],
            " ".join(f"{row['name']} {row['code']}" for row in stocks),
        ]).lower()
        actors.append(actor)
    actors.sort(key=lambda row: abs(_number(row.get("net_buy_yuan"))), reverse=True)
    return actors

def _code(value: Any) -> str:
    text = _text(value).split(".")[0]
    return text.zfill(6) if text else ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _number(value: Any, default: Any = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            raise ValueError
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "y"}


__all__ = ["build_lhb_view", "list_lhb_dates"]
