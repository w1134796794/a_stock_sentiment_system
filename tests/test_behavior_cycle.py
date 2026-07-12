import pytest

from core.factors.behavior_cycle import (
    intraday_behavior_cycle,
    sector_behavior_cycle,
    stock_behavior_cycle,
)


def _assert_probability_payload(result):
    assert sum(result["probabilities"].values()) == pytest.approx(100.0, abs=0.02)
    assert result["dominant_state"] in result["probabilities"]
    assert result["dominant_label"]


def test_sector_cycle_identifies_consensus_acceleration():
    result = sector_behavior_cycle(
        pct_chg=7.0,
        previous_pct_chg=3.0,
        amount_ratio=1.2,
        current_rank_percentile=0.95,
        previous_rank_percentile=0.70,
        persistence_score=95,
        flow_score=80,
        positive_streak=2,
        breadth_acceleration_score=90,
    )
    _assert_probability_payload(result)
    assert result["dominant_state"] == "acceleration"


def test_stock_cycle_identifies_weak_to_strong_repair():
    result = stock_behavior_cycle(
        open_gap_pct=-2.0,
        close_pct_chg=4.0,
        amount_ratio=1.2,
        amount_ratio_score=90,
        relative_strength_score=90,
        seal_quality_score=50,
        reseal_resilience_score=85,
        crowding_safety_score=80,
        late_seal_safety_score=70,
        board_score=60,
        sector_states={
            "attention": 50, "acceleration": 60, "divergence": 50,
            "repair": 95, "decay": 20,
        },
    )
    _assert_probability_payload(result)
    assert result["dominant_state"] == "repair"
    assert result["atomic"]["repair_quality"] >= 95


def test_stock_cycle_identifies_crowding_decay():
    result = stock_behavior_cycle(
        open_gap_pct=2.0,
        close_pct_chg=-3.0,
        amount_ratio=3.0,
        amount_ratio_score=20,
        relative_strength_score=10,
        seal_quality_score=20,
        reseal_resilience_score=10,
        crowding_safety_score=10,
        late_seal_safety_score=10,
        board_score=30,
        sector_states={
            "attention": 20, "acceleration": 20, "divergence": 80,
            "repair": 20, "decay": 90,
        },
    )
    _assert_probability_payload(result)
    assert result["dominant_state"] == "decay"


def test_intraday_cycle_reuses_weak_to_strong_evidence():
    result = intraday_behavior_cycle(
        entry_mode="weak_only",
        signal_status="confirmed",
        amount_pace=1.2,
        sector_confirmed=True,
        hold_minutes=4,
        false_break_count=0,
        pullback_quality=0.9,
        active_buy_ratio=0.7,
    )
    _assert_probability_payload(result)
    assert result["dominant_state"] == "repair"
