import importlib.util

import pandas as pd
import pytest

from backtest.plan_source import _rows_from_screening
from core.etl.normalizers import (
    standardize_lhb_daily_frame,
    standardize_lhb_hot_money_frame,
    standardize_lhb_institution_frame,
)
from core.factors.jobs.lhb_factor_job import LHBFactorJob
from core.screening.screening_engine import ScreeningEngine


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


def test_lhb_normalizers_unify_codes_dates_seats_and_yuan():
    daily = standardize_lhb_daily_frame(pd.DataFrame([{
        "trade_date": "2026-06-16",
        "ts_code": "000001.SZ",
        "name": "平安银行",
        "amount": 1_000_000,
        "l_buy": 300_000,
        "l_sell": 100_000,
        "net_amount": 200_000,
        "reason": "日涨幅偏离",
    }]), as_of_date="20260616")
    institution = standardize_lhb_institution_frame(pd.DataFrame([{
        "trade_date": "20260616",
        "ts_code": "000001.SZ",
        "exalter": "机构专用",
        "buy": 200_000,
        "sell": 50_000,
        "net_buy": 150_000,
    }]), as_of_date="20260616")
    hot_money = standardize_lhb_hot_money_frame(pd.DataFrame([{
        "trade_date": "20260616",
        "ts_code": "000001.SZ",
        "ts_name": "平安银行",
        "hm_name": "测试游资",
        "hm_orgs": "测试营业部",
        "buy_amount": 100_000,
        "sell_amount": 20_000,
        "net_amount": 80_000,
    }]), as_of_date="20260616")

    assert daily.iloc[0]["code"] == "000001"
    assert daily.iloc[0]["trade_date"] == "20260616"
    assert daily.iloc[0]["net_buy_yuan"] == 200_000
    assert institution.iloc[0]["seat_name"] == "机构专用"
    assert bool(institution.iloc[0]["is_institution"]) is True
    assert hot_money.iloc[0]["actor_name"] == "测试游资"
    assert hot_money.iloc[0]["seat_name"] == "测试营业部"


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed")
def test_lhb_job_writes_point_in_time_stock_and_sector_factors(tmp_path, monkeypatch):
    import duckdb
    import core.factors.jobs.lhb_factor_job as module

    cache_dir = tmp_path / "cache"
    membership_dir = cache_dir / "sector" / "stock_sectors"
    membership_dir.mkdir(parents=True)
    pd.DataFrame([{
        "ts_code": "885001.TI", "con_code": "000001.SZ",
        "name": "金融科技", "type": "N",
    }]).to_csv(membership_dir / "000001.SZ.csv", index=False)
    monkeypatch.setattr(module, "CACHE_DIR", cache_dir)

    db_path = tmp_path / "factors.duckdb"
    with duckdb.connect(str(db_path)) as con:
        frames = {
            "lhb_daily_silver": standardize_lhb_daily_frame(pd.DataFrame([
                {"trade_date": "20260615", "ts_code": "000001.SZ", "name": "平安银行", "amount": 2_000_000, "l_buy": 300_000, "l_sell": 200_000, "l_amount": 500_000, "net_amount": 100_000, "turnover_rate": 8, "reason": "三日偏离"},
                {"trade_date": "20260616", "ts_code": "000001.SZ", "name": "平安银行", "amount": 2_500_000, "l_buy": 900_000, "l_sell": 200_000, "l_amount": 1_100_000, "net_amount": 700_000, "turnover_rate": 12, "reason": "日涨幅偏离"},
            ]), as_of_date="20260616"),
            "lhb_institution_silver": standardize_lhb_institution_frame(pd.DataFrame([
                {"trade_date": "20260616", "ts_code": "000001.SZ", "exalter": "机构专用", "buy": 300_000, "sell": 20_000, "net_buy": 280_000},
                {"trade_date": "20260616", "ts_code": "000001.SZ", "exalter": "普通营业部", "buy": 100_000, "sell": 30_000, "net_buy": 70_000},
            ]), as_of_date="20260616"),
            "lhb_hot_money_silver": standardize_lhb_hot_money_frame(pd.DataFrame([
                {"trade_date": "20260616", "ts_code": "000001.SZ", "ts_name": "平安银行", "hm_name": "测试游资", "hm_orgs": "测试营业部", "buy_amount": 100_000, "sell_amount": 10_000, "net_amount": 90_000},
            ]), as_of_date="20260616"),
            "stock_daily_silver": pd.DataFrame([{
                "trade_date": "20260616", "code": "000001", "ts_code": "000001.SZ",
                "name": "平安银行", "amount_yuan": 2_500_000,
            }]),
            "sector_daily_silver": pd.DataFrame([{
                "trade_date": "20260616", "sector_code": "885001.TI",
                "sector_name": "金融科技", "sector_type": "概念", "amount_yuan": 10_000_000,
            }]),
        }
        for name, frame in frames.items():
            con.register("seed", frame)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM seed")
            con.unregister("seed")
        result = LHBFactorJob().run(con, "20260616")
        stock = con.execute("SELECT * FROM factor_lhb_stock_wide").fetchdf().iloc[0]
        sector = con.execute("SELECT * FROM factor_lhb_sector_wide").fetchdf().iloc[0]

    assert result.ok is True
    assert stock["signal_date"] == "20260616"
    assert stock["effective_date"] == "20260617"
    assert stock["lhb_net_buy_score"] > 50
    assert stock["institution_net_buy_score"] > 50
    assert stock["appearance_days_5d"] == 2
    assert stock["sector_lhb_resonance_score"] > 50
    assert sector["sector_code"] == "885001.TI"
    assert sector["lhb_stock_count"] == 1


def test_screening_lhb_is_optional_adjustment_and_builds_four_scenarios(tmp_path):
    engine = ScreeningEngine(duckdb_path=tmp_path / "none.duckdb", output_dir=tmp_path)
    frame = pd.DataFrame([
        {
            "tech_score": 70.0, "stk_total_score": 70.0, "lhb_present": 1,
            "stk_lhb_net_buy_score": 90.0, "stk_lhb_institution_score": 80.0,
            "stk_lhb_institution_consensus": 80.0,
            "stk_lhb_repeat_persistence": 70.0, "stk_lhb_sector_resonance": 85.0,
            "crowding_penalty_score": 10.0,
        },
        {
            "tech_score": 70.0, "stk_total_score": 70.0, "lhb_present": 0,
        },
    ])
    cfg = {
        "ranking": {"weights": {"tech_score": 1.0}},
        "lhb_enhancement": {"enabled": True, "max_total_adjustment": 8},
    }

    scores = engine._scenario_scores(frame, cfg, 50.0)

    assert set(scores) == {"no_lhb", "net_buy", "institution", "lhb_sector"}
    assert scores["no_lhb"].iloc[0] == scores["no_lhb"].iloc[1] == 70.0
    assert scores["lhb_sector"].iloc[0] > scores["no_lhb"].iloc[0]
    assert scores["lhb_sector"].iloc[1] == scores["no_lhb"].iloc[1]


def test_backtest_plan_source_selects_requested_lhb_scenario():
    payload = {"etl": {"screening": {
        "profile": "default",
        "final": [{"code": "000001", "name": "默认", "rank": 1, "score": 70}],
        "scenarios": {
            "no_lhb": [{"code": "000002", "name": "无龙虎榜", "rank": 1, "score": 72}],
        },
    }}}

    rows = _rows_from_screening(payload, lhb_scenario="no_lhb")

    assert rows[0]["股票代码"] == "000002"
    assert rows[0]["龙虎榜口径"] == "no_lhb"
