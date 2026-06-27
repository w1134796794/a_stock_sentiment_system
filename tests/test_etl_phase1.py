import json
import importlib.util

import pandas as pd
import pytest

from core.data.market_dataset import MarketDataset, call_key
from core.etl.normalizers import (
    normalize_amount_yuan,
    normalize_stock_code,
    normalize_trade_date,
    standardize_sector_daily_frame,
    standardize_stock_daily_frame,
)
from core.etl.warehouse import SilverWarehouse, persist_market_dataset_silver


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


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


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_persist_market_dataset_silver_keeps_historical_trade_dates(tmp_path):
    import duckdb  # type: ignore

    db_path = tmp_path / "factors.duckdb"

    def make_ds(trade_date: str, close: float) -> MarketDataset:
        ds = MarketDataset(trade_date=trade_date)
        ds.all_daily[trade_date] = pd.DataFrame([{
            "ts_code": "000001.SZ",
            "trade_date": trade_date,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "pre_close": close - 0.1,
            "pct_chg": 1.0,
            "vol": 1000,
            "amount": 100.0,
        }])
        return ds

    persist_market_dataset_silver(
        make_ds("20260617", 10.0),
        duckdb_path=db_path,
        silver_dir=tmp_path / "silver",
        quality_dir=tmp_path / "quality",
    )
    persist_market_dataset_silver(
        make_ds("20260618", 11.0),
        duckdb_path=db_path,
        silver_dir=tmp_path / "silver",
        quality_dir=tmp_path / "quality",
    )
    persist_market_dataset_silver(
        make_ds("20260618", 12.0),
        duckdb_path=db_path,
        silver_dir=tmp_path / "silver",
        quality_dir=tmp_path / "quality",
    )

    with duckdb.connect(str(db_path), read_only=True) as con:
        rows = con.execute(
            """
            SELECT trade_date, close
            FROM stock_daily_silver
            ORDER BY trade_date
            """
        ).fetchall()

    assert rows == [("20260617", 10.0), ("20260618", 12.0)]


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_silver_warehouse_migrates_legacy_integer_trade_date(tmp_path):
    import duckdb  # type: ignore

    db_path = tmp_path / "factors.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            CREATE TABLE stock_daily_silver (
                trade_date INTEGER,
                code INTEGER,
                close DOUBLE
            )
            """
        )
        con.execute("INSERT INTO stock_daily_silver VALUES (20260625, 1, 10.0)")
        con.execute("INSERT INTO stock_daily_silver VALUES (20260626, 1, 11.0)")

    incoming = pd.DataFrame([
        {"trade_date": "20260626", "code": "000001", "close": 12.0},
    ])
    result = SilverWarehouse(duckdb_path=db_path).write_table(
        "stock_daily_silver",
        incoming,
        mode="upsert_dates",
    )

    assert result["duckdb"] is True
    with duckdb.connect(str(db_path), read_only=True) as con:
        schema = {row[0]: row[1] for row in con.execute("DESCRIBE stock_daily_silver").fetchall()}
        rows = con.execute(
            "SELECT trade_date, code, close FROM stock_daily_silver ORDER BY trade_date"
        ).fetchall()

    assert schema["trade_date"] == "VARCHAR"
    assert schema["code"] == "VARCHAR"
    assert rows == [("20260625", "1", 10.0), ("20260626", "000001", 12.0)]


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_silver_warehouse_rolls_back_partition_when_insert_fails(tmp_path):
    import duckdb  # type: ignore

    db_path = tmp_path / "factors.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            "CREATE TABLE stock_daily_silver (trade_date INTEGER, code VARCHAR, close DOUBLE)"
        )
        con.execute("INSERT INTO stock_daily_silver VALUES (20260626, '000001', 11.0)")

    incoming = pd.DataFrame([
        {"trade_date": "20260626", "code": "000001", "close": "not-a-number"},
    ])
    result = SilverWarehouse(duckdb_path=db_path).write_table(
        "stock_daily_silver",
        incoming,
        mode="upsert_dates",
    )

    assert result["duckdb"] is False
    with duckdb.connect(str(db_path), read_only=True) as con:
        schema = {row[0]: row[1] for row in con.execute("DESCRIBE stock_daily_silver").fetchall()}
        rows = con.execute("SELECT trade_date, code, close FROM stock_daily_silver").fetchall()

    assert schema["trade_date"] == "INTEGER"
    assert rows == [(20260626, "000001", 11.0)]
