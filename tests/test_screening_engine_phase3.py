import importlib.util

import pandas as pd
import pytest

from core.screening.screening_engine import ScreeningEngine


DUCKDB_MISSING = importlib.util.find_spec("duckdb") is None


def test_screening_compare_value_ops():
    assert ScreeningEngine.compare_value(60, ">=", 50) is True
    assert ScreeningEngine.compare_value(40, "<", 50) is True
    assert ScreeningEngine.compare_value("退潮期", "not_in", ["系统性风险"]) is True
    assert ScreeningEngine.compare_value(55, "between", [50, 60]) is True


def test_candidate_percentile_score_avoids_absolute_score_saturation(tmp_path):
    engine = ScreeningEngine(duckdb_path=tmp_path / "none.duckdb", output_dir=tmp_path)
    frame = pd.DataFrame({"tech_score": [98.0, 99.0, 100.0]})
    cfg = {"ranking": {"candidate_percentile": True, "weights": {"tech_score": 1.0}}}

    score = engine._ranking_score(frame, cfg, 50.0)

    assert score.iloc[0] < 40
    assert 60 < score.iloc[1] < 70
    assert score.iloc[2] == 100


@pytest.mark.skipif(DUCKDB_MISSING, reason="duckdb is not installed in this Python environment")
def test_screening_engine_reads_gold_tables_and_writes_json(tmp_path):
    import duckdb  # type: ignore

    db_path = tmp_path / "factors.duckdb"
    con = duckdb.connect(str(db_path))
    stock_wide = pd.DataFrame([
        {
            "trade_date": "20260616",
            "code": "000001",
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "total_score": 82.0,
            "rank": 1,
            "liquidity_score": 90.0,
            "sector_resonance_score": 50.0,
        },
        {
            "trade_date": "20260616",
            "code": "600000",
            "ts_code": "600000.SH",
            "name": "浦发银行",
            "total_score": 48.0,
            "rank": 2,
            "liquidity_score": 45.0,
            "sector_resonance_score": 50.0,
        },
    ])
    value_long = pd.DataFrame([
        ["20260616", "market", "market", "mkt_market_score", 70.0],
        ["20260616", "stock", "000001", "stk_total_score", 82.0],
        ["20260616", "stock", "000001", "stk_liquidity_percentile", 90.0],
        ["20260616", "stock", "000001", "stk_amount_ratio_5d", 80.0],
        ["20260616", "stock", "000001", "stk_vol_ratio_5d", 75.0],
        ["20260616", "stock", "000001", "stk_new_high_20d", 72.0],
        ["20260616", "stock", "600000", "stk_total_score", 48.0],
        ["20260616", "stock", "600000", "stk_liquidity_percentile", 45.0],
        ["20260616", "stock", "600000", "stk_amount_ratio_5d", 35.0],
        ["20260616", "stock", "600000", "stk_vol_ratio_5d", 30.0],
        ["20260616", "stock", "600000", "stk_new_high_20d", 40.0],
    ], columns=["trade_date", "entity_type", "entity_id", "factor_id", "score"])
    con.register("stock_wide", stock_wide)
    con.register("value_long", value_long)
    con.execute("CREATE TABLE factor_stock_wide AS SELECT * FROM stock_wide")
    con.execute("CREATE TABLE factor_value_long AS SELECT * FROM value_long")
    con.close()

    engine = ScreeningEngine(duckdb_path=db_path, output_dir=tmp_path / "screening")
    result = engine.run("20260616", persist=True)

    assert result.ok is True
    assert result.input_count == 2
    assert result.final[0]["code"] == "000001"
    assert result.final[0]["score"] > result.final[-1]["score"]
    assert result.traces
    assert (tmp_path / "screening" / "screening_20260616.json").exists()
