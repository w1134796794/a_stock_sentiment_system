import importlib.util

import pandas as pd
import pytest

import json

from backtest.plan_source import _rows_from_screening, build_backtest_plan_dir
from core.etl.normalizers import (
    standardize_sector_capital_flow_frame,
    standardize_stock_capital_flow_frame,
)
from core.factors.jobs.short_signal_factor_job import ShortSignalFactorJob


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


def test_short_signal_normalizers_convert_units_and_effective_date():
    stock = standardize_stock_capital_flow_frame(
        pd.DataFrame([{
            "trade_date": "20260626", "ts_code": "000001.SZ", "name": "平安银行",
            "net_amount": 12.5, "buy_lg_amount": 3.0,
        }]),
        trade_date="20260626", effective_date="20260629", source="ths",
    )
    sector = standardize_sector_capital_flow_frame(
        pd.DataFrame([{
            "trade_date": "20260626", "ts_code": "885001.TI", "name": "示例概念",
            "net_amount": 1.2,
        }]),
        trade_date="20260626", effective_date="20260629",
    )

    assert stock.iloc[0]["net_amount_yuan"] == 125000.0
    assert stock.iloc[0]["large_net_yuan"] == 30000.0
    assert stock.iloc[0]["effective_date"] == "20260629"
    assert sector.iloc[0]["net_amount_yuan"] == 120000000.0


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed")
def test_short_signal_job_builds_independent_adjustments(tmp_path):
    import duckdb

    con = duckdb.connect(str(tmp_path / "signals.duckdb"))
    tables = {
        "stock_capital_flow_silver": pd.DataFrame([
            {"trade_date": "20260626", "effective_date": "20260629", "code": "000001", "source": "ths", "net_amount_yuan": 500, "large_net_yuan": 100, "net_5d_amount_yuan": 800},
            {"trade_date": "20260626", "effective_date": "20260629", "code": "000001", "source": "dc", "net_amount_yuan": 400, "large_net_yuan": 80, "net_5d_amount_yuan": 0},
            {"trade_date": "20260626", "effective_date": "20260629", "code": "600000", "source": "ths", "net_amount_yuan": -500, "large_net_yuan": -100, "net_5d_amount_yuan": -800},
            {"trade_date": "20260626", "effective_date": "20260629", "code": "600000", "source": "dc", "net_amount_yuan": -400, "large_net_yuan": -80, "net_5d_amount_yuan": 0},
        ]),
        "stock_attention_silver": pd.DataFrame([
            {"trade_date": "20260626", "effective_date": "20260629", "code": "000001", "source": "ths", "rank": 1},
            {"trade_date": "20260626", "effective_date": "20260629", "code": "600000", "source": "ths", "rank": 90},
        ]),
        "stock_leader_signal_silver": pd.DataFrame([
            {"trade_date": "20260626", "effective_date": "20260629", "code": "000001", "tag": "核心龙头", "status": "连板", "lu_desc": "强势", "limit_order": 100},
        ]),
        "stock_daily_silver": pd.DataFrame([
            {"trade_date": "20260626", "code": "000001", "close": 10.0},
            {"trade_date": "20260626", "code": "600000", "close": 10.0},
        ]),
        "sector_capital_flow_silver": pd.DataFrame([
            {"trade_date": "20260626", "effective_date": "20260629", "sector_code": "885001.TI", "sector_name": "示例概念", "net_amount_yuan": 1000, "pct_chg": 3.0},
            {"trade_date": "20260626", "effective_date": "20260629", "sector_code": "885002.TI", "sector_name": "弱概念", "net_amount_yuan": -1000, "pct_chg": -2.0},
        ]),
    }
    for name, frame in tables.items():
        con.register("frame", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame")
        con.unregister("frame")

    result = ShortSignalFactorJob().run(con, "20260626")
    rows = con.execute(
        "SELECT code, capital_flow_adjustment, leader_adjustment FROM factor_signal_stock_wide ORDER BY code"
    ).fetchall()
    sector_rows = con.execute("SELECT COUNT(*) FROM factor_signal_sector_wide").fetchone()[0]
    con.close()

    assert result.ok is True
    assert rows[0][1] > 0
    assert rows[0][2] > 0
    assert rows[1][1] < 0
    assert sector_rows == 2


def test_backtest_plan_source_reranks_candidate_pool_for_selected_combination():
    screening = {
        "profile": "default",
        "final": [{"code": "000001", "name": "甲", "rank": 1}],
        "candidate_pool": [
            {"code": "000001", "name": "甲", "model_baseline_score": 80, "enhancements": {"capital_flow": 0}},
            {"code": "600000", "name": "乙", "model_baseline_score": 79, "enhancements": {"capital_flow": 3}},
        ],
    }
    payload = {"etl": {"screening": screening}}

    baseline = _rows_from_screening(payload, enhancements=[])
    enhanced = _rows_from_screening(payload, enhancements=["capital_flow"])

    assert baseline[0]["股票代码"] == "000001"
    assert enhanced[0]["股票代码"] == "600000"
    assert enhanced[0]["回测增强组合"] == "基线 + 资金流共识"


def test_enhanced_backtest_rejects_legacy_screening_dates(tmp_path):
    snapshots = tmp_path / "snapshots"
    screening_dir = tmp_path / "screening"
    snapshots.mkdir()
    screening_dir.mkdir()
    (snapshots / "20260625.json").write_text(
        json.dumps({"meta": {"date": "20260625"}}), encoding="utf-8",
    )
    (screening_dir / "screening_20260625.json").write_text(
        json.dumps({"final": [{"code": "000001", "name": "甲", "score": 80}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="20260625"):
        build_backtest_plan_dir(
            snapshot_dir=snapshots,
            screening_dir=screening_dir,
            output_dir=tmp_path,
            enhancements=["capital_flow"],
        )
