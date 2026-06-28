import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.point_in_time import StaticPriceProvider
from backtest.replay_engine import ReplayEngine, ReplayPlan
from backtest.trade_simulator import TradeSimulator
from risk.risk_config import RiskConfig


class FakeDailyDataManager:
    def __init__(self, rows):
        self.rows = rows

    def get_stock_daily_data(self, ts_code, trade_date):
        return self.rows.get((ts_code, trade_date), {})


def _plan():
    return pd.Series({
        "代码": "000001",
        "名称": "平安银行",
        "模式": "指标筛选/default",
        "目标价": 10.2,
        "介入时机": "竞价",
        "综合评分": 75,
    })


def test_backtest_engine_requires_positive_open_gap():
    plan = _plan()

    low_dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 9.9, "high": 10.4, "low": 9.8, "close": 10.2, "pre_close": 10.0
        }
    })
    low_engine = BacktestEngine(low_dm)
    can_buy, price = low_engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")
    assert can_buy is False
    assert price == 0

    flat_dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.0, "high": 10.4, "low": 9.9, "close": 10.2, "pre_close": 10.0
        }
    })
    flat_engine = BacktestEngine(flat_dm)
    can_buy, price = flat_engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")
    assert can_buy is False
    assert price == 0

    high_dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.2, "pre_close": 10.0
        }
    })
    high_engine = BacktestEngine(high_dm)
    can_buy, price = high_engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")
    assert can_buy is True
    assert price > 10.1


def test_replay_engine_skips_low_or_flat_open_entries():
    cfg = RiskConfig(enabled=False, initial_capital=100_000, slippage=0)
    plan = ReplayPlan(code="000001", name="平安银行", target_price=0, position_pct=0.2)

    low_engine = ReplayEngine(lambda _: [], StaticPriceProvider({}), config=cfg)
    low_engine._process_entries("20260612", [plan], {
        "000001": {"open": 9.9, "high": 10.2, "low": 9.8, "close": 10.1, "pre_close": 10.0, "vol": 1000}
    })
    assert "000001" not in low_engine.account.positions

    high_engine = ReplayEngine(lambda _: [], StaticPriceProvider({}), config=cfg)
    high_engine._process_entries("20260612", [plan], {
        "000001": {"open": 10.1, "high": 10.4, "low": 10.0, "close": 10.2, "pre_close": 10.0, "vol": 1000}
    })
    assert "000001" in high_engine.account.positions


def test_trade_simulator_open_gap_gate():
    sim = TradeSimulator(FakeDailyDataManager({}))
    assert sim._check_open_gap({"open": 9.9, "pre_close": 10.0})["passed"] is False
    assert sim._check_open_gap({"open": 10.0, "pre_close": 10.0})["passed"] is False
    assert sim._check_open_gap({"open": 10.01, "pre_close": 10.0})["passed"] is True


def test_backtest_engine_reduces_position_near_gap_ceiling():
    engine = BacktestEngine(None, BacktestConfig(
        reduced_position_gap=0.02,
        high_gap_position_multiplier=0.75,
    ))

    assert engine._entry_gap_position_multiplier(0.0199) == 1.0
    assert engine._entry_gap_position_multiplier(0.02) == 0.75
    assert engine._entry_gap_position_multiplier(0.03) == 0.75


def test_observation_candidate_buys_only_after_intraday_strength_trigger():
    plan = _plan().copy()
    plan["综合评分"] = 72
    plan["因子_tech_score"] = 85
    plan["因子_stk_sector_resonance_score"] = 65
    plan["原始_amount_ratio"] = 1.2
    dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.1, "high": 10.25, "low": 10.0, "close": 10.2, "pre_close": 10.0
        }
    })
    engine = BacktestEngine(dm, BacktestConfig(slippage=0))

    can_buy, price = engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")

    assert can_buy is True
    assert round(price, 3) == 10.201
    assert engine._last_entry_signal["000001"] == "盘中转强"


def test_observation_candidate_stays_unbought_without_trigger():
    plan = _plan().copy()
    plan["综合评分"] = 72
    plan["因子_tech_score"] = 85
    plan["因子_stk_sector_resonance_score"] = 65
    plan["原始_amount_ratio"] = 1.2
    dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.1, "high": 10.15, "low": 10.0, "close": 10.12, "pre_close": 10.0
        }
    })
    engine = BacktestEngine(dm, BacktestConfig(slippage=0))

    can_buy, price = engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")

    assert can_buy is False
    assert price == 0
