import importlib.util

import pandas as pd
import pytest

from core.factors.jobs.gold_utils import percentile_score
from core.factors.jobs.runner import FactorJobRunner


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


def _duckdb():
    if DUCKDB_MISSING:
        pytest.skip("duckdb is not installed in this Python environment")
    import duckdb  # type: ignore

    return duckdb


def _seed_silver_tables(db_path):
    duckdb = _duckdb()
    con = duckdb.connect(str(db_path))
    stock = pd.DataFrame([
        {"trade_date": "20260614", "code": "000001", "ts_code": "000001.SZ", "name": "平安银行", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "pre_close": 9.9, "pct_chg": 3.0, "vol_hand": 1000, "amount_yuan": 1000000, "source": "test", "as_of_date": "20260614", "ingested_at": "now"},
        {"trade_date": "20260615", "code": "000001", "ts_code": "000001.SZ", "name": "平安银行", "open": 10.2, "high": 10.8, "low": 10.0, "close": 10.7, "pre_close": 10.2, "pct_chg": 4.9, "vol_hand": 1200, "amount_yuan": 1200000, "source": "test", "as_of_date": "20260615", "ingested_at": "now"},
        {"trade_date": "20260616", "code": "000001", "ts_code": "000001.SZ", "name": "平安银行", "open": 10.7, "high": 11.1, "low": 10.5, "close": 11.0, "pre_close": 10.7, "pct_chg": 2.8, "vol_hand": 1800, "amount_yuan": 2500000, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
        {"trade_date": "20260614", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 8.0, "high": 8.1, "low": 7.8, "close": 7.9, "pre_close": 8.0, "pct_chg": -1.2, "vol_hand": 900, "amount_yuan": 800000, "source": "test", "as_of_date": "20260614", "ingested_at": "now"},
        {"trade_date": "20260615", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 7.9, "high": 8.0, "low": 7.7, "close": 7.8, "pre_close": 7.9, "pct_chg": -1.3, "vol_hand": 950, "amount_yuan": 820000, "source": "test", "as_of_date": "20260615", "ingested_at": "now"},
        {"trade_date": "20260616", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 7.8, "high": 8.2, "low": 7.8, "close": 8.1, "pre_close": 7.8, "pct_chg": 3.8, "vol_hand": 1500, "amount_yuan": 1600000, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
    ])
    sector = pd.DataFrame([
        {"trade_date": "20260615", "sector_code": "886001", "sector_name": "AI应用", "sector_type": "概念", "open": 100, "high": 105, "low": 98, "close": 104, "pre_close": 100, "pct_chg": 4.0, "vol_hand": 1000, "amount_yuan": 10000000, "member_count": 20, "source": "test", "as_of_date": "20260615", "ingested_at": "now"},
        {"trade_date": "20260616", "sector_code": "886001", "sector_name": "AI应用", "sector_type": "概念", "open": 104, "high": 110, "low": 103, "close": 109, "pre_close": 104, "pct_chg": 4.8, "vol_hand": 1200, "amount_yuan": 15000000, "member_count": 20, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
        {"trade_date": "20260616", "sector_code": "886002", "sector_name": "机器人", "sector_type": "概念", "open": 90, "high": 92, "low": 88, "close": 89, "pre_close": 90, "pct_chg": -1.1, "vol_hand": 800, "amount_yuan": 7000000, "member_count": 18, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
    ])
    con.register("stock_df", stock)
    con.register("sector_df", sector)
    con.execute("CREATE TABLE stock_daily_silver AS SELECT * FROM stock_df")
    con.execute("CREATE TABLE sector_daily_silver AS SELECT * FROM sector_df")
    con.close()


def test_percentile_score_direction():
    higher_scores = percentile_score(pd.Series([1, 2, 3]), higher_better=True)
    lower_scores = percentile_score(pd.Series([1, 2, 3]), higher_better=False)

    assert higher_scores.iloc[2] > higher_scores.iloc[0]
    assert lower_scores.iloc[0] > lower_scores.iloc[2]


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_phase2_factor_jobs_write_gold_tables(tmp_path):
    duckdb = _duckdb()
    db_path = tmp_path / "factors.duckdb"
    _seed_silver_tables(db_path)

    runner = FactorJobRunner(db_path)
    results = runner.run("20260616")

    assert all(r.ok for r in results)
    con = duckdb.connect(str(db_path))
    market_rows = con.execute("SELECT COUNT(*) FROM factor_market_wide").fetchone()[0]
    sector_rows = con.execute("SELECT COUNT(*) FROM factor_sector_wide").fetchone()[0]
    stock_rows = con.execute("SELECT COUNT(*) FROM factor_stock_wide").fetchone()[0]
    long_types = set(row[0] for row in con.execute("SELECT DISTINCT entity_type FROM factor_value_long").fetchall())
    top_stock = con.execute("SELECT code FROM factor_stock_wide ORDER BY rank LIMIT 1").fetchone()[0]
    con.close()

    assert market_rows == 1
    assert sector_rows == 2
    assert stock_rows == 2
    assert {"market", "sector", "stock"} <= long_types
    assert top_stock in {"000001", "600000"}
