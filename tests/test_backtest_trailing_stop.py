from __future__ import annotations

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from risk.risk_config import RiskConfig


class DailyRows:
    def __init__(self, rows):
        self.rows = rows

    def get_stock_daily_data(self, ts_code, trade_date):
        return self.rows.get((ts_code, trade_date), {})


class MinuteRows(DailyRows):
    def __init__(self, rows, minutes):
        super().__init__(rows)
        self.minutes = minutes

    def get_stock_tick(self, ts_code, trade_date):
        return self.minutes.get((ts_code, trade_date), pd.DataFrame())


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
        daily_ohlc_path_policy="optimistic_high_first",
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
        daily_ohlc_path_policy="optimistic_high_first",
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


def test_conservative_daily_path_does_not_assume_new_high_precedes_low():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 11.5, "high": 12.0, "low": 10.8, "close": 10.95, "pre_close": 11.0,
        },
    })
    engine = BacktestEngine(dm, BacktestConfig(
        trailing_stop_pct=0.08,
        trailing_activation_pct=0.05,
        daily_ohlc_path_policy="conservative_stop_first",
        time_stop_days=999,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage=0,
    ))
    engine.current_positions["000001"] = _position()

    engine._check_stop_loss_take_profit("20260623")

    assert "000001" in engine.current_positions
    assert engine._exit_execution_audit["ambiguous_daily_bars"] == 1


def test_minute_exit_uses_chronological_trailing_threshold():
    date = "20260623"
    dm = MinuteRows({
        ("000001.SZ", date): {"open": 10.2, "high": 11.5, "low": 10.1, "close": 10.6, "pre_close": 10.0},
    }, {
        ("000001.SZ", date): pd.DataFrame([
            {"time": "09:30", "open": 10.2, "high": 10.8, "low": 10.2, "close": 10.7},
            {"time": "09:31", "open": 10.7, "high": 11.5, "low": 10.7, "close": 11.4},
            {"time": "09:32", "open": 11.4, "high": 11.4, "low": 10.7, "close": 10.8},
        ]),
    })
    engine = BacktestEngine(dm, BacktestConfig(
        trailing_activation_pct=0.05,
        trailing_early_stop_pct=0.04,
        trailing_mid_profit_pct=0.10,
        trailing_mid_stop_pct=0.06,
        exit_minute_data_policy="cache_or_fetch",
        time_stop_days=999,
        commission_rate=0,
        stamp_duty_rate=0,
        slippage=0,
    ))
    engine.current_positions["000001"] = _position()

    engine._check_stop_loss_take_profit(date)

    assert "000001" not in engine.current_positions
    assert engine.trade_history[-1].exit_reason == "trailing_stop_minute"
    assert engine._exit_execution_audit["minute_exit_triggers"] == 1


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


def test_risk_projection_uses_single_risk_configuration_source():
    config = BacktestConfig.from_risk_config(RiskConfig(
        market_entry_threshold=50,
        market_strong_threshold=70,
        hard_stop_loss=0.05,
        trailing_stop=0.08,
    ))

    assert config.market_entry_threshold == 50
    assert config.market_strong_threshold == 65
    assert config.market_hot_threshold == 70
    assert config.stop_loss_pct == 0.05
    assert config.trailing_stop_pct == 0.08


def test_entry_day_stop_is_recorded_but_not_sold_due_to_t_plus_one():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 10.1, "high": 10.2, "low": 9.3, "close": 9.4, "pre_close": 10.0,
        },
    })
    engine = BacktestEngine(dm, BacktestConfig(
        commission_rate=0, stamp_duty_rate=0, slippage=0,
    ))
    engine.current_positions["000001"] = _position()

    engine._check_entry_day_stop("000001", "20260623", "竞价买点")

    assert "000001" in engine.current_positions
    assert engine.current_positions["000001"]["entry_day_stop_breached"] is True
    assert engine.trade_history == []


def test_corporate_action_adjusts_excursion_price_anchors():
    engine = BacktestEngine(None, BacktestConfig())
    position = _position()
    position.update({
        "entry_price": 100.0,
        "highest_price": 120.0,
        "stop_loss_price": 95.0,
        "max_favorable_price": 125.0,
        "min_adverse_price": 90.0,
        "last_close": 110.0,
        "shares": 1000,
    })

    engine._apply_corporate_action_adjustment(
        "000001", position, {"pre_close": 88.0}, "20260623",
    )

    assert position["entry_price"] == 80.0
    assert position["highest_price"] == 96.0
    assert position["stop_loss_price"] == 76.0
    assert position["max_favorable_price"] == 100.0
    assert position["min_adverse_price"] == 72.0
    assert position["last_close"] == 88.0
    assert position["shares"] == 1250


def test_corporate_action_price_break_keeps_position_value_continuous():
    dm = DailyRows({
        ("000001.SZ", "20260623"): {
            "open": 7.1, "high": 7.5, "low": 6.9, "close": 7.3, "pre_close": 7.0,
        },
    })
    engine = BacktestEngine(dm, BacktestConfig(
        time_stop_days=999, commission_rate=0, stamp_duty_rate=0, slippage=0,
    ))
    position = _position()
    position["last_close"] = 10.0
    engine.current_positions["000001"] = position

    engine._check_stop_loss_take_profit("20260623")

    adjusted = engine.current_positions["000001"]
    assert adjusted["entry_price"] == 7.0
    assert round(adjusted["stop_loss_price"], 6) == 6.65
    assert round(adjusted["shares"], 6) == round(1000 / 0.7, 6)
    assert adjusted["market_value"] > 10000
