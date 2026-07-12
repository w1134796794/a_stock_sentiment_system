import pandas as pd

from core.factors.jobs.stock_advanced import (
    crowding_metrics,
    late_seal_safety,
    relative_sector_strength,
    reseal_resilience,
    seal_quality,
    sector_rotation_metrics,
)


def test_seal_quality_rewards_early_stable_seal():
    strong, _ = seal_quality(
        {"first_time": "09:31:00", "last_time": "09:31:00", "open_times": 0, "fd_amount": 12},
        100,
    )
    weak, _ = seal_quality(
        {"first_time": "14:20:00", "last_time": "14:50:00", "open_times": 4, "fd_amount": 1},
        100,
    )
    assert strong > weak


def test_rotation_prefers_fresh_accelerating_sector():
    history = pd.DataFrame([
        {"trade_date": "20260701", "sector_code": "A", "momentum_score": 60, "mainline_score": 45},
        {"trade_date": "20260702", "sector_code": "A", "momentum_score": 92, "mainline_score": 75},
        {"trade_date": "20260701", "sector_code": "B", "momentum_score": 55, "mainline_score": 75},
        {"trade_date": "20260702", "sector_code": "B", "momentum_score": 55, "mainline_score": 62},
    ])
    result = sector_rotation_metrics(history)
    assert result["A"]["score"] > result["B"]["score"]
    assert result["A"]["age"] == 1


def test_crowding_penalizes_repeated_limit_ups():
    rows = []
    for index, date in enumerate(["1", "2", "3", "4", "5"]):
        rows.append({"trade_date": date, "code": "000001", "limit_times": index + 1})
        if index == 4:
            rows.append({"trade_date": date, "code": "000002", "limit_times": 1})
    result = crowding_metrics(pd.DataFrame(rows))
    assert result["000001"]["score"] < result["000002"]["score"]


def test_relative_strength_is_ranked_inside_sector():
    today = pd.DataFrame({
        "pct_chg": [10.0, 5.0, 1.0, 8.0],
        "primary_sector_code": ["A", "A", "A", "B"],
    })
    result = relative_sector_strength(today)
    assert result.loc[0, "relative_strength_sector_score"] > result.loc[2, "relative_strength_sector_score"]
    assert result.loc[3, "relative_strength_sector_score"] == 50.0


def test_reseal_and_late_seal_capture_divergence_quality():
    stable = reseal_resilience({
        "first_time": "09:31:00", "last_time": "09:31:00", "open_times": 0,
    })
    unstable = reseal_resilience({
        "first_time": "09:31:00", "last_time": "14:40:00", "open_times": 4,
    })
    late_score, delay = late_seal_safety(
        {"first_time": "13:30:00"}, ["09:35:00", "09:40:00"],
    )
    assert stable > unstable
    assert late_score < 50
    assert delay > 0
