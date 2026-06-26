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
        {"trade_date": "20260616", "code": "000001", "ts_code": "000001.SZ", "name": "平安银行", "open": 10.7, "high": 11.1, "low": 10.5, "close": 11.0, "pre_close": 10.7, "pct_chg": 2.8, "vol_hand": 1800, "amount_yuan": 2500000, "circ_mv": 4000000.0, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
        {"trade_date": "20260614", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 8.0, "high": 8.1, "low": 7.8, "close": 7.9, "pre_close": 8.0, "pct_chg": -1.2, "vol_hand": 900, "amount_yuan": 800000, "source": "test", "as_of_date": "20260614", "ingested_at": "now"},
        {"trade_date": "20260615", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 7.9, "high": 8.0, "low": 7.7, "close": 7.8, "pre_close": 7.9, "pct_chg": -1.3, "vol_hand": 950, "amount_yuan": 820000, "source": "test", "as_of_date": "20260615", "ingested_at": "now"},
        {"trade_date": "20260616", "code": "600000", "ts_code": "600000.SH", "name": "浦发银行", "open": 7.8, "high": 8.2, "low": 7.8, "close": 8.1, "pre_close": 7.8, "pct_chg": 3.8, "vol_hand": 1500, "amount_yuan": 1600000, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
        {"trade_date": "20260616", "code": "300059", "ts_code": "300059.SZ", "name": "东方财富", "open": 10.0, "high": 11.5, "low": 9.9, "close": 11.27, "pre_close": 10.0, "pct_chg": 12.74, "vol_hand": 5000, "amount_yuan": 5000000, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
    ])
    sector = pd.DataFrame([
        {"trade_date": "20260615", "sector_code": "886001", "sector_name": "AI应用", "sector_type": "概念", "open": 100, "high": 105, "low": 98, "close": 104, "pre_close": 100, "pct_chg": 4.0, "vol_hand": 1000, "amount_yuan": 10000000, "member_count": 20, "source": "test", "as_of_date": "20260615", "ingested_at": "now"},
        {"trade_date": "20260616", "sector_code": "886001", "sector_name": "AI应用", "sector_type": "概念", "open": 104, "high": 110, "low": 103, "close": 109, "pre_close": 104, "pct_chg": 4.8, "vol_hand": 1200, "amount_yuan": 15000000, "member_count": 20, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
        {"trade_date": "20260616", "sector_code": "886002", "sector_name": "机器人", "sector_type": "概念", "open": 90, "high": 92, "low": 88, "close": 89, "pre_close": 90, "pct_chg": -1.1, "vol_hand": 800, "amount_yuan": 7000000, "member_count": 18, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
    ])
    limit_up = pd.DataFrame([
        {"trade_date": "20260616", "code": "300059", "ts_code": "300059.SZ", "name": "东方财富",
         "pct_chg": 20.0, "first_time": "09:31:00", "last_time": "09:31:00", "open_times": 0.0,
         "limit_times": 3.0, "fd_amount": 1.0e8, "float_mv": 500000.0, "total_mv": 800000.0,
         "turnover_ratio": 8.0, "source": "test", "as_of_date": "20260616", "ingested_at": "now"},
    ])
    limit_down = pd.DataFrame(columns=limit_up.columns)
    con.register("stock_df", stock)
    con.register("sector_df", sector)
    con.register("limit_up_df", limit_up)
    con.register("limit_down_df", limit_down)
    con.execute("CREATE TABLE stock_daily_silver AS SELECT * FROM stock_df")
    con.execute("CREATE TABLE sector_daily_silver AS SELECT * FROM sector_df")
    con.execute("CREATE TABLE limit_up_pool_silver AS SELECT * FROM limit_up_df")
    con.execute("CREATE TABLE limit_down_pool_silver AS SELECT * FROM limit_down_df")
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
    growth = con.execute(
        "SELECT limit_pct, limit_progress FROM factor_stock_wide WHERE code = '300059'"
    ).fetchone()
    pct_score_300059 = con.execute(
        "SELECT score FROM factor_value_long "
        "WHERE entity_type = 'stock' AND entity_id = '300059' AND factor_id = 'stk_pct_chg_1d'"
    ).fetchone()[0]
    limit_up_count = con.execute("SELECT limit_up_count FROM factor_market_wide").fetchone()[0]
    limit_down_count = con.execute("SELECT limit_down_count FROM factor_market_wide").fetchone()[0]
    board = con.execute(
        "SELECT board_height, board_height_score, seal_time_score, float_mv, float_mv_fit_score, board_score "
        "FROM factor_stock_wide WHERE code = '300059'"
    ).fetchone()
    board_factor_ids = set(
        row[0] for row in con.execute(
            "SELECT DISTINCT factor_id FROM factor_value_long WHERE entity_type = 'stock'"
        ).fetchall()
    )
    non_lu_board = con.execute(
        "SELECT board_height_score, seal_time_score, float_mv, float_mv_fit_score "
        "FROM factor_stock_wide WHERE code = '000001'"
    ).fetchone()
    con.close()

    assert market_rows == 1
    assert sector_rows == 2
    assert stock_rows == 3
    assert {"market", "sector", "stock"} <= long_types
    assert top_stock in {"000001", "600000", "300059"}
    assert growth[0] == 20.0
    assert 0.63 < growth[1] < 0.64
    assert pct_score_300059 < 90
    assert limit_up_count == 1
    assert limit_down_count == 0

    # 打板身位子类：300059 在涨停池里（3连板、早封、50亿流通市值）
    assert board[0] == 3.0                # board_height
    assert board[1] == 85.0               # 3连板梯队分
    assert board[2] == 95.0               # 09:31 首封、无炸板
    assert board[4] == 90.0               # 50亿流通市值适配分
    assert board[5] > 50.0                # board_score 优于中性
    # 非涨停票：连板/封板维度退化为中性，但流通市值适配分由全市场 circ_mv 驱动
    assert non_lu_board[0] == 50.0        # 非连板 -> 中性
    assert non_lu_board[1] == 50.0        # 无封板时间 -> 中性
    assert non_lu_board[2] == 4000000.0   # 流通市值取自 stock_daily_silver.circ_mv
    assert 37.0 < non_lu_board[3] < 40.0  # 400亿大盘 -> 适配分明显低于中性
    assert {"stk_board_height", "stk_seal_time_quality", "stk_float_mv_fit", "stk_board_position"} <= board_factor_ids
