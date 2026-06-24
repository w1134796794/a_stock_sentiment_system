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
    "circ_mv",
    "total_mv",
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

LIMIT_UP_POOL_SILVER_COLUMNS = [
    "trade_date",
    "code",
    "ts_code",
    "name",
    "pct_chg",
    "first_time",
    "last_time",
    "open_times",
    "limit_times",
    "fd_amount",
    "float_mv",
    "total_mv",
    "turnover_ratio",
    "source",
    "as_of_date",
    "ingested_at",
]

LIMIT_DOWN_POOL_SILVER_COLUMNS = LIMIT_UP_POOL_SILVER_COLUMNS.copy()

QUALITY_TABLES = [
    "stock_daily_silver",
    "sector_daily_silver",
    "index_daily_silver",
    "limit_up_pool_silver",
    "limit_down_pool_silver",
]
