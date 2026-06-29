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

LHB_DAILY_SILVER_COLUMNS = [
    "trade_date",
    "code",
    "ts_code",
    "name",
    "close",
    "pct_chg",
    "turnover_rate",
    "amount_yuan",
    "listed_sell_yuan",
    "listed_buy_yuan",
    "listed_amount_yuan",
    "net_buy_yuan",
    "net_buy_rate",
    "listed_amount_rate",
    "float_mv_yuan",
    "reason",
    "source",
    "as_of_date",
    "ingested_at",
]

LHB_INSTITUTION_SILVER_COLUMNS = [
    "trade_date",
    "code",
    "ts_code",
    "seat_name",
    "seat_type",
    "is_institution",
    "buy_yuan",
    "sell_yuan",
    "net_buy_yuan",
    "buy_rate",
    "sell_rate",
    "side",
    "reason",
    "source",
    "as_of_date",
    "ingested_at",
]

LHB_HOT_MONEY_SILVER_COLUMNS = [
    "trade_date",
    "code",
    "ts_code",
    "name",
    "actor_name",
    "seat_name",
    "tag",
    "buy_yuan",
    "sell_yuan",
    "net_buy_yuan",
    "source",
    "as_of_date",
    "ingested_at",
]

STOCK_CAPITAL_FLOW_SILVER_COLUMNS = [
    "trade_date", "effective_date", "code", "ts_code", "name", "source",
    "pct_chg", "close", "net_amount_yuan", "net_5d_amount_yuan",
    "large_net_yuan", "large_net_rate", "extra_large_net_yuan",
    "extra_large_net_rate", "medium_net_yuan", "medium_net_rate",
    "small_net_yuan", "small_net_rate", "as_of_date", "ingested_at",
]

SECTOR_CAPITAL_FLOW_SILVER_COLUMNS = [
    "trade_date", "effective_date", "sector_code", "sector_name", "lead_stock",
    "pct_chg", "member_count", "net_buy_yuan", "net_sell_yuan",
    "net_amount_yuan", "source", "as_of_date", "ingested_at",
]

STOCK_ATTENTION_SILVER_COLUMNS = [
    "trade_date", "effective_date", "code", "ts_code", "name", "source",
    "data_type", "rank", "hot", "pct_chg", "current_price", "concept",
    "rank_time", "rank_reason", "as_of_date", "ingested_at",
]

STOCK_LEADER_SIGNAL_SILVER_COLUMNS = [
    "trade_date", "effective_date", "code", "ts_code", "name", "source",
    "lu_time", "open_time", "last_time", "lu_desc", "tag", "theme",
    "status", "bid_amount", "bid_turnover", "lu_bid_vol", "pct_chg",
    "bid_pct_chg", "rt_pct_chg", "limit_order", "as_of_date", "ingested_at",
]

STOCK_MARGIN_SILVER_COLUMNS = [
    "trade_date", "effective_date", "code", "ts_code", "rzye_yuan",
    "rqye_yuan", "rzmre_yuan", "rzche_yuan", "rqyl", "rqchl", "rqmcl",
    "rzrqye_yuan", "source", "as_of_date", "ingested_at",
]

STOCK_EVENT_SILVER_COLUMNS = [
    "trade_date", "effective_date", "code", "ts_code", "event_type", "price",
    "vol", "amount_yuan", "buyer", "seller", "source", "as_of_date", "ingested_at",
]

QUALITY_TABLES = [
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
]
