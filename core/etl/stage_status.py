"""Local artifact checks for the decoupled post-close pipeline."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable


CORE_SILVER_TABLES = (
    "stock_daily_silver",
    "sector_daily_silver",
    "index_daily_silver",
)

FACTOR_TABLES = (
    "factor_market_wide",
    "factor_sector_wide",
    "factor_stock_wide",
)

POST_CLOSE_SOURCES = (
    "stock_daily",
    "stock_basic",
    "daily_basic",
    "limit_up",
    "limit_down",
    "limit_up_concepts",
    "index_daily",
    "ths_index",
    "ths_daily",
    "limit_cpt_list",
    "moneyflow_summary",
    "top_list",
    "top_inst",
    "hm_detail",
    "moneyflow_ths",
    "moneyflow_dc",
    "sector_moneyflow_ths",
    "ths_hot",
    "dc_hot",
    "kpl_list",
    "margin_detail",
    "block_trade",
)

POST_CLOSE_SILVER_TABLES = (
    "stock_daily_silver",
    "sector_daily_silver",
    "index_daily_silver",
    "limit_up_pool_silver",
    "limit_down_pool_silver",
    "lhb_daily_silver",
    "lhb_institution_silver",
    "lhb_hot_money_silver",
    "stock_capital_flow_silver",
    "sector_capital_flow_silver",
    "stock_attention_silver",
    "stock_leader_signal_silver",
    "stock_margin_silver",
    "stock_event_silver",
)


def _date_count(db_path: Path, table: str, trade_date: str) -> int:
    if not Path(db_path).exists():
        return 0
    try:
        import duckdb

        # DuckDB requires every in-process connection to the same file to use
        # identical configuration. Silver writes are read-write, so status
        # checks must not open a concurrent read_only connection.
        with duckdb.connect(str(db_path)) as con:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
                [str(table)],
            ).fetchone()[0]
            if not exists:
                return 0
            return int(
                con.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE CAST(trade_date AS VARCHAR) = ?',
                    [str(trade_date)],
                ).fetchone()[0]
            )
    except Exception:
        return 0


def table_partition_status(
    trade_date: str,
    *,
    db_path: Path,
    tables: Iterable[str],
) -> Dict[str, int]:
    return {
        str(table): _date_count(Path(db_path), str(table), str(trade_date))
        for table in tables
    }


def fetch_status(trade_date: str, *, db_path: Path, web_data_dir: Path) -> Dict[str, Any]:
    """Return whether the local Silver partition can feed factor jobs."""
    date = str(trade_date)
    quality_path = Path(web_data_dir) / "etl_quality" / f"quality_{date}.json"
    quality: Dict[str, Any] = {}
    if quality_path.exists():
        try:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
        except Exception:
            quality = {}
    manifest_path = Path(web_data_dir) / "fetch_status" / f"fetch_{date}.json"
    manifest: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    counts = table_partition_status(date, db_path=Path(db_path), tables=CORE_SILVER_TABLES)
    missing = [table for table, count in counts.items() if count <= 0]
    ready = bool(quality_path.exists() and quality.get("ok") is True and not missing)
    sources = manifest.get("sources") or {}
    source_missing = [
        source for source in POST_CLOSE_SOURCES
        if not bool((sources.get(source) or {}).get("ok"))
    ] if manifest else []
    source_complete = bool(manifest and not source_missing)
    writes = manifest.get("writes") or {}
    write_missing = [
        table for table in POST_CLOSE_SILVER_TABLES
        if not bool((writes.get(table) or {}).get("duckdb"))
    ] if manifest else []
    write_complete = bool(writes and not write_missing)
    legacy_complete = bool(ready and not manifest_path.exists())
    ready_for_factors = bool(ready and (write_complete or legacy_complete))
    return {
        "stage": "fetch",
        "trade_date": date,
        "ready": ready_for_factors,
        "core_ready": ready,
        "quality_ok": quality.get("ok") is True,
        "quality_path": str(quality_path),
        "table_rows": counts,
        "missing": missing,
        "manifest_path": str(manifest_path),
        "sources": sources,
        "source_missing": source_missing,
        "source_complete": source_complete,
        "writes": writes,
        "write_missing": write_missing,
        "write_complete": write_complete,
        "complete": bool(ready_for_factors and (source_complete or legacy_complete)),
        "legacy_complete": legacy_complete,
        "message": (
            "盘后数据已就绪"
            if ready_for_factors else "盘后数据不完整，请先运行盘后取数"
        ),
    }


def factor_status(trade_date: str, *, db_path: Path) -> Dict[str, Any]:
    """Return whether factor partitions required by screening are present."""
    date = str(trade_date)
    counts = table_partition_status(date, db_path=Path(db_path), tables=FACTOR_TABLES)
    missing = [table for table, count in counts.items() if count <= 0]
    ready = not missing
    return {
        "stage": "factors",
        "trade_date": date,
        "ready": ready,
        "table_rows": counts,
        "missing": missing,
        "message": "因子数据已就绪" if ready else "因子数据不完整，请先运行因子计算",
    }


def write_fetch_manifest(
    trade_date: str,
    *,
    web_data_dir: Path,
    sources: Dict[str, Any],
    writes: Dict[str, Any] | None = None,
) -> Path:
    path = Path(web_data_dir) / "fetch_status" / f"fetch_{trade_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "trade_date": str(trade_date),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "sources": sources or {},
                "writes": writes or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def require_stage(status: Dict[str, Any]) -> None:
    if status.get("ready"):
        return
    missing = ", ".join(status.get("missing") or []) or "未知产物"
    raise RuntimeError(f"{status.get('message')}；缺失：{missing}")


__all__ = [
    "CORE_SILVER_TABLES",
    "FACTOR_TABLES",
    "POST_CLOSE_SOURCES",
    "POST_CLOSE_SILVER_TABLES",
    "factor_status",
    "fetch_status",
    "require_stage",
    "table_partition_status",
    "write_fetch_manifest",
]
