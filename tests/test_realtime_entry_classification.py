from core.realtime.entry_signal_service import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_WEAK,
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
