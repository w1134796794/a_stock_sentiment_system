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
    standardize_limit_down_pool_frame,
    standardize_limit_up_pool_frame,
    standardize_sector_daily_frame,
    standardize_stock_daily_frame,
)
from core.etl.quality import QualityReport, build_quality_report


SILVER_DATE_PARTITION_COLUMN = {
    "stock_daily_silver": "trade_date",
    "sector_daily_silver": "trade_date",
    "index_daily_silver": "trade_date",
    "limit_up_pool_silver": "trade_date",
    "limit_down_pool_silver": "trade_date",
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
            if mode == "append":
                con.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT * FROM _etl_df WHERE 1=0")
                con.execute(f"INSERT INTO {table_name} SELECT * FROM _etl_df")
            elif mode == "upsert_dates":
                self._upsert_duckdb_date_partitions(con, table_name, df)
            else:
                con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM _etl_df")
            con.unregister("_etl_df")

    def _upsert_duckdb_date_partitions(self, con, table_name: str, df: pd.DataFrame) -> None:
        partition_col = SILVER_DATE_PARTITION_COLUMN.get(table_name, "trade_date")
        con.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT * FROM _etl_df WHERE 1=0")
        table_cols = [str(row[0]) for row in con.execute(f"DESCRIBE {table_name}").fetchall()]
        for col in df.columns:
            if str(col) not in table_cols:
                con.execute(f"ALTER TABLE {table_name} ADD COLUMN {_quote_ident(col)} {_duckdb_type(df[col])}")
                table_cols.append(str(col))
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

        dates = sorted(str(x) for x in df[partition_col].dropna().astype(str).unique().tolist())
        if dates:
            con.execute(f"DELETE FROM {table_name} WHERE {_quote_ident(partition_col)} IN (SELECT DISTINCT {_quote_ident(partition_col)} FROM _etl_df)")
        if not df.empty:
            cols = ", ".join(_quote_ident(col) for col in table_cols)
            con.execute(f"INSERT INTO {table_name} ({cols}) SELECT {cols} FROM _etl_df")

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


def build_silver_frames(ds: MarketDataset) -> Dict[str, pd.DataFrame]:
    return {
        "stock_daily_silver": _normalize_stock_tables(ds),
        "sector_daily_silver": _normalize_sector_tables(ds),
        "index_daily_silver": _normalize_index_tables(ds),
        "limit_up_pool_silver": _normalize_limit_up_tables(ds),
        "limit_down_pool_silver": _normalize_limit_down_tables(ds),
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
