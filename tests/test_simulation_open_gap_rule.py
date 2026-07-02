import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import normalize_minute_bars
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
    low_engine = BacktestEngine(low_dm, BacktestConfig(entry_mode="fixed_gap"))
    can_buy, price = low_engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")
    assert can_buy is False
    assert price == 0

    flat_dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.0, "high": 10.4, "low": 9.9, "close": 10.2, "pre_close": 10.0
        }
    })
    flat_engine = BacktestEngine(flat_dm, BacktestConfig(entry_mode="fixed_gap"))
    can_buy, price = flat_engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")
    assert can_buy is False
    assert price == 0

    high_dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 10.1, "high": 10.4, "low": 10.0, "close": 10.2, "pre_close": 10.0
        }
    })
    high_engine = BacktestEngine(high_dm, BacktestConfig(entry_mode="fixed_gap"))
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
        entry_mode="fixed_gap",
    ))

    assert engine._entry_gap_position_multiplier(0.0199) == 1.0
    assert engine._entry_gap_position_multiplier(0.02) == 0.75
    assert engine._entry_gap_position_multiplier(0.03) == 0.75


def test_daily_amount_is_normalized_from_tushare_thousand_yuan():
    assert BacktestEngine._daily_amount_yuan({"amount": 1234.5}) == 1_234_500
    assert BacktestEngine._daily_amount_yuan({"amount_yuan": 987_654, "amount": 1}) == 987_654


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
    engine = BacktestEngine(dm, BacktestConfig(slippage=0, entry_mode="fixed_gap"))

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
    engine = BacktestEngine(dm, BacktestConfig(slippage=0, entry_mode="fixed_gap"))

    can_buy, price = engine._check_buy_conditions(plan, "20260612", "000001", "平安银行")

    assert can_buy is False
    assert price == 0


def test_backtest_engine_weak_entry_uses_next_minute_open_not_daily_high():
    plan = _plan().copy()
    plan["原始_amount_ratio_5d"] = 1.2
    plan["因子_stk_sector_resonance_score"] = 70
    dm = FakeDailyDataManager({
        ("000001.SZ", "20260612"): {
            "open": 9.8, "high": 10.8, "low": 9.7, "close": 10.5, "pre_close": 10.0,
        },
    })
    engine = BacktestEngine(dm, BacktestConfig(slippage=0, entry_mode="weak_only"))
    minute = pd.DataFrame([
        {"time": "09:30:00", "open": 9.80, "high": 9.90, "low": 9.75, "close": 9.88, "volume": 1000},
        {"time": "09:31:00", "open": 9.88, "high": 9.92, "low": 9.82, "close": 9.90, "volume": 1000},
        {"time": "09:32:00", "open": 9.90, "high": 9.95, "low": 9.86, "close": 9.93, "volume": 1000},
        {"time": "09:33:00", "open": 9.93, "high": 9.97, "low": 9.90, "close": 9.95, "volume": 1000},
        {"time": "09:34:00", "open": 9.95, "high": 9.99, "low": 9.93, "close": 9.98, "volume": 1000},
        {"time": "09:35:00", "open": 9.98, "high": 10.05, "low": 9.96, "close": 10.03, "volume": 1000},
        {"time": "09:36:00", "open": 10.04, "high": 10.08, "low": 10.02, "close": 10.06, "volume": 1000},
    ])
    engine._minute_frames[("20260612", "000001")] = normalize_minute_bars(minute)

    can_buy, price = engine._check_buy_conditions(
        plan, "20260612", "000001", "平安银行",
    )

    assert can_buy is True
    assert price == 10.04
    assert price != dm.rows[("000001.SZ", "20260612")]["high"]
    assert engine._last_entry_signal["000001"] == "弱转强"
    assert engine._last_entry_meta["000001"]["entry_time"] == "09:36:00"
