import pandas as pd

from risk.kelly_sizer import KellySizer
from risk.portfolio_allocator import historical_cvar, portfolio_risk_report, risk_parity_weights
from risk.risk_config import RiskConfig


def test_conservative_kelly_uses_lower_bound_and_quality_discount():
    cfg = RiskConfig(kelly_fraction=0.25, kelly_min_samples=50, kelly_max_position=0.10)
    result = KellySizer(cfg).size(
        0.65, 2.0, 200, 0.10,
        data_quality=0.9, regime_match=0.8, tradability=0.9, correlation_penalty=0.2,
    )
    assert result["method"] == "conservative_kelly"
    assert 0 < result["position_pct"] <= 0.10
    assert result["win_rate_lower"] < 0.65


def test_kelly_falls_back_to_fixed_risk_for_small_sample():
    cfg = RiskConfig(kelly_min_samples=50, fixed_risk_per_trade=0.005)
    result = KellySizer(cfg).size(0.8, 2.0, 10, 0.20, stop_distance=0.05)
    assert result["method"] == "fallback_fixed_risk_insufficient_samples"
    assert result["position_pct"] == 0.1


def test_risk_parity_and_cvar_report():
    returns = pd.DataFrame({
        "a": [0.01, -0.01, 0.02, -0.02],
        "b": [0.005, -0.004, 0.006, -0.005],
    })
    weights = risk_parity_weights(returns, max_weight=0.6, total_position=0.8)
    report = portfolio_risk_report(returns, weights)
    assert abs(sum(weights.values()) - 0.8) < 1e-6
    assert weights["b"] > weights["a"]
    assert report["status"] == "ok"
    assert historical_cvar([-0.1, -0.05, 0.1])["cvar"] <= -0.05
