"""Display scoring must remain causal and honest about incomplete evidence."""

from copy import deepcopy

import pytest

from web.sector_review import hotspots, mainlines

DATES = [f"202610{day:02d}" for day in range(10, 20)]


def sample():
    current = [{"sector_code": f"88{i:04d}.TI", "sector_name": f"主题{i}", "sector_type": "N"} for i in range(5)]
    history = [
        {
            "sector_code": row["sector_code"],
            "trade_date": date,
            "pct_chg": 2 if i == 0 else 0,
            "amount_yuan": 100 if n < 7 else 160,
        }
        for i, row in enumerate(current)
        for n, date in enumerate(DATES)
    ]
    return current, history


def calculate(current, history):
    return mainlines(current, history, as_of=DATES[-1], expected_dates=DATES)


def test_persistent_relative_leader_and_compounding():
    current, history = sample()
    before = deepcopy((current, history))
    result = calculate(current, history)
    assert result[0]["_review_qualified"] is True
    assert result[0]["近10日累计涨幅%"] == pytest.approx(21.90)
    assert result[0]["_review_score_coverage"] == 85
    assert (current, history) == before
    assert all(not row["_review_qualified"] for row in result[1:])


def test_future_rows_cannot_change_result():
    current, history = sample()
    expected = calculate(current, history)
    history.append({**history[0], "trade_date": "20261020", "pct_chg": 99})
    assert calculate(current, history) == expected


def test_missing_entire_sessions_not_extended_or_zero_filled():
    current, history = sample()
    result = calculate(current, [row for row in history if row["trade_date"] in DATES[-6:]])
    assert result[0]["_review_days"] == 6
    assert result[0]["_review_history_coverage"] == 60
    assert result[0]["_review_status"] == "数据不足"
    assert not result[0]["_review_qualified"]


def test_conflicting_duplicates_excluded_identical_duplicates_harmless():
    current, history = sample()
    assert calculate(current, history + [dict(history[0])]) == calculate(current, history)
    result = calculate(current, history + [{**history[0], "pct_chg": -5}])
    assert result[0]["_review_days"] == 9


def test_sparse_peer_data_cannot_establish_relative_leadership():
    current, history = sample()
    assert calculate(current, history[:10]) == []


def test_market_wide_rally_not_a_unique_mainline():
    current, history = sample()
    for row in history:
        row["pct_chg"] = 2
    assert all(not row["_review_qualified"] for row in calculate(current, history))


def test_concept_scope_aliases_and_unknown_not_zero():
    rows = [
        {"sector_code": "884218.TI", "sector_type": "N", "sector_name": "机器人", "momentum_score": 80},
        {"sector_code": "885517.TI", "sector_type": "N", "sector_name": "机器人概念", "momentum_score": 70},
        {"sector_code": "700408.TI", "sector_type": "N", "sector_name": "软件(A股)", "momentum_score": 99},
        {"sector_code": "881001.TI", "sector_type": "I", "sector_name": "行业", "momentum_score": 99},
        {
            "sector_code": "886001.TI",
            "sector_type": "N",
            "sector_name": "未知",
            "momentum_score": float("nan"),
            "amount_score": 200,
        },
    ]
    result = hotspots(rows)
    assert len(result) == 2
    assert result[0]["_review_score"] == 80
    assert result[0]["_review_score_coverage"] == 45
    assert result[0]["_review_aliases"][0]["code"] == "885517.TI"
    assert result[1]["_review_score"] is None
    assert result[1]["_review_status"] == "数据不足"


def test_missing_amount_does_not_become_zero_volume():
    current, history = sample()
    for row in history:
        row.pop("amount_yuan")
    leader = calculate(current, history)[0]
    assert leader["_review_amount_ratio"] is None
    assert leader["_review_score_coverage"] == 65
    assert leader["_review_status"] == "数据不足"
