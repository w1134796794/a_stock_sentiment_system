"""Utilities for Phase 2 gold factor jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


GOLD_SCHEMA_VERSION = "phase2_mvp_v1"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return default


def score_between(value: Any, low: float, high: float, *, invert: bool = False) -> float:
    value = to_float(value)
    if high <= low:
        return 50.0
    clamped = max(low, min(high, value))
    score = (clamped - low) / (high - low) * 100.0
    return 100.0 - score if invert else score


def percentile_score(series: pd.Series, *, higher_better: bool = True) -> pd.Series:
    if series is None or series.empty:
        return pd.Series(dtype=float)
    values = pd.to_numeric(series, errors="coerce")
    if values.notna().sum() <= 1:
        return pd.Series([50.0] * len(series), index=series.index)
    ranks = values.rank(pct=True, ascending=higher_better)
    return (ranks * 100.0).fillna(50.0)


def safe_weighted_score(parts: Iterable[tuple[float, float]], default: float = 50.0) -> float:
    total = 0.0
    weight_sum = 0.0
    for score, weight in parts:
        if weight <= 0:
            continue
        total += to_float(score, default) * weight
        weight_sum += weight
    return total / weight_sum if weight_sum > 0 else default


def table_exists(con, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()[0])
    except Exception:
        return False


def read_table(con, table: str, *, where: str = "", params: Optional[List[Any]] = None) -> pd.DataFrame:
    if not table_exists(con, table):
        return pd.DataFrame()
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return con.execute(sql, params or []).fetchdf()


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _duckdb_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE"
    return "VARCHAR"


def write_replace_partition(
    con,
    table: str,
    df: pd.DataFrame,
    *,
    where: str,
    params: List[Any],
) -> int:
    df = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    con.register("_gold_df", df)
    try:
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM _gold_df WHERE 1=0")
    finally:
        con.unregister("_gold_df")

    table_cols = [
        str(row[0])
        for row in con.execute(f"DESCRIBE {table}").fetchall()
    ]
    for col in df.columns:
        if str(col) not in table_cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {_quote_ident(col)} {_duckdb_type(df[col])}")
            table_cols.append(str(col))
    for col in table_cols:
        if col not in df.columns:
            df[col] = None
    df = df[table_cols]

    con.register("_gold_df", df)
    try:
        con.execute(f"DELETE FROM {table} WHERE {where}", params)
        if not df.empty:
            cols = ", ".join(_quote_ident(col) for col in table_cols)
            con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _gold_df")
    finally:
        con.unregister("_gold_df")
    return int(len(df))


def connect_duckdb(path: Path):
    import duckdb  # type: ignore

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


@dataclass
class FactorJobResult:
    name: str
    trade_date: str
    ok: bool = True
    rows: Dict[str, int] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)

    def add_message(self, message: str) -> None:
        self.messages.append(str(message))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "trade_date": self.trade_date,
            "ok": self.ok,
            "rows": self.rows,
            "messages": self.messages,
        }


def make_long_record(
    *,
    trade_date: str,
    entity_type: str,
    entity_id: str,
    factor_id: str,
    raw_value: Any,
    score: Any,
    rank_value: Any = None,
    percentile: Any = None,
    direction: str = "higher_better",
) -> Dict[str, Any]:
    return {
        "trade_date": str(trade_date),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "factor_id": factor_id,
        "raw_value": to_float(raw_value),
        "score": to_float(score),
        "rank_value": None if rank_value is None else to_float(rank_value),
        "percentile": None if percentile is None else to_float(percentile),
        "direction": direction,
        "source_version": GOLD_SCHEMA_VERSION,
        "computed_at": now_iso(),
    }


FACTOR_VALUE_LONG_COLUMNS = [
    "trade_date",
    "entity_type",
    "entity_id",
    "factor_id",
    "raw_value",
    "score",
    "rank_value",
    "percentile",
    "direction",
    "source_version",
    "computed_at",
]


def long_records_to_frame(records: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    for col in FACTOR_VALUE_LONG_COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[FACTOR_VALUE_LONG_COLUMNS].copy()
