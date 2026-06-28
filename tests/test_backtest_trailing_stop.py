from __future__ import annotations

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from risk.risk_config import RiskConfig


class DailyRows:
    def __init__(self, rows):
        self.rows = rows

    def get_stock_daily_data(self, ts_code, trade_date):
        return self.rows.get((ts_code, trade_date), {})


def _position():
    return {
        "stock_name": "测试股票",
        "entry_date": "20260622",
        "entry_price": 10.0,
        "shares": 1000,
        "cost_basis": 10000.0,
        "market_value": 10000.0,
        "pattern_type": "指标筛选/default",
        "hot_resonance": False,
        "resonance_sectors": "",
        "plan_rank": 1,
        "plan_score": 90.0,
        "plan_reason": "test",
        "factor_metrics_json": "{}",
        "stop_loss_price": 9.5,
        "highest_price": 10.0,
    }


def test_rising_stock_has_no_fixed_or_partial_take_profit():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 10.2, "high": 11.2, "low": 10.1, "close": 11.0, "pre_close": 10.0,
        },
    })
    config = BacktestConfig(
        trailing_stop_pct=0.08,
        trailing_activation_pct=0.05,
        time_stop_days=999,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage=0,
    )
    engine = BacktestEngine(dm, config)
    engine.current_positions["000001"] = _position()

    engine._check_stop_loss_take_profit("20260623")

    assert "000001" in engine.current_positions
    assert engine.current_positions["000001"]["highest_price"] == 11.2
    assert not engine.trade_history


def test_pullback_from_session_high_exits_full_position_as_take_profit():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 11.5, "high": 12.0, "low": 10.8, "close": 10.95, "pre_close": 11.0,
        },
    })
    config = BacktestConfig(
        trailing_stop_pct=0.08,
        trailing_activation_pct=0.05,
        time_stop_days=999,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage=0,
    )
    engine = BacktestEngine(dm, config)
    engine.current_positions["000001"] = _position()

    engine._check_stop_loss_take_profit("20260623")

    assert "000001" not in engine.current_positions
    trade = engine.trade_history[-1]
    assert trade.action == "SELL"
    assert trade.shares == 1000
    assert trade.exit_reason == "trailing_stop"
    assert trade.take_profit_triggered is True
    assert trade.stop_loss_triggered is False


def test_hard_stop_loss_behavior_is_preserved():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 9.6, "high": 9.7, "low": 9.3, "close": 9.4, "pre_close": 10.0,
        },
    })
    config = BacktestConfig(
        time_stop_days=999,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage=0,
    )
    engine = BacktestEngine(dm, config)
    engine.current_positions["000001"] = _position()

    engine._check_stop_loss_take_profit("20260623")

    trade = engine.trade_history[-1]
    assert trade.exit_reason == "stop_loss"
    assert trade.stop_loss_triggered is True
    assert trade.take_profit_triggered is False


def test_trailing_pullback_uses_profit_stages():
    engine = BacktestEngine(None, BacktestConfig(
        trailing_early_stop_pct=0.04,
        trailing_mid_stop_pct=0.06,
        trailing_stop_pct=0.10,
    ))

    assert engine._trailing_stop_distance(0.07) == 0.04
    assert engine._trailing_stop_distance(0.15) == 0.06
    assert engine._trailing_stop_distance(0.30) == 0.10


def test_risk_projection_keeps_simulation_specific_strategy_thresholds():
    config = BacktestConfig.from_risk_config(RiskConfig(
        market_entry_threshold=50,
        market_strong_threshold=70,
        hard_stop_loss=0.05,
        trailing_stop=0.08,
    ))

    assert config.market_entry_threshold == 60
    assert config.market_strong_threshold == 65
    assert config.stop_loss_pct == 0.04
    assert config.trailing_stop_pct == 0.10
