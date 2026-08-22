import pandas as pd

from backtest.minute_entry import EntryDecision
from core.realtime.entry_signal_service import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_WEAK,
    RealtimeEntrySignalService,
    classify_entry_mode,
    entry_mode_text,
)


def test_opening_gap_is_classified_before_minute_confirmation():
    assert classify_entry_mode(9.8, 10.0) == ENTRY_WEAK
    assert classify_entry_mode(10.1, 10.0) == ENTRY_WEAK
    assert classify_entry_mode(10.3, 10.0) == ENTRY_CONTINUATION
    assert classify_entry_mode(10.6, 10.0) == ENTRY_ACCELERATION
    assert classify_entry_mode(0, 10.0) == ""


def test_entry_modes_have_chinese_labels():
    assert entry_mode_text(ENTRY_WEAK) == "弱转强"
    assert entry_mode_text(ENTRY_CONTINUATION) == "强势延续"
    assert entry_mode_text(ENTRY_ACCELERATION) == "高开加速"


def test_disallowed_fast_limit_is_visible_but_never_authorised_as_buy():
    bars = pd.DataFrame([
        {"time": "09:30:00", "open": 18.10, "high": 18.12, "low": 17.90, "close": 18.02, "volume": 1000},
        {"time": "09:31:00", "open": 18.02, "high": 18.36, "low": 18.00, "close": 18.35, "volume": 1200},
        {"time": "09:32:00", "open": 18.35, "high": 18.65, "low": 18.34, "close": 18.65, "volume": 1800},
        {"time": "09:33:00", "open": 18.65, "high": 18.65, "low": 18.65, "close": 18.65, "volume": 500},
    ])

    decision, detected = RealtimeEntrySignalService._disallowed_mode_observation(
        decision=EntryDecision("rejected", "高开加速", "非龙头不参与"),
        mode=ENTRY_ACCELERATION,
        frame=bars,
        limit_price=18.65,
        allowed_text="弱转强、强势延续",
        open_gap=0.0678,
    )

    assert detected is True
    assert decision.status == "signal_unfilled"
    assert decision.signal == "高开加速"
    assert decision.confirm_time == "09:32:00"
    assert "走势已确认" in decision.reason
    assert "不触发买入" in decision.reason


def test_disallowed_mode_without_strength_remains_rejected():
    bars = pd.DataFrame([
        {"time": "09:30:00", "open": 10.60, "high": 10.65, "low": 10.30, "close": 10.35, "volume": 1000},
        {"time": "09:31:00", "open": 10.35, "high": 10.40, "low": 10.20, "close": 10.25, "volume": 900},
    ])

    decision, detected = RealtimeEntrySignalService._disallowed_mode_observation(
        decision=EntryDecision("rejected", "高开加速", "非龙头不参与"),
        mode=ENTRY_ACCELERATION,
        frame=bars,
        limit_price=11.0,
        allowed_text="弱转强、强势延续",
        open_gap=0.06,
    )

    assert detected is False
    assert decision.status == "rejected"
    assert "不执行" in decision.reason
