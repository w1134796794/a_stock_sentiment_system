"""打板身位（board）子类评分与涨停池 normalizer 单测。"""
import pandas as pd

from core.data.market_dataset import MarketDataset
from core.etl.normalizers import standardize_limit_up_pool_frame, standardize_stock_daily_frame
from core.etl.warehouse import _normalize_stock_tables
from core.factors.jobs.stock_factor_job import (
    _board_height_score,
    _float_mv_fit_score,
    _seal_time_score,
)


def test_board_height_score_is_non_monotonic():
    # 非涨停=中性；二板加速最优；高位衰减
    assert _board_height_score(0) == 50.0
    assert _board_height_score(1) == 70.0
    assert _board_height_score(2) == 90.0
    assert _board_height_score(2) > _board_height_score(1)
    assert _board_height_score(2) > _board_height_score(4)
    assert _board_height_score(7) < _board_height_score(3)


def test_seal_time_score_earlier_is_stronger_and_break_penalized():
    assert _seal_time_score("09:31:00", 0) > _seal_time_score("10:45:00", 0)
    # 同样首封时间，炸板越多分越低
    assert _seal_time_score("09:31:00", 3) < _seal_time_score("09:31:00", 0)
    # 缺封板时间（非涨停）=中性
    assert _seal_time_score("", 0) == 50.0
    assert _seal_time_score("00:00:00", 0) == 50.0


def test_float_mv_fit_prefers_mid_small_cap():
    # 输入为万元：20亿 = 200000 万元
    mid = _float_mv_fit_score(200000.0)      # 20亿
    huge = _float_mv_fit_score(8000000.0)    # 800亿
    tiny = _float_mv_fit_score(30000.0)      # 3亿
    assert mid > huge
    assert mid > tiny
    assert _float_mv_fit_score(0.0) == 50.0  # 缺失=中性


def test_standardize_limit_up_pool_handles_chinese_and_english_columns():
    raw = pd.DataFrame([
        {"ts_code": "300059.SZ", "名称": "东方财富", "first_time": "093100",
         "limit_times": 3, "open_times": 1, "float_mv": 500000.0, "fd_amount": 1.0e8, "pct_chg": 20.0},
        {"代码": "000001", "name": "平安银行", "首次封板时间": "10:05:00",
         "连板数": 1, "炸板次数": 0, "流通市值": 1500000.0, "涨跌幅": 10.0},
    ])
    out = standardize_limit_up_pool_frame(raw, trade_date="20260616")
    assert list(out["code"]) == ["300059", "000001"]
    assert out.iloc[0]["first_time"] == "09:31:00"  # 93100 -> 规范化
    assert out.iloc[0]["limit_times"] == 3.0
    assert out.iloc[1]["limit_times"] == 1.0
    assert out.iloc[1]["float_mv"] == 1500000.0


def test_standardize_limit_up_pool_empty_returns_schema_columns():
    out = standardize_limit_up_pool_frame(pd.DataFrame())
    assert "limit_times" in out.columns
    assert "first_time" in out.columns
    assert out.empty


def test_standardize_stock_daily_carries_market_cap():
    raw = pd.DataFrame([
        {"ts_code": "000001.SZ", "trade_date": "20260616", "close": 11.0, "pct_chg": 2.8,
         "circ_mv": 4000000.0, "total_mv": 5000000.0},
    ])
    out = standardize_stock_daily_frame(raw, source="test")
    assert out.iloc[0]["circ_mv"] == 4000000.0
    assert out.iloc[0]["total_mv"] == 5000000.0


def test_warehouse_merges_daily_basic_market_cap_into_stock_silver():
    ds = MarketDataset(trade_date="20260616")
    ds.all_daily["20260616"] = pd.DataFrame([
        {"ts_code": "000001.SZ", "trade_date": "20260616", "open": 10.7, "high": 11.1,
         "low": 10.5, "close": 11.0, "pre_close": 10.7, "pct_chg": 2.8, "vol": 1800, "amount": 2500},
    ])
    ds.prefetched.add("all_daily")
    # daily_basic 用 ts_code + 万元市值，应按 (trade_date, code) 合并进银表
    ds.daily_basic["20260616"] = pd.DataFrame([
        {"ts_code": "000001.SZ", "trade_date": "20260616", "circ_mv": 4000000.0, "total_mv": 5000000.0},
    ])
    ds.prefetched.add("daily_basic")

    silver = _normalize_stock_tables(ds)
    row = silver[silver["code"] == "000001"].iloc[0]
    assert row["circ_mv"] == 4000000.0
    assert row["total_mv"] == 5000000.0
