import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine, TradeRecord


def _sell(pnl_pct: float) -> TradeRecord:
    return TradeRecord(
        date="20260601", stock_code="000001", stock_name="测试",
        pattern_type="指标筛选/default", action="SELL", entry_price=10.0,
        exit_price=10.0 * (1 + pnl_pct), shares=100, position_size=1000.0,
        pnl=1000.0 * pnl_pct, pnl_pct=pnl_pct, holding_days=1,
        hot_resonance=False, resonance_sectors="",
    )


def test_fixed_risk_sizes_by_account_risk_and_stop_distance():
    config = BacktestConfig(
        initial_capital=1_000_000,
        position_sizing_mode="fixed_risk",
        fixed_risk_per_trade=0.005,
        stop_loss_pct=0.04,
        kelly_max_position=0.20,
    )
    engine = BacktestEngine(None, config)
    value = engine._calculate_position_size(pd.Series({"模式": "指标筛选/default", "仓位": "heavy"}))
    assert value == 125_000
    assert engine._last_sizing_meta["method"] == "fixed_risk"


def test_explicit_plan_base_position_can_raise_size_within_risk_caps():
    config = BacktestConfig(
        initial_capital=100_000,
        position_sizing_mode="fixed_risk",
        fixed_risk_per_trade=0.04,
        stop_loss_pct=0.05,
        max_position_per_stock=0.70,
        kelly_max_position=0.70,
    )
    engine = BacktestEngine(None, config)
    value = engine._calculate_position_size(pd.Series({
        "模式": "指标筛选/default",
        "仓位": "heavy",
        "计划基础仓位%": 50,
    }))
    assert value == 50_000
    assert engine._last_sizing_meta["position_pct"] == 0.5


def test_kelly_uses_only_prior_closed_trades_and_rejects_negative_edge():
    config = BacktestConfig(
        initial_capital=1_000_000,
        position_sizing_mode="conservative_kelly",
        kelly_min_samples=10,
        kelly_max_position=0.20,
    )
    engine = BacktestEngine(None, config)
    engine.trade_history = [_sell(-0.04) for _ in range(8)] + [_sell(0.02) for _ in range(2)]
    value = engine._calculate_position_size(pd.Series({"模式": "指标筛选/default", "仓位": "medium"}))
    assert value == 0
    assert engine._last_sizing_meta["method"] == "reject_negative_edge"
    assert engine._last_sizing_meta["sample_size"] == 10


def test_kelly_falls_back_when_sample_is_insufficient():
    config = BacktestConfig(
        initial_capital=1_000_000,
        position_sizing_mode="conservative_kelly",
        kelly_min_samples=50,
        fixed_risk_per_trade=0.005,
        stop_loss_pct=0.05,
    )
    engine = BacktestEngine(None, config)
    engine.trade_history = [_sell(0.10) for _ in range(5)]
    value = engine._calculate_position_size(pd.Series({"模式": "指标筛选/default", "仓位": "medium"}))
    assert value == 100_000
    assert engine._last_sizing_meta["method"] == "fallback_fixed_risk_insufficient_samples"
