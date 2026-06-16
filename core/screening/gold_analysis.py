"""Read-only Phase 4 adapter for gold factor tables and screening results."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _records(df, limit: int = 20) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.head(limit).to_dict(orient="records")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def build_gold_analysis_summary(
    trade_date: str,
    *,
    duckdb_path: Optional[Path] = None,
    screening_dir: Optional[Path] = None,
    stock_limit: int = 20,
    sector_limit: int = 10,
) -> Dict[str, Any]:
    """Build a slim analysis payload from precomputed gold tables."""
    from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

    trade_date = str(trade_date)
    duckdb_path = Path(duckdb_path or FACTOR_DB_PATH)
    screening_dir = Path(screening_dir or WEB_DATA_DIR / "screening")
    summary: Dict[str, Any] = {
        "trade_date": trade_date,
        "ok": False,
        "duckdb_path": str(duckdb_path),
        "market": {},
        "top_sectors": [],
        "top_stocks": [],
        "screening": _read_json(screening_dir / f"screening_{trade_date}.json"),
        "messages": [],
    }
    if not duckdb_path.exists():
        summary["messages"].append("factors.duckdb 不存在")
        return summary

    try:
        import duckdb  # type: ignore
    except Exception as e:  # pragma: no cover
        summary["messages"].append(f"duckdb 不可用: {e}")
        return summary

    con = duckdb.connect(str(duckdb_path))
    try:
        market = _table_for_date(con, "factor_market_wide", trade_date)
        sectors = _table_for_date(con, "factor_sector_wide", trade_date)
        stocks = _table_for_date(con, "factor_stock_wide", trade_date)
    finally:
        con.close()

    if not market.empty:
        summary["market"] = market.iloc[0].to_dict()
    if not sectors.empty:
        sectors = sectors.sort_values(["rank", "mainline_score"], ascending=[True, False])
        summary["top_sectors"] = _records(sectors, sector_limit)
    if not stocks.empty:
        stocks = stocks.sort_values(["rank", "total_score"], ascending=[True, False])
        summary["top_stocks"] = _records(stocks, stock_limit)

    summary["ok"] = bool(summary["market"] or summary["top_sectors"] or summary["top_stocks"])
    if not summary["ok"]:
        summary["messages"].append(f"Gold 表无 {trade_date} 数据")
    return summary


def _table_for_date(con, table: str, trade_date: str):
    import pandas as pd

    exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0]
    if not exists:
        return pd.DataFrame()
    return con.execute(f"SELECT * FROM {table} WHERE trade_date = ?", [str(trade_date)]).fetchdf()
