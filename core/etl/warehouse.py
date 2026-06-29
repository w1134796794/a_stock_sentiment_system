"""Silver storage for Phase 1 ETL outputs."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.data.market_dataset import MarketDataset
from core.etl.normalizers import (
    normalize_stock_code,
    standardize_index_daily_frame,
    standardize_lhb_daily_frame,
    standardize_lhb_hot_money_frame,
    standardize_lhb_institution_frame,
    standardize_limit_down_pool_frame,
    standardize_limit_up_pool_frame,
    standardize_sector_daily_frame,
    standardize_stock_daily_frame,
    standardize_sector_capital_flow_frame,
    standardize_stock_attention_frame,
    standardize_stock_capital_flow_frame,
    standardize_stock_event_frame,
    standardize_stock_leader_signal_frame,
    standardize_stock_margin_frame,
)
from core.etl.quality import QualityReport, build_quality_report


SILVER_DATE_PARTITION_COLUMN = {
    "stock_daily_silver": "trade_date",
    "sector_daily_silver": "trade_date",
    "index_daily_silver": "trade_date",
    "limit_up_pool_silver": "trade_date",
    "limit_down_pool_silver": "trade_date",
    "lhb_daily_silver": "trade_date",
    "lhb_institution_silver": "trade_date",
    "lhb_hot_money_silver": "trade_date",
    "stock_capital_flow_silver": "trade_date",
    "sector_capital_flow_silver": "trade_date",
    "stock_attention_silver": "trade_date",
    "stock_leader_signal_silver": "trade_date",
    "stock_margin_silver": "trade_date",
    "stock_event_silver": "trade_date",
}

SILVER_CANONICAL_VARCHAR_COLUMNS = {
    "trade_date",
    "code",
    "ts_code",
    "name",
    "exchange",
    "sector_code",
    "sector_name",
    "sector_type",
    "index_code",
    "index_name",
    "seat_name",
    "seat_type",
    "actor_name",
    "reason",
    "tag",
    "side",
    "first_time",
    "last_time",
    "source",
    "as_of_date",
    "ingested_at",
    "effective_date",
    "data_type",
    "concept",
    "rank_time",
    "rank_reason",
    "lu_time",
    "open_time",
    "lu_desc",
    "theme",
    "status",
    "lead_stock",
    "event_type",
    "buyer",
    "seller",
}


class SilverWarehouse:
    """Write normalized silver tables to DuckDB when available, else parquet/csv."""

    def __init__(self, *, duckdb_path: Optional[Path] = None, silver_dir: Optional[Path] = None):
        self.duckdb_path = Path(duckdb_path) if duckdb_path else None
        self.silver_dir = Path(silver_dir) if silver_dir else None

    def write_table(self, table_name: str, df: pd.DataFrame, *, mode: str = "replace") -> Dict[str, Any]:
        df = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        result = {
            "table": table_name,
            "rows": int(len(df)),
            "duckdb": False,
            "file": "",
            "mode": mode,
            "partition_dates": [],
        }
        if "trade_date" in df.columns and not df.empty:
            result["partition_dates"] = sorted(str(x) for x in df["trade_date"].dropna().astype(str).unique().tolist())

        if self.duckdb_path is not None:
            result["duckdb"] = self._write_duckdb_with_retry(table_name, df, mode=mode)

        if self.silver_dir is not None:
            self.silver_dir.mkdir(parents=True, exist_ok=True)
            base = self.silver_dir / table_name
            try:
                path = base.with_suffix(".parquet")
                self._write_parquet_file(df, path)
                result["file"] = str(path)
            except Exception as e:  # noqa: BLE001
                path = base.with_suffix(".csv")
                df.to_csv(path, index=False, encoding="utf-8-sig")
                result["file"] = str(path)
                logger.debug(f"[SilverWarehouse] parquet 写入失败，已降级 CSV {table_name}: {e}")

        return result

    def _write_duckdb_with_retry(self, table_name: str, df: pd.DataFrame, *, mode: str) -> bool:
        if self.duckdb_path is None:
            return False

        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._write_duckdb_table(table_name, df, mode=mode)
                return True
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < 2 and _looks_like_duckdb_lock(e):
                    time.sleep(0.3 * (attempt + 1))
                    continue
                break

        logger.warning(f"[SilverWarehouse] DuckDB 写入失败，降级文件落盘 {table_name}: {last_error}")
        return False

    def _write_duckdb_table(self, table_name: str, df: pd.DataFrame, *, mode: str) -> None:
        if self.duckdb_path is None:
            return
        import duckdb  # type: ignore

        with duckdb.connect(str(self.duckdb_path)) as con:
            con.register("_etl_df", df)
            try:
                con.execute("BEGIN TRANSACTION")
                if mode == "append":
                    con.execute(
                        f"CREATE TABLE IF NOT EXISTS {_quote_ident(table_name)} "
                        "AS SELECT * FROM _etl_df WHERE 1=0"
                    )
                    con.execute(f"INSERT INTO {_quote_ident(table_name)} SELECT * FROM _etl_df")
                elif mode == "upsert_dates":
                    self._upsert_duckdb_date_partitions(con, table_name, df)
                else:
                    con.execute(
                        f"CREATE OR REPLACE TABLE {_quote_ident(table_name)} AS SELECT * FROM _etl_df"
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            finally:
                try:
                    con.unregister("_etl_df")
                except Exception:
                    pass

    def _upsert_duckdb_date_partitions(self, con, table_name: str, df: pd.DataFrame) -> None:
        partition_col = SILVER_DATE_PARTITION_COLUMN.get(table_name, "trade_date")
        quoted_table = _quote_ident(table_name)
        con.execute(f"CREATE TABLE IF NOT EXISTS {quoted_table} AS SELECT * FROM _etl_df WHERE 1=0")
        table_schema = _duckdb_table_schema(con, table_name)
        table_cols = list(table_schema)
        for col in df.columns:
            if str(col) not in table_cols:
                column_type = _duckdb_type(df[col])
                con.execute(f"ALTER TABLE {quoted_table} ADD COLUMN {_quote_ident(col)} {column_type}")
                table_cols.append(str(col))
                table_schema[str(col)] = column_type

        for col in table_cols:
            column_type = table_schema.get(col, "")
            if col in SILVER_CANONICAL_VARCHAR_COLUMNS and not _is_varchar_type(column_type):
                quoted_col = _quote_ident(col)
                con.execute(
                    f"ALTER TABLE {quoted_table} ALTER COLUMN {quoted_col} "
                    f"SET DATA TYPE VARCHAR USING CAST({quoted_col} AS VARCHAR)"
                )
                table_schema[col] = "VARCHAR"
                logger.info(
                    f"[SilverWarehouse] 迁移字段类型 {table_name}.{col}: {column_type} -> VARCHAR"
                )

        for col in table_cols:
            if col not in df.columns:
                df[col] = None
        df = df[table_cols]

        con.unregister("_etl_df")
        con.register("_etl_df", df)
        if partition_col not in df.columns:
            if not df.empty:
                cols = ", ".join(_quote_ident(col) for col in table_cols)
                con.execute(f"INSERT INTO {table_name} ({cols}) SELECT {cols} FROM _etl_df")
            return

        table_schema = _duckdb_table_schema(con, table_name)
        dates = sorted(str(x) for x in df[partition_col].dropna().astype(str).unique().tolist())
        if dates:
            quoted_partition = _quote_ident(partition_col)
            con.execute(
                f"DELETE FROM {quoted_table} "
                f"WHERE CAST({quoted_partition} AS VARCHAR) IN "
                f"(SELECT DISTINCT CAST({quoted_partition} AS VARCHAR) FROM _etl_df)"
            )
        if not df.empty:
            cols = ", ".join(_quote_ident(col) for col in table_cols)
            select_cols = ", ".join(
                f"CAST({_quote_ident(col)} AS {table_schema[col]}) AS {_quote_ident(col)}"
                for col in table_cols
            )
            con.execute(f"INSERT INTO {quoted_table} ({cols}) SELECT {select_cols} FROM _etl_df")

    def _write_parquet_file(self, df: pd.DataFrame, path: Path) -> None:
        """Write parquet without pandas.to_parquet to avoid pyarrow extension re-registration in long-lived apps."""
        try:
            import duckdb  # type: ignore
        except ImportError:
            df.to_parquet(path, index=False)
            return

        tmp_path = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}")
        try:
            with duckdb.connect(":memory:") as con:
                con.register("_etl_df", df)
                con.execute("COPY (SELECT * FROM _etl_df) TO ? (FORMAT PARQUET)", [str(tmp_path)])
                con.unregister("_etl_df")
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def _looks_like_duckdb_lock(error: Exception) -> bool:
    text = str(error).lower()
    return any(token in text for token in ("cannot open file", "正在使用", "being used", "locked", "lock"))


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


def _duckdb_table_schema(con, table_name: str) -> Dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in con.execute(f"DESCRIBE {_quote_ident(table_name)}").fetchall()
    }


def _is_varchar_type(column_type: str) -> bool:
    normalized = str(column_type or "").upper()
    return normalized.startswith(("VARCHAR", "CHAR", "TEXT"))


def _normalize_stock_tables(ds: MarketDataset) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    name_map = _stock_name_map(ds)

    for date, df in (ds.all_daily or {}).items():
        frames.append(
            standardize_stock_daily_frame(
                df,
                trade_date=str(date),
                as_of_date=ds.trade_date,
                source="all_daily",
                amount_unit="thousand_yuan",
            )
        )

    for code, df in (ds.daily or {}).items():
        frames.append(
            standardize_stock_daily_frame(
                df,
                as_of_date=ds.trade_date,
                source="stock_daily_window",
                default_code=code,
                amount_unit="thousand_yuan",
            )
        )

    if not frames:
        return standardize_stock_daily_frame(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    if not out.empty:
        out = out.drop_duplicates(["trade_date", "code"], keep="first")
        if name_map and "name" in out.columns:
            empty_name = out["name"].fillna("").astype(str).str.strip() == ""
            out.loc[empty_name, "name"] = out.loc[empty_name, "code"].map(name_map).fillna("")
        out = _merge_daily_basic_market_cap(ds, out)
    return out


def _merge_daily_basic_market_cap(ds: MarketDataset, out: pd.DataFrame) -> pd.DataFrame:
    """把 daily_basic 的流通/总市值（万元）按 (trade_date, code) 合并进个股银表。

    daily_basic 仅按交易日预取（通常只有当日），历史日无市值时保持 0。
    """
    mv_frames: List[pd.DataFrame] = []
    for date, df in (ds.daily_basic or {}).items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        cols = {c.lower(): c for c in df.columns}
        code_col = cols.get("ts_code") or cols.get("code")
        if not code_col:
            continue
        sub = pd.DataFrame({
            "trade_date": str(date),
            "code": df[code_col].map(lambda v: normalize_stock_code(v, add_suffix=False)),
            "circ_mv": pd.to_numeric(df.get(cols.get("circ_mv")), errors="coerce") if cols.get("circ_mv") else 0.0,
            "total_mv": pd.to_numeric(df.get(cols.get("total_mv")), errors="coerce") if cols.get("total_mv") else 0.0,
        })
        mv_frames.append(sub)
    if not mv_frames:
        return out
    mv = pd.concat(mv_frames, ignore_index=True)
    mv = mv[mv["code"].astype(str).str.len() > 0].drop_duplicates(["trade_date", "code"], keep="last")
    merged = out.merge(mv, on=["trade_date", "code"], how="left", suffixes=("", "_db"))
    for col in ("circ_mv", "total_mv"):
        db_col = f"{col}_db"
        if db_col in merged.columns:
            merged[col] = pd.to_numeric(merged[db_col], errors="coerce").fillna(
                pd.to_numeric(merged.get(col), errors="coerce")
            ).fillna(0.0)
            merged = merged.drop(columns=[db_col])
    return merged


def _stock_name_map(ds: MarketDataset) -> Dict[str, str]:
    mapping: Dict[str, str] = {}

    def add(code: Any, name: Any) -> None:
        text = str(code or "").strip()
        if "." in text:
            text = text.split(".")[0]
        digits = "".join(ch for ch in text if ch.isdigit())
        code6 = digits[-6:] if len(digits) >= 6 else digits
        label = str(name or "").strip()
        if code6 and label and not mapping.get(code6):
            mapping[code6] = label

    stock_basic = ds.calls.get("stock_basic")
    if isinstance(stock_basic, pd.DataFrame) and not stock_basic.empty:
        for row in stock_basic.to_dict(orient="records"):
            add(row.get("ts_code") or row.get("symbol") or row.get("code"), row.get("name") or row.get("股票名称"))

    for frame in list((ds.limit_up or {}).values()) + list((ds.limit_down or {}).values()):
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            for row in frame.to_dict(orient="records"):
                add(row.get("代码") or row.get("ts_code") or row.get("code"), row.get("名称") or row.get("name"))

    return mapping


def _normalize_sector_tables(ds: MarketDataset) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    sector_map = _sector_index_map(ds)
    for key, value in (ds.calls or {}).items():
        if not str(key).startswith("ths_daily|"):
            continue
        if not isinstance(value, pd.DataFrame):
            continue
        frame = standardize_sector_daily_frame(
            value,
            as_of_date=ds.trade_date,
            source="ths_daily",
            amount_unit="yuan",
        )
        if sector_map and not frame.empty:
            empty_name = frame["sector_name"].fillna("").astype(str).str.strip() == ""
            frame.loc[empty_name, "sector_name"] = (
                frame.loc[empty_name, "sector_code"].map(lambda c: sector_map.get(str(c), {}).get("name", ""))
            )
            empty_type = frame["sector_type"].fillna("").astype(str).str.strip() == ""
            frame.loc[empty_type, "sector_type"] = (
                frame.loc[empty_type, "sector_code"].map(lambda c: sector_map.get(str(c), {}).get("type", ""))
            )
        frames.append(frame)
    if not frames:
        return standardize_sector_daily_frame(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    if not out.empty:
        out = out.drop_duplicates(["trade_date", "sector_code"], keep="first")
    return out


def _sector_index_map(ds: MarketDataset) -> Dict[str, Dict[str, str]]:
    mapping: Dict[str, Dict[str, str]] = {}

    def add(code: Any, name: Any, typ: Any) -> None:
        code_text = str(code or "").strip()
        if not code_text:
            return
        label = str(name or "").strip()
        type_label = _sector_type_label(typ)
        keys = [code_text]
        if "." not in code_text and code_text.isdigit():
            keys.append(f"{code_text}.TI")
        for key in keys:
            current = mapping.setdefault(key, {"name": "", "type": ""})
            if label and not current.get("name"):
                current["name"] = label
            if type_label and not current.get("type"):
                current["type"] = type_label

    for key, value in (ds.calls or {}).items():
        if not str(key).startswith("ths_index|"):
            continue
        if not isinstance(value, pd.DataFrame) or value.empty:
            continue
        for row in value.to_dict(orient="records"):
            code = str(
                row.get("sector_code")
                or row.get("ts_code")
                or row.get("index_code")
                or row.get("code")
                or ""
            ).strip()
            if not code:
                continue
            add(
                code,
                row.get("sector_name") or row.get("name") or row.get("index_name") or "",
                row.get("sector_type") or row.get("type") or "",
            )
    for row in _optional_adata_concepts():
        add(
            row.get("index_code") or row.get("sector_code") or row.get("code") or "",
            row.get("name") or row.get("sector_name") or "",
            row.get("sector_type") or "概念",
        )
    return mapping


def _optional_adata_concepts() -> List[Dict[str, Any]]:
    try:
        import adata  # type: ignore

        method = getattr(getattr(getattr(adata, "stock", None), "info", None), "all_concept_code_ths", None)
        if method is None:
            return []
        raw = method()
        if hasattr(raw, "to_dict"):
            return list(raw.to_dict(orient="records"))
        if isinstance(raw, list):
            return [dict(x) for x in raw if isinstance(x, dict)]
    except Exception:
        return []
    return []


def _sector_type_label(value: Any) -> str:
    text = str(value or "").strip()
    return {
        "N": "概念",
        "I": "行业",
        "R": "地域",
        "S": "特色",
    }.get(text, text)


def _parse_call_param(key: str, param: str) -> str:
    for part in str(key).split("|"):
        prefix = f"{param}="
        if part.startswith(prefix):
            return part[len(prefix):]
    return ""


def _normalize_index_tables(ds: MarketDataset) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for key, value in (ds.calls or {}).items():
        if not str(key).startswith("index_daily|"):
            continue
        if not isinstance(value, pd.DataFrame):
            continue
        code = _parse_call_param(str(key), "ts_code")
        frames.append(
            standardize_index_daily_frame(
                value,
                as_of_date=ds.trade_date,
                source="index_daily",
                default_index_code=code,
                amount_unit="thousand_yuan",
            )
        )
    if not frames:
        return standardize_index_daily_frame(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    if not out.empty:
        out = out.drop_duplicates(["trade_date", "index_code"], keep="first")
    return out


def _normalize_limit_up_tables(ds: MarketDataset) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for date, df in (ds.limit_up or {}).items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        frames.append(
            standardize_limit_up_pool_frame(
                df,
                trade_date=str(date),
                as_of_date=ds.trade_date,
                source="limit_up_pool",
            )
        )
    if not frames:
        return standardize_limit_up_pool_frame(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    if not out.empty:
        out = out.drop_duplicates(["trade_date", "code"], keep="first")
    return out


def _normalize_limit_down_tables(ds: MarketDataset) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for date, df in (ds.limit_down or {}).items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        frames.append(
            standardize_limit_down_pool_frame(
                df,
                trade_date=str(date),
                as_of_date=ds.trade_date,
                source="limit_down_pool",
            )
        )
    if not frames:
        return standardize_limit_down_pool_frame(pd.DataFrame())
    out = pd.concat(frames, ignore_index=True)
    if not out.empty:
        out = out.drop_duplicates(["trade_date", "code"], keep="first")
    return out


def _call_frame(ds: MarketDataset, prefix: str) -> pd.DataFrame:
    for key, value in (ds.calls or {}).items():
        if str(key).startswith(prefix) and isinstance(value, pd.DataFrame):
            return value
    return pd.DataFrame()


def _effective_date(trade_date: str) -> str:
    try:
        from core.utils.date_utils import get_next_trade_date

        return str(get_next_trade_date(str(trade_date)))
    except Exception:
        return ""


def _normalize_stock_capital_flow(ds: MarketDataset) -> pd.DataFrame:
    effective = _effective_date(ds.trade_date)
    frames = [
        standardize_stock_capital_flow_frame(
            _call_frame(ds, "moneyflow_ths|"), trade_date=ds.trade_date,
            effective_date=effective, source="ths",
        ),
        standardize_stock_capital_flow_frame(
            _call_frame(ds, "moneyflow_dc|"), trade_date=ds.trade_date,
            effective_date=effective, source="dc",
        ),
    ]
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        ["trade_date", "code", "source"], keep="last",
    )


def _normalize_sector_capital_flow(ds: MarketDataset) -> pd.DataFrame:
    return standardize_sector_capital_flow_frame(
        _call_frame(ds, "sector_moneyflow_ths|"), trade_date=ds.trade_date,
        effective_date=_effective_date(ds.trade_date), source="ths",
    )


def _normalize_stock_attention(ds: MarketDataset) -> pd.DataFrame:
    effective = _effective_date(ds.trade_date)
    frames = [
        standardize_stock_attention_frame(
            _call_frame(ds, "ths_hot|"), trade_date=ds.trade_date,
            effective_date=effective, source="ths",
        ),
        standardize_stock_attention_frame(
            _call_frame(ds, "dc_hot|"), trade_date=ds.trade_date,
            effective_date=effective, source="dc",
        ),
    ]
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        ["trade_date", "code", "source"], keep="last",
    )


def _normalize_stock_leader_signal(ds: MarketDataset) -> pd.DataFrame:
    return standardize_stock_leader_signal_frame(
        _call_frame(ds, "kpl_list|"), trade_date=ds.trade_date,
        effective_date=_effective_date(ds.trade_date), source="kpl",
    )


def _normalize_stock_margin(ds: MarketDataset) -> pd.DataFrame:
    return standardize_stock_margin_frame(
        _call_frame(ds, "margin_detail|"), trade_date=ds.trade_date,
        effective_date=_effective_date(ds.trade_date), source="margin_detail",
    )


def _normalize_stock_event(ds: MarketDataset) -> pd.DataFrame:
    return standardize_stock_event_frame(
        _call_frame(ds, "block_trade|"), trade_date=ds.trade_date,
        effective_date=_effective_date(ds.trade_date), source="block_trade",
    )


def _normalize_lhb_daily(ds: MarketDataset) -> pd.DataFrame:
    return standardize_lhb_daily_frame(
        _call_frame(ds, "top_list|"), trade_date=ds.trade_date,
        as_of_date=ds.trade_date, source="top_list",
    )


def _normalize_lhb_institution(ds: MarketDataset) -> pd.DataFrame:
    return standardize_lhb_institution_frame(
        _call_frame(ds, "top_inst|"), trade_date=ds.trade_date,
        as_of_date=ds.trade_date, source="top_inst",
    )


def _normalize_lhb_hot_money(ds: MarketDataset) -> pd.DataFrame:
    return standardize_lhb_hot_money_frame(
        _call_frame(ds, "hm_detail|"), trade_date=ds.trade_date,
        as_of_date=ds.trade_date, source="hm_detail",
    )


def build_silver_frames(ds: MarketDataset) -> Dict[str, pd.DataFrame]:
    return {
        "stock_daily_silver": _normalize_stock_tables(ds),
        "sector_daily_silver": _normalize_sector_tables(ds),
        "index_daily_silver": _normalize_index_tables(ds),
        "limit_up_pool_silver": _normalize_limit_up_tables(ds),
        "limit_down_pool_silver": _normalize_limit_down_tables(ds),
        "lhb_daily_silver": _normalize_lhb_daily(ds),
        "lhb_institution_silver": _normalize_lhb_institution(ds),
        "lhb_hot_money_silver": _normalize_lhb_hot_money(ds),
        "stock_capital_flow_silver": _normalize_stock_capital_flow(ds),
        "sector_capital_flow_silver": _normalize_sector_capital_flow(ds),
        "stock_attention_silver": _normalize_stock_attention(ds),
        "stock_leader_signal_silver": _normalize_stock_leader_signal(ds),
        "stock_margin_silver": _normalize_stock_margin(ds),
        "stock_event_silver": _normalize_stock_event(ds),
    }


def persist_market_dataset_silver(
    ds: MarketDataset,
    *,
    duckdb_path: Optional[Path] = None,
    silver_dir: Optional[Path] = None,
    quality_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Normalize a MarketDataset, write silver tables and emit quality reports."""
    if duckdb_path is None or silver_dir is None or quality_dir is None:
        from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

        duckdb_path = duckdb_path or FACTOR_DB_PATH
        silver_dir = silver_dir or WEB_DATA_DIR / "warehouse" / "silver"
        quality_dir = quality_dir or WEB_DATA_DIR / "etl_quality"

    frames = build_silver_frames(ds)
    warehouse = SilverWarehouse(duckdb_path=duckdb_path, silver_dir=silver_dir)
    writes = {
        name: warehouse.write_table(name, frame, mode="upsert_dates")
        for name, frame in frames.items()
    }

    report: QualityReport = build_quality_report(ds.trade_date, frames)
    qdir = Path(quality_dir)
    report.write_json(qdir / f"quality_{ds.trade_date}.json")
    report.write_markdown(qdir / f"quality_{ds.trade_date}.md")

    summary = {
        "trade_date": ds.trade_date,
        "writes": writes,
        "quality_ok": report.ok,
        "issue_count": len(report.issues),
        "quality_json": str(qdir / f"quality_{ds.trade_date}.json"),
        "quality_md": str(qdir / f"quality_{ds.trade_date}.md"),
    }
    ds.meta["silver_persist"] = summary
    logger.info(
        f"[SilverWarehouse] Phase1 silver 落盘完成 {ds.trade_date}: "
        f"quality_ok={report.ok} issues={len(report.issues)}"
    )
    return summary
