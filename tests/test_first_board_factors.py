from pathlib import Path

import pandas as pd

from core.factors.jobs.first_board_factors import (
    build_first_board_stock_metrics,
    first_board_market_metrics,
)


class _NoHistoryConnection:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("no historical table")


def test_first_board_stock_metrics_distinguish_pioneer_from_follower():
    today = pd.DataFrame([
        {"code": "000001", "pct_chg": 10.0, "amount_ratio_score": 90.0},
        {"code": "000002", "pct_chg": 10.0, "amount_ratio_score": 80.0},
        {"code": "000003", "pct_chg": 6.0, "amount_ratio_score": 60.0},
        {"code": "000004", "pct_chg": 2.0, "amount_ratio_score": 50.0},
    ])
    pool = {
        "000001": {"limit_times": 1, "first_time": "09:32:00"},
        "000002": {"limit_times": 1, "first_time": "10:10:00"},
    }
    sector_scores = {
        "885001": {
            "momentum_score": 90.0,
            "amount_ratio_score": 85.0,
        },
    }
    memberships = {
        code: [{"code": "885001", "name": "机器人", "type": "N"}]
        for code in today["code"]
    }

    metrics = build_first_board_stock_metrics(
        _NoHistoryConnection(), "20260730", today, pool, sector_scores, memberships,
    )

    assert metrics["000001"]["first_board_factor_available"] == 1
    assert metrics["000001"]["first_board_leadership_score"] == 100.0
    assert metrics["000002"]["first_board_leadership_score"] == 0.0
    assert (
        metrics["000001"]["first_board_resonance_score"]
        > metrics["000002"]["first_board_resonance_score"]
    )
    assert metrics["000003"]["first_board_resonance_score"] == 0.0


def test_first_board_market_metrics_use_official_first_board_pool(tmp_path: Path):
    import duckdb

    membership_dir = tmp_path / "sector" / "stock_sectors"
    membership_dir.mkdir(parents=True)
    for code in ("000001", "000002", "000003"):
        pd.DataFrame([
            {"ts_code": "885001.TI", "name": "机器人", "type": "N"},
        ]).to_csv(membership_dir / f"{code}.SZ.csv", index=False)

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE stock_daily_silver(trade_date VARCHAR, code VARCHAR)"
    )
    con.execute("INSERT INTO stock_daily_silver VALUES ('20260729','000001'),('20260730','000001')")
    con.execute(
        "CREATE TABLE limit_up_pool_silver(trade_date VARCHAR, code VARCHAR, limit_times DOUBLE)"
    )
    con.execute(
        "INSERT INTO limit_up_pool_silver VALUES "
        "('20260729','000001',1),('20260730','000001',1),"
        "('20260730','000002',1),('20260730','000003',1)"
    )
    con.execute(
        "CREATE TABLE sector_daily_silver("
        "trade_date VARCHAR, sector_code VARCHAR, sector_name VARCHAR, pct_chg DOUBLE)"
    )
    con.execute(
        "INSERT INTO sector_daily_silver VALUES "
        "('20260730','885001','机器人',4.0),('20260730','885002','银行',-1.0)"
    )
    current_pool = con.execute(
        "SELECT code, limit_times FROM limit_up_pool_silver WHERE trade_date='20260730'"
    ).fetchdf()

    result = first_board_market_metrics(con, "20260730", current_pool, tmp_path)
    con.close()

    assert result["first_board_sector_resonance_ratio"] == 1.0
    assert result["first_board_cluster_count"] == 1
    assert result["first_board_follow_through_ratio"] == 1.0
