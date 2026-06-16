import json

import pandas as pd

from core.data.market_dataset import MarketDataset, call_key
from core.etl.normalizers import (
    normalize_amount_yuan,
    normalize_stock_code,
    normalize_trade_date,
    standardize_sector_daily_frame,
    standardize_stock_daily_frame,
)
from core.etl.warehouse import persist_market_dataset_silver


def test_etl_normalizes_stock_daily_schema_and_units():
    df = pd.DataFrame([{
        "ts_code": "000001.SZ",
        "trade_date": pd.Timestamp("2026-06-16"),
        "name": "平安银行",
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "pre_close": 9.9,
        "pct_chg": 3.03,
        "vol": 1000,
        "amount": 123.4,
    }])

    out = standardize_stock_daily_frame(df, as_of_date="20260616", source="unit_test")

    assert list(out["code"]) == ["000001"]
    assert list(out["ts_code"]) == ["000001.SZ"]
    assert list(out["trade_date"]) == ["20260616"]
    assert out.iloc[0]["amount_yuan"] == 123400.0
    assert out.iloc[0]["source"] == "unit_test"


def test_etl_normalizes_common_values():
    assert normalize_stock_code("sz000001") == "000001"
    assert normalize_stock_code("600000", add_suffix=True) == "600000.SH"
    assert normalize_trade_date("2026-06-16") == "20260616"
    assert normalize_amount_yuan(1.5, unit="万元") == 15000.0


def test_etl_normalizes_sector_daily_schema():
    df = pd.DataFrame([{
        "index_code": "886109",
        "name": "AI应用",
        "trade_date": "20260616",
        "open": 100.0,
        "high": 110.0,
        "low": 99.0,
        "price": 108.0,
        "amount": 1000000,
    }])

    out = standardize_sector_daily_frame(df, source="adata_ths")

    assert out.iloc[0]["sector_code"] == "886109"
    assert out.iloc[0]["sector_name"] == "AI应用"
    assert out.iloc[0]["close"] == 108.0
    assert out.iloc[0]["amount_yuan"] == 1000000.0


def test_persist_market_dataset_silver_writes_files_and_quality(tmp_path):
    ds = MarketDataset(trade_date="20260616", prev_trade_date="20260615")
    ds.all_daily["20260616"] = pd.DataFrame([{
        "ts_code": "000001.SZ",
        "trade_date": "20260616",
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "pre_close": 9.9,
        "pct_chg": 3.03,
        "vol": 1000,
        "amount": 123.4,
    }])
    ds.put_call(
        call_key("ths_daily", ts_code=None, trade_date="20260616", start_date=None, end_date=None),
        pd.DataFrame([{
            "ts_code": "886109",
            "trade_date": "20260616",
            "open": 100.0,
            "high": 110.0,
            "low": 99.0,
            "close": 108.0,
            "pre_close": 98.0,
            "pct_chg": 10.2,
            "amount": 1000000,
        }]),
        "ths_daily",
    )
    ds.put_call(
        call_key("index_daily", ts_code="000001.SH", start_date="20260101", end_date="20260616"),
        pd.DataFrame([{
            "ts_code": "000001.SH",
            "trade_date": "20260616",
            "open": 3000.0,
            "high": 3050.0,
            "low": 2990.0,
            "close": 3040.0,
            "pre_close": 3001.0,
            "pct_chg": 1.3,
            "vol": 10000,
            "amount": 999.0,
        }]),
        "index_daily",
    )

    summary = persist_market_dataset_silver(
        ds,
        duckdb_path=tmp_path / "factors.duckdb",
        silver_dir=tmp_path / "silver",
        quality_dir=tmp_path / "quality",
    )

    assert summary["trade_date"] == "20260616"
    assert summary["writes"]["stock_daily_silver"]["rows"] == 1
    assert (tmp_path / "quality" / "quality_20260616.json").exists()
    assert (tmp_path / "silver" / "stock_daily_silver.parquet").exists()
    report = json.loads((tmp_path / "quality" / "quality_20260616.json").read_text(encoding="utf-8"))
    assert report["tables"]["stock_daily_silver"]["rows"] == 1
