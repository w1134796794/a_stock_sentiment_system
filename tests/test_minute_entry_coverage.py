from __future__ import annotations

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import ENTRY_CONTINUATION, MinuteEntryEvaluator


def _continuation_bars() -> pd.DataFrame:
    rows = []
    prices = [10.20, 10.24, 10.28, 10.30, 10.32, 10.33, 10.46, 10.48]
    for minute, close in enumerate(prices, start=30):
        rows.append({
            "time": f"09:{minute:02d}:00",
            "open": close - 0.01,
            "high": close + (0.06 if minute == 36 else 0.02),
            "low": close - 0.03,
            "close": close,
            "volume": 200_000,
            "amount": close * 200_000,
            "volume_unit": "shares",
            "amount_is_estimated": False,
        })
    return pd.DataFrame(rows)


def test_continuation_uses_strict_minute_confirmation_when_auction_is_missing():
    evaluator = MinuteEntryEvaluator()

    decision = evaluator.evaluate(
        mode=ENTRY_CONTINUATION,
        bars=_continuation_bars(),
        open_gap=0.02,
        prev_close=10.0,
        previous_amount=100_000_000,
        previous_volume=10_000_000,
        auction_amount=0.0,
        auction_volume=0.0,
        sector_sync=lambda _: True,
        expected_amount_fraction=lambda _: 0.08,
        amount_profile_samples=50,
    )

    assert decision.filled
    assert decision.signal == "开盘强势确认"
    assert decision.confirm_time == "09:36:00"
    assert decision.entry_time == "09:37:00"


def test_real_but_weak_auction_is_still_rejected():
    evaluator = MinuteEntryEvaluator()

    decision = evaluator.evaluate(
        mode=ENTRY_CONTINUATION,
        bars=_continuation_bars(),
        open_gap=0.02,
        prev_close=10.0,
        previous_amount=100_000_000,
        previous_volume=10_000_000,
        auction_amount=1_000_000,
        auction_volume=10_000,
        sector_sync=lambda _: True,
        expected_amount_fraction=lambda _: 0.08,
    )

    assert decision.status == "cancelled"
    assert decision.reason == "竞价成交额或竞价量比不足"


def test_entry_signal_is_evaluated_before_portfolio_capacity_gate():
    engine = BacktestEngine(object(), BacktestConfig(max_positions=1, risk_control=True))
    engine.current_positions["000001"] = {
        "market_value": 100_000.0,
        "strategy_id": "other",
        "resonance_sectors": "",
    }
    calls = []

    def check(plan, date, stock_code, stock_name):
        calls.append(stock_code)
        engine._last_entry_gap[stock_code] = 0.01
        engine._last_entry_signal[stock_code] = "弱转强"
        return True, 10.0

    engine._check_buy_conditions = check
    engine._calculate_position_size = lambda plan: 10_000.0
    plan = pd.Series({
        "代码": "000002",
        "名称": "测试股票",
        "模式": "指标筛选/default",
        "策略ID": "test",
        "综合评分": 80.0,
    })

    engine._execute_buy(plan, "20260701")

    assert calls == ["000002"]
    assert any(row["reason_code"] == "account_position_limit" for row in engine.entry_attempts)
    assert "000002" not in engine._last_entry_signal
