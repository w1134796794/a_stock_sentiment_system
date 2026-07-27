from __future__ import annotations

import pandas as pd
import pytest

from backtest.plan_source import _to_backtest_row
from core.factors.jobs.market_factor_job import MarketFactorJob
from core.models.market_state import MarketStateSnapshot, classify_emotion_phase


def test_boom_phase_uses_cycle_and_limit_up_breadth():
    phase, reasons = classify_emotion_phase(
        76,
        {
            "cycle_duration": 6,
            "limit_up_count": 81,
            "limit_down_count": 4,
        },
    )

    assert phase == "boom"
    assert any("持续6天" in reason for reason in reasons)


def test_emotion_risk_flags_adjust_or_veto_board_strategies():
    state = MarketStateSnapshot.resolve(
        76,
        weak_threshold=30,
        strong_threshold=70,
        context={
            "cycle_duration": 7,
            "limit_up_count": 90,
            "limit_down_count": 3,
            "market_emotion_divergence": 41,
            "echelon_integrity": 0.45,
            "prev_limit_up_premium": -1.5,
            "prev_limit_up_positive": 0.30,
            "prev_first_board_gap_up": 0.30,
        },
    )

    assert state.phase == "boom"
    assert state.position_scale == 0.55
    assert "market_emotion_divergence" in state.risk_flags
    assert "echelon_broken" in state.risk_flags
    assert "prev_limit_positive_weak" in state.risk_flags
    assert state.strategy_position_multiplier("ultra_short_board") == 0.0
    assert state.strategy_position_multiplier("first_board_launch") == 0.0
    assert 0 < state.strategy_position_multiplier("mainline_leader") < 0.7


def test_market_factor_job_materializes_cycle_ladder_and_premium_factors():
    import duckdb

    con = duckdb.connect(":memory:")
    previous_date = "20990107"
    trade_date = "20990108"

    previous_codes = [f"{index:06d}" for index in range(1, 6)]
    current_codes = [f"{index:06d}" for index in range(1, 101)]
    stock_rows = []
    for code in previous_codes:
        stock_rows.append({
            "trade_date": previous_date,
            "code": code,
            "ts_code": f"{code}.SZ",
            "pct_chg": 1.0,
            "amount_yuan": 1_000_000.0,
            "open": 10.0,
            "close": 10.0,
            "pre_close": 9.9,
        })
    for code in current_codes:
        stock_rows.append({
            "trade_date": trade_date,
            "code": code,
            "ts_code": f"{code}.SZ",
            "pct_chg": 2.0,
            "amount_yuan": 1_000_000.0,
            "open": 10.1,
            "close": 10.2,
            "pre_close": 10.0,
        })
    stock = pd.DataFrame(stock_rows)
    con.register("_stock", stock)
    con.execute("CREATE TABLE stock_daily_silver AS SELECT * FROM _stock")

    previous_limit = pd.DataFrame({
        "trade_date": [previous_date] * 5,
        "code": previous_codes,
        "ts_code": [f"{code}.SZ" for code in previous_codes],
        "limit_times": [1, 1, 2, 3, 4],
    })
    current_heights = [1] * 70 + [2] * 8 + [3] * 4 + [4] * 2 + [5]
    current_limit = pd.DataFrame({
        "trade_date": [trade_date] * len(current_heights),
        "code": current_codes[:len(current_heights)],
        "ts_code": [f"{code}.SZ" for code in current_codes[:len(current_heights)]],
        "limit_times": current_heights,
    })
    limit_up = pd.concat([previous_limit, current_limit], ignore_index=True)
    con.register("_limit_up", limit_up)
    con.execute("CREATE TABLE limit_up_pool_silver AS SELECT * FROM _limit_up")
    con.execute(
        "CREATE TABLE limit_down_pool_silver("
        "trade_date VARCHAR, code VARCHAR, ts_code VARCHAR)"
    )

    history = pd.DataFrame({
        "trade_date": [f"2099010{index}" for index in range(2, 7)],
        "market_score": [60.0] * 5,
        "emotion_phase": ["active"] * 5,
        "cycle_duration": list(range(1, 6)),
    })
    con.register("_history", history)
    con.execute("CREATE TABLE factor_market_wide AS SELECT * FROM _history")

    result = MarketFactorJob().run(con, trade_date)

    assert result.ok
    row = con.execute(
        "SELECT emotion_phase, cycle_duration, echelon_integrity, "
        "prev_limit_up_premium, prev_limit_up_positive, "
        "prev_first_board_gap_up, market_position_scale, market_score, "
        "limit_up_count, market_emotion_divergence "
        "FROM factor_market_wide WHERE trade_date = ?",
        [trade_date],
    ).fetchone()
    assert row[0] == "boom", {
        "phase": row[0],
        "cycle": row[1],
        "score": row[7],
        "limit_up": row[8],
        "divergence": row[9],
    }
    assert row[1] == 6
    assert row[2] == 1.0
    assert row[3] == pytest.approx(1.0)
    assert row[4] == 1.0
    assert row[5] == 1.0
    assert row[6] == 0.55

    factor_ids = {
        item[0]
        for item in con.execute(
            "SELECT factor_id FROM factor_value_long WHERE trade_date = ?",
            [trade_date],
        ).fetchall()
    }
    assert {
        "F1_cycle_duration",
        "F2_market_emotion_divergence",
        "echelon_integrity",
        "prev_limit_up_premium",
        "prev_limit_up_positive",
        "prev_first_board_gap_up",
    }.issubset(factor_ids)
    con.close()


def test_backtest_plan_keeps_emotion_position_caps():
    row = _to_backtest_row({
        "股票代码": "000001",
        "股票名称": "平安银行",
        "策略ID": "weak_market_probe",
        "策略单票仓位上限%": 8,
        "市场总仓位上限%": 16,
    })

    assert row is not None
    assert row["策略单票仓位上限%"] == 8
    assert row["市场总仓位上限%"] == 16
