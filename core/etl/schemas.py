"""Phase 1 ETL schema constants."""
from __future__ import annotations

STOCK_DAILY_SILVER_COLUMNS = [
    "trade_date",
    "code",
    "ts_code",
    "name",
    "exchange",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol_hand",
    "amount_yuan",
    "source",
    "as_of_date",
    "ingested_at",
]

SECTOR_DAILY_SILVER_COLUMNS = [
    "trade_date",
    "sector_code",
    "sector_name",
    "sector_type",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol_hand",
    "amount_yuan",
    "member_count",
    "source",
    "as_of_date",
    "ingested_at",
]

INDEX_DAILY_SILVER_COLUMNS = [
    "trade_date",
    "index_code",
    "index_name",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "pct_chg",
    "vol_hand",
    "amount_yuan",
    "source",
    "as_of_date",
    "ingested_at",
]

QUALITY_TABLES = [
    "stock_daily_silver",
    "sector_daily_silver",
    "index_daily_silver",
]
