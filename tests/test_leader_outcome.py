from __future__ import annotations

from pathlib import Path

import duckdb

from core.signals.leader_outcome import LeaderOutcomeTracker


def test_refresh_outcomes_migrates_integer_columns_and_recomputes_history(tmp_path: Path) -> None:
    db_path = tmp_path / "leader.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute(
            """
            CREATE TABLE leader_signal_history (
                signal_date VARCHAR, code VARCHAR, name VARCHAR,
                lifecycle_state VARCHAR, pool_type VARCHAR, primary_sector VARCHAR,
                leader_score DOUBLE, sector_status_score DOUBLE, market_status_score DOUBLE,
                capital_recognition_score DOUBLE, safety_score DOUBLE,
                next_3d_excess_return INTEGER, mfe_3d INTEGER, mae_3d INTEGER,
                success INTEGER, outcome_date VARCHAR, recorded_at VARCHAR
            )
            """
        )
        con.execute(
            "INSERT INTO leader_signal_history VALUES "
            "('20260105', '000001', '测试股', '萌芽龙头', '板块龙头', '测试板块', "
            "70, 70, 70, 70, 70, 0, 0, 0, 0, '20260108', '2026-01-05T18:00:00')"
        )
        con.execute(
            """
            CREATE TABLE signal_outcome_wide (
                trade_date VARCHAR, code VARCHAR, future_date VARCHAR,
                next_3d_excess_return DOUBLE, mfe_3d DOUBLE, mae_3d DOUBLE,
                label_success DOUBLE
            )
            """
        )
        con.execute(
            "INSERT INTO signal_outcome_wide VALUES "
            "('20260105', '000001', '20260108', 0.0325, 0.086, -0.021, 1.0)"
        )

    tracker = LeaderOutcomeTracker(db_path)
    tracker.refresh_outcomes("20260109")

    with duckdb.connect(str(db_path), read_only=True) as con:
        schema = {row[0]: row[1] for row in con.execute("DESCRIBE leader_signal_history").fetchall()}
        values = con.execute(
            "SELECT next_3d_excess_return, mfe_3d, mae_3d FROM leader_signal_history"
        ).fetchone()

    assert schema["next_3d_excess_return"] == "DOUBLE"
    assert schema["mfe_3d"] == "DOUBLE"
    assert schema["mae_3d"] == "DOUBLE"
    assert values == (0.0325, 0.086, -0.021)
    stats = tracker.stats("萌芽龙头", as_of_date="20260109")
    assert stats["sample_size"] == 1
    assert stats["expected_return"] == 0.0325
    assert stats["average_mfe"] == 0.086
    assert stats["average_mae"] == -0.021
