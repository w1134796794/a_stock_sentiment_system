from scripts.experiment_strategy_factors import _promotion_check


def test_promotion_rejects_high_return_with_too_few_trades():
    result = _promotion_check({
        "total_return": 0.2153,
        "closed_trades": 4,
        "max_drawdown": -0.053,
        "win_rate": 1.0,
    })

    assert result["publishable"] is False
    assert result["status"] == "challenge_only"
    assert result["checks"]["target_return"] is True
    assert result["checks"]["minimum_trades"] is False


def test_promotion_accepts_a_broad_stable_sample():
    result = _promotion_check({
        "total_return": 0.22,
        "closed_trades": 28,
        "max_drawdown": -0.11,
        "win_rate": 0.54,
    })

    assert result["publishable"] is True
    assert result["status"] == "publishable"
