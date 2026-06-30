from __future__ import annotations

import duckdb
import pandas as pd

from web.lhb_view import build_lhb_view, list_lhb_dates


def _write_table(con, name: str, frame: pd.DataFrame) -> None:
    con.register("_frame", frame)
    con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _frame')
    con.unregister("_frame")


def test_lhb_view_groups_stocks_and_known_hot_money(tmp_path):
    db_path = tmp_path / "factors.duckdb"
    daily = pd.DataFrame([
        {
            "trade_date": "20260626", "code": "600001", "ts_code": "600001.SH", "name": "示例科技",
            "close": 12.3, "pct_chg": 10.0, "listed_buy_yuan": 8_000_000.0,
            "listed_sell_yuan": 2_000_000.0, "listed_amount_yuan": 10_000_000.0,
            "net_buy_yuan": 6_000_000.0, "reason": "日涨幅偏离值达到7%",
        },
        {
            "trade_date": "20260626", "code": "000001", "ts_code": "000001.SZ", "name": "示例银行",
            "close": 10.0, "pct_chg": -2.0, "listed_buy_yuan": 1_000_000.0,
            "listed_sell_yuan": 2_000_000.0, "listed_amount_yuan": 3_000_000.0,
            "net_buy_yuan": -1_000_000.0, "reason": "日换手率达到20%",
        },
    ])
    seats = pd.DataFrame([
        {
            "trade_date": "20260626", "code": "600001", "ts_code": "600001.SH",
            "seat_name": "华泰证券股份有限公司上海武定路证券营业部", "is_institution": False,
            "buy_yuan": 5_000_000.0, "sell_yuan": 1_000_000.0, "net_buy_yuan": 4_000_000.0,
        },
        {
            "trade_date": "20260626", "code": "600001", "ts_code": "600001.SH",
            "seat_name": "机构专用", "is_institution": True,
            "buy_yuan": 2_000_000.0, "sell_yuan": 500_000.0, "net_buy_yuan": 1_500_000.0,
        },
        {
            "trade_date": "20260626", "code": "000001", "ts_code": "000001.SZ",
            "seat_name": "普通证券营业部", "is_institution": False,
            "buy_yuan": 200_000.0, "sell_yuan": 800_000.0, "net_buy_yuan": -600_000.0,
        },
    ])
    factors = pd.DataFrame([
        {
            "trade_date": "20260626", "effective_date": "20260629", "code": "600001",
            "ts_code": "600001.SH", "name": "示例科技", "lhb_buy_yuan": 8_000_000.0,
            "lhb_sell_yuan": 2_000_000.0, "lhb_net_buy_yuan": 6_000_000.0,
            "institution_net_buy_yuan": 1_500_000.0, "lhb_composite_score": 82.0,
        },
        {
            "trade_date": "20260626", "effective_date": "20260629", "code": "000001",
            "ts_code": "000001.SZ", "name": "示例银行", "lhb_buy_yuan": 1_000_000.0,
            "lhb_sell_yuan": 2_000_000.0, "lhb_net_buy_yuan": -1_000_000.0,
            "institution_net_buy_yuan": 0.0, "lhb_composite_score": 35.0,
        },
    ])
    hot_money = pd.DataFrame([
        {
            "trade_date": "20260626", "code": "600001", "ts_code": "600001.SH",
            "name": "示例科技", "actor_name": "测试游资",
            "seat_name": "华泰证券股份有限公司上海武定路证券营业部",
            "buy_yuan": 5_000_000.0, "sell_yuan": 1_000_000.0, "net_buy_yuan": 4_000_000.0,
        },
    ])
    stock_daily = pd.DataFrame([
        {"trade_date": "20260626", "code": "600001", "name": "示例科技"},
        {"trade_date": "20260626", "code": "000001", "name": "示例银行"},
    ])

    with duckdb.connect(str(db_path)) as con:
        _write_table(con, "lhb_daily_silver", daily)
        _write_table(con, "lhb_institution_silver", seats)
        _write_table(con, "lhb_hot_money_silver", hot_money)
        _write_table(con, "factor_lhb_stock_wide", factors)
        _write_table(con, "stock_daily_silver", stock_daily)

    result = build_lhb_view("20260626", db_path)

    assert list_lhb_dates(db_path) == ["20260626"]
    assert result["summary"]["stock_count"] == 2
    assert result["summary"]["actor_count"] == 1
    assert result["stocks"][0]["name"] == "示例科技"
    assert result["stocks"][0]["institution_net_yuan"] == 1_500_000.0
    known_seat = next(row for row in result["stocks"][0]["seats"] if row["actor_name"])
    assert known_seat["actor_name"] == "测试游资"
    assert known_seat["source_label"] == "官方游资明细"
    assert result["actors"][0]["name"] == "测试游资"
    assert result["actors"][0]["stocks"][0]["code"] == "600001"
