import importlib.util

import pandas as pd
import pytest

from core.etl.daily_pipeline import ETLDailyPipeline


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


class FakeETLDataManager:
    def get_limit_up_pool(self, trade_date):
        return pd.DataFrame([{"代码": "000001", "名称": "平安银行"}])

    def get_all_stocks_daily(self, trade_date):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": trade_date, "name": "平安银行", "open": 10.7, "high": 11.1, "low": 10.5, "close": 11.0, "pre_close": 10.7, "pct_chg": 2.8, "vol": 1800, "amount": 2500},
            {"ts_code": "600000.SH", "trade_date": trade_date, "name": "浦发银行", "open": 7.8, "high": 8.2, "low": 7.8, "close": 8.1, "pre_close": 7.8, "pct_chg": 3.8, "vol": 1500, "amount": 1600},
        ])

    def get_stocks_daily_batch(self, codes, start, end):
        return {
            "000001": pd.DataFrame([
                {"ts_code": "000001.SZ", "trade_date": "20260614", "name": "平安银行", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "pre_close": 9.9, "pct_chg": 3.0, "vol": 1000, "amount": 1000},
                {"ts_code": "000001.SZ", "trade_date": "20260615", "name": "平安银行", "open": 10.2, "high": 10.8, "low": 10.0, "close": 10.7, "pre_close": 10.2, "pct_chg": 4.9, "vol": 1200, "amount": 1200},
                {"ts_code": "000001.SZ", "trade_date": "20260616", "name": "平安银行", "open": 10.7, "high": 11.1, "low": 10.5, "close": 11.0, "pre_close": 10.7, "pct_chg": 2.8, "vol": 1800, "amount": 2500},
            ])
        }

    def get_ths_daily(self, trade_date):
        return pd.DataFrame([
            {"ts_code": "886001", "trade_date": trade_date, "name": "AI应用", "open": 100, "high": 110, "low": 98, "close": 109, "pre_close": 104, "pct_chg": 4.8, "amount": 15000000},
            {"ts_code": "886002", "trade_date": trade_date, "name": "机器人", "open": 90, "high": 92, "low": 88, "close": 89, "pre_close": 90, "pct_chg": -1.1, "amount": 7000000},
        ])

    def get_index_daily(self, code, start, end):
        return pd.DataFrame([
            {"ts_code": code, "trade_date": "20260616", "open": 3000, "high": 3050, "low": 2990, "close": 3040, "pre_close": 3001, "pct_chg": 1.3, "vol": 10000, "amount": 999}
        ])


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_etl_daily_pipeline_writes_snapshot_and_screening(tmp_path):
    pipeline = ETLDailyPipeline(
        FakeETLDataManager(),
        duckdb_path=tmp_path / "factors.duckdb",
        web_data_dir=tmp_path / "webdata",
        snapshot_dir=tmp_path / "snapshots",
        app_db_path=tmp_path / "app.sqlite",
        kb_db_path=tmp_path / "kb.sqlite",
        ingest_kb=False,
    )

    result = pipeline.run("20260616", "20260615")

    assert result.ok is True
    assert result.screening["final_count"] >= 1
    assert result.snapshot_paths.get("json")
    assert (tmp_path / "snapshots" / "20260616.json").exists()
    assert (tmp_path / "webdata" / "screening" / "screening_20260616.json").exists()
