from __future__ import annotations

import pandas as pd

from core.data.data_manager_main import DataManager


class ForbiddenDailyApi:
    def __init__(self):
        self.calls = 0

    def daily(self, **kwargs):
        self.calls += 1
        raise AssertionError("prefetched daily data must not call Tushare")


def test_single_stock_daily_reads_whole_market_prefetch(tmp_path):
    dm = DataManager("", tmp_path)
    api = ForbiddenDailyApi()
    dm.ts_pro = api
    date = "20260623"
    pd.DataFrame([
        {
            "ts_code": "600584.SH", "trade_date": date,
            "open": 31.0, "high": 33.2, "low": 30.8, "close": 32.5,
            "pre_close": 30.5, "change": 2.0, "pct_chg": 6.56,
            "vol": 1000, "amount": 5000,
        }
    ]).to_csv(dm.stock_dir / "all_daily" / f"{date}.csv", index=False)

    row = dm.get_stock_daily_data("600584.SH", date)

    assert row["open"] == 31.0
    assert row["high"] == 33.2
    assert api.calls == 0


def test_trade_plan_cache_is_materialized_without_remote_request(tmp_path):
    dm = DataManager("", tmp_path, allow_remote_history=False)
    date = "20260623"
    pd.DataFrame([
        {"ts_code": "000762.SZ", "trade_date": date, "open": 10, "high": 11,
         "low": 9.9, "close": 10.8, "pre_close": 9.8, "vol": 100, "amount": 200},
    ]).to_csv(dm.stock_dir / "all_daily" / f"{date}.csv", index=False)

    summary = dm.warm_trade_plan_daily_cache(date, ["000762", "002463"])

    assert summary["requested"] == 2
    assert summary["cached"] == 1
    assert summary["missing"] == ["002463"]
    assert (dm.stock_dir / "daily" / f"000762.SZ_{date}_{date}.csv").exists()


def test_offline_backtest_does_not_fetch_missing_history(tmp_path):
    dm = DataManager("", tmp_path, allow_remote_history=False)
    api = ForbiddenDailyApi()
    dm.ts_pro = api

    assert dm.get_stock_daily_data("002463.SZ", "20260623") == {}
    assert api.calls == 0
