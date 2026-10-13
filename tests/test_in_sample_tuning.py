from copy import deepcopy

import pytest

from scripts.summarize_in_sample_tuning import rank_trials


def test_rank_keeps_unprofitable_insufficient_and_incomplete_trials_out():
    row = {"complete": True, "blocked": [], "data_quality": {},
           "summary": {"return_pct": 1., "closed_trades": 10, "max_drawdown_pct": 3.}}
    results = {"profit": row, "loss": deepcopy(row), "tiny": deepcopy(row),
               "partial": deepcopy(row), "missing": deepcopy(row), "tail": deepcopy(row)}
    results["loss"]["summary"]["return_pct"] = -1
    results["tiny"]["summary"].update(return_pct=10, closed_trades=3)
    results["partial"].update(complete=False)
    results["missing"]["data_quality"]["skipped_candidate_count"] = 1
    results["tail"]["summary"].update(return_pct=10, max_drawdown_pct=6)
    policy = {"minimum_closed_trades": 10, "maximum_drawdown_pct": 5}
    assert rank_trials(results, policy) == "profit"
    results["profit"]["summary"]["return_pct"] = 0
    assert rank_trials(results, policy) is None


@pytest.mark.parametrize("arm", ["combined_quality", "combined_patient", "combined_sustain3", "combined_weak_regime"])
def test_combined_variants_keep_risk_and_both_gates(arm):
    from core.operations.strategy_optimization import PULLBACK_GATE_ARMS, REVERSAL_GATE_ARMS, RISK_ARMS
    assert arm in PULLBACK_GATE_ARMS & REVERSAL_GATE_ARMS & RISK_ARMS


def test_fill_audit_checks_registered_window_and_real_next_minute_price():
    import pandas as pd

    from scripts.audit_tuning_execution import check_buy
    frame = pd.DataFrame({"time": ["09:35:00", "09:36:00", "09:37:00", "09:38:00", "09:39:00"],
        "close": [10.1] * 5, "vwap": [10.] * 5, "volume": [100] * 5, "open": [10.05] * 5})
    trade = {"entry_time": "09:39:00", "entry_price": 10.05 * 1.002, "shares": 100}
    attempt = {"initial_confirm_time": "09:35:00", "confirm_time": "09:38:00", "breakout_level": 10.,
        "confirmation_policy": "sustain", "sector_confirmed": True, "sector_evidence": {"at_time": "09:38:00",
            "observed_count": 2, "minimum_observations": 2, "breadth": 1., "positive_ratio_threshold": .55}}
    check_buy(trade, attempt, frame, sustain_minutes=3, slippage=.002)
    with pytest.raises(AssertionError):
        check_buy(trade, attempt, frame, sustain_minutes=2, slippage=.002)
    trade["entry_price"] = 9.99
    with pytest.raises(AssertionError):
        check_buy(trade, attempt, frame, sustain_minutes=3, slippage=.002)
