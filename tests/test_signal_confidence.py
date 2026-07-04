import pandas as pd

from backtest.minute_entry import ENTRY_CONTINUATION, ENTRY_WEAK, MinuteEntryEvaluator
from core.signals.confidence_service import ConfidenceService
from core.signals.minute_amount_profile import MinuteAmountProfileRepository, MinuteAmountProfileTrainer


def _minute_bars():
    return pd.DataFrame([
        {"time": f"09:{30 + i:02d}:00", "open": 10 + i * 0.01, "high": 10.05 + i * 0.01,
         "low": 9.98 + i * 0.01, "close": 10.02 + i * 0.01, "volume": 1000, "amount": 10000}
        for i in range(8)
    ])


def test_confidence_without_samples_is_always_unverified():
    result = ConfidenceService.assess(
        calibrated_probability=0.9, sample_size=0, data_completeness=1,
        regime_match=1, tradability=1,
    )
    assert result["candidate_probability"] == 90.0
    assert result["confidence_grade"] == "D"
    assert result["confidence_score"] == 0.0


def test_confidence_profile_resolves_matching_score_bin():
    result = ConfidenceService.from_profile({"bins": [{
        "score_min": 70, "score_max": 80, "sample_size": 150,
        "success_probability": 0.67, "expected_return": 0.032,
        "stop_probability": 0.28, "average_mfe": 0.084, "average_mae": -0.031,
    }]}, score=76, data_completeness=1, regime_match=1, tradability=1)
    assert result["candidate_probability"] == 67.0
    assert result["baseline_probability"] == 50.0
    assert result["probability_lift"] == 1.34
    assert result["expected_return_pct"] == 3.2
    assert result["sample_size"] == 150
    assert result["confidence_grade"] == "A"


def test_confidence_profile_interpolates_per_stock_score():
    profile = {"bins": [
        {"score_center": 60, "sample_size": 100, "success_probability": 0.20,
         "expected_return": 0.0, "stop_probability": 0.40},
        {"score_center": 80, "sample_size": 100, "success_probability": 0.40,
         "expected_return": 0.04, "stop_probability": 0.20},
    ]}
    lower = ConfidenceService.from_profile(profile, score=65)
    higher = ConfidenceService.from_profile(profile, score=75)
    assert lower["candidate_probability"] == 25.0
    assert higher["candidate_probability"] == 35.0
    assert lower["expected_return_pct"] < higher["expected_return_pct"]


def test_confidence_grade_uses_relative_edge_for_strict_event_label():
    result = ConfidenceService.assess(
        calibrated_probability=0.2654,
        baseline_probability=0.2173,
        expected_return=0.0061,
        sample_size=2524,
        data_completeness=1,
        regime_match=1,
        tradability=0.935,
    )
    assert result["probability_lift"] == 1.22
    assert result["confidence_grade"] == "B"
    assert result["confidence_score"] == 26.23


def test_missing_sector_or_auction_is_data_insufficient():
    weak = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK, bars=_minute_bars(), open_gap=0, prev_close=10,
        plan_amount_ratio=1.2, sector_sync=lambda _: None,
    )
    continuation = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_CONTINUATION, bars=_minute_bars(), open_gap=0.02, prev_close=10,
        previous_volume=1000, auction_volume=0, auction_amount=0,
        sector_sync=lambda _: True,
    )
    assert weak.status == "data_insufficient"
    assert weak.data_status == "missing_sector"
    assert continuation.status == "data_insufficient"
    assert continuation.data_status == "missing_auction"


def test_minute_amount_profile_is_learned_from_cache(tmp_path):
    cache = tmp_path / "tick"
    cache.mkdir()
    for day in range(21):
        rows = []
        for minute in range(31):
            rows.append({"time": f"09:{30 + minute:02d}", "amount": (day + 1) * 1000 + minute})
        pd.DataFrame(rows).to_csv(cache / f"000001.SZ_202601{day + 1:02d}.csv", index=False)
    repository = MinuteAmountProfileRepository(tmp_path / "profile.json")
    result = MinuteAmountProfileTrainer(cache_dir=cache, repository=repository).train()
    fraction, samples = repository.expected_fraction(100000, "09:45:00")
    assert result["ok"] is True
    assert 0 < fraction < 1
    assert samples > 0
