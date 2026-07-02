import pandas as pd

from backtest.minute_entry import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_HYBRID,
    ENTRY_WEAK,
    MinuteEntryEvaluator,
)


def _bars(prices, *, volumes=None):
    volumes = volumes or [1000] * len(prices)
    rows = []
    for offset, (open_price, high, low, close) in enumerate(prices):
        minute = 30 + offset
        rows.append({
            "time": f"09:{minute:02d}:00",
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volumes[offset],
            "amount": close * volumes[offset],
        })
    return pd.DataFrame(rows)


def test_weak_to_strong_fills_at_next_minute_open():
    frame = _bars([
        (9.80, 9.90, 9.75, 9.88),
        (9.88, 9.92, 9.82, 9.90),
        (9.90, 9.95, 9.86, 9.93),
        (9.93, 9.97, 9.90, 9.95),
        (9.95, 9.99, 9.93, 9.98),
        (9.98, 10.05, 9.96, 10.03),
        (10.04, 10.08, 10.02, 10.06),
    ])
    decision = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_WEAK,
        bars=frame,
        open_gap=-0.02,
        prev_close=10.0,
        plan_amount_ratio=1.2,
        sector_sync=lambda _: True,
    )

    assert decision.filled is True
    assert decision.signal == "弱转强"
    assert decision.confirm_time == "09:35:00"
    assert decision.entry_time == "09:36:00"
    assert decision.entry_price == 10.04


def test_weak_to_strong_cancels_after_breaking_opening_low():
    frame = _bars([
        (9.80, 9.90, 9.75, 9.88),
        (9.88, 9.92, 9.82, 9.90),
        (9.90, 9.95, 9.86, 9.93),
        (9.93, 9.97, 9.90, 9.95),
        (9.95, 9.99, 9.93, 9.98),
        (9.76, 9.85, 9.70, 9.72),
        (9.72, 10.08, 9.72, 10.06),
    ])
    decision = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_HYBRID,
        bars=frame,
        open_gap=-0.02,
        prev_close=10.0,
        plan_amount_ratio=1.2,
        sector_sync=lambda _: True,
    )

    assert decision.status == "cancelled"
    assert "跌破" in decision.reason


def test_continuation_requires_auction_volume_and_fills_next_minute():
    frame = _bars([
        (10.20, 10.25, 10.18, 10.22),
        (10.22, 10.28, 10.20, 10.25),
        (10.25, 10.30, 10.22, 10.28),
        (10.28, 10.32, 10.25, 10.30),
        (10.30, 10.35, 10.28, 10.33),
        (10.33, 10.38, 10.30, 10.37),
        (10.36, 10.42, 10.34, 10.40),
    ])
    evaluator = MinuteEntryEvaluator()
    rejected = evaluator.evaluate(
        mode=ENTRY_CONTINUATION,
        bars=frame,
        open_gap=0.02,
        prev_close=10.0,
        previous_volume=1_000_000,
        auction_volume=1_000,
        auction_amount=1_000_000,
        plan_amount_ratio=1.2,
        sector_sync=lambda _: True,
    )
    filled = evaluator.evaluate(
        mode=ENTRY_CONTINUATION,
        bars=frame,
        open_gap=0.02,
        prev_close=10.0,
        previous_volume=1_000_000,
        auction_volume=10_000,
        auction_amount=6_000_000,
        plan_amount_ratio=1.2,
        sector_sync=lambda _: True,
    )

    assert rejected.status == "cancelled"
    assert filled.filled is True
    assert filled.signal == "强势延续"
    assert filled.entry_time == "09:36:00"


def test_near_limit_open_is_signal_unfilled_not_a_buy():
    frame = _bars([
        (10.99, 11.00, 10.99, 11.00),
        (11.00, 11.00, 11.00, 11.00),
        (11.00, 11.00, 11.00, 11.00),
        (11.00, 11.00, 11.00, 11.00),
        (11.00, 11.00, 11.00, 11.00),
        (11.00, 11.00, 11.00, 11.00),
        (11.00, 11.00, 11.00, 11.00),
    ])
    decision = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_ACCELERATION,
        bars=frame,
        open_gap=0.099,
        prev_close=10.0,
        limit_price=11.0,
        is_leader=True,
        sector_sync=lambda _: True,
    )

    assert decision.status == "signal_unfilled"
    assert decision.filled is False
    assert decision.signal == "高开加速"


def test_hybrid_does_not_silently_include_acceleration():
    frame = _bars([
        (10.60, 10.65, 10.55, 10.62),
        (10.62, 10.68, 10.60, 10.66),
        (10.66, 10.70, 10.63, 10.68),
        (10.68, 10.72, 10.65, 10.70),
        (10.70, 10.74, 10.68, 10.72),
        (10.72, 10.80, 10.70, 10.78),
        (10.79, 10.85, 10.76, 10.82),
    ])
    decision = MinuteEntryEvaluator().evaluate(
        mode=ENTRY_HYBRID,
        bars=frame,
        open_gap=0.06,
        prev_close=10.0,
        limit_price=11.0,
        is_leader=True,
        sector_sync=lambda _: True,
    )

    assert decision.status == "rejected"
    assert "高开加速模式" in decision.reason
