from datetime import datetime, timedelta

import pandas as pd
import pytest

from backtest.minute_entry import ENTRY_WEAK, EntryDecision
from core.realtime.snapshot_signal_service import SnapshotSignalService


def _minutes():
    return pd.DataFrame([
        {
            "time": f"09:{30 + index:02d}:00",
            "open": 9.82 + index * 0.02,
            "high": 9.84 + index * 0.02,
            "low": 9.80 + index * 0.02,
            "close": 9.83 + index * 0.02,
            "volume": 100,
            "amount": (9.83 + index * 0.02) * 100,
        }
        for index in range(6)
    ])


def _ticks(size=3, start=datetime(2026, 8, 13, 9, 35, 6)):
    return pd.DataFrame([
        {
            "date": start.strftime("%Y%m%d"),
            "time": (start + timedelta(seconds=index * 3)).strftime("%H:%M:%S"),
            "received_at": (start + timedelta(seconds=index * 3)).isoformat(),
            "last_price": 10.05 + index * 0.01,
            "ask1": 10.06 + index * 0.01,
            "delta_volume": 10,
            "quality_ok": True,
        }
        for index in range(size)
    ])


def test_snapshot_signal_waits_for_next_tick_then_fills():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 9, 35, 12))
    common = {
        "code": "000001",
        "trade_date": "20260813",
        "mode": ENTRY_WEAK,
        "minute_bars": _minutes(),
        "prev_close": 10.0,
        "open_gap": -0.02,
        "sector_confirmed": True,
        "is_leader": False,
        "limit_price": 11.0,
        "minute_decision": EntryDecision("confirmed"),
    }

    first = service.evaluate(snapshots=_ticks(2), **common)
    second = service.evaluate(snapshots=_ticks(3), **common)

    assert first.status == "confirmed"
    assert first.entry_price == 0  # Notification is eligible before the simulated fill.
    assert first.data_status == "snapshot_triggered"
    assert second.status == "filled"
    assert second.entry_time == "09:35:12"


def test_snapshot_gap_cannot_fill_trigger_from_before_collector_downtime():
    clock = [datetime(2026, 8, 13, 9, 35, 9)]
    service = SnapshotSignalService(clock=lambda: clock[0])
    common = dict(code="000001", trade_date="20260813", mode=ENTRY_WEAK,
                  minute_bars=_minutes(), prev_close=10.0, open_gap=-0.02,
                  sector_confirmed=True, is_leader=False, limit_price=11.0,
                  minute_decision=EntryDecision("confirmed"))
    assert service.evaluate(snapshots=_ticks(2), **common).data_status == "snapshot_triggered"
    clock[0] = datetime(2026, 8, 13, 9, 41, 3)
    resumed = pd.concat([_ticks(2), _ticks(2, datetime(2026, 8, 13, 9, 41))], ignore_index=True)
    result = service.evaluate(snapshots=resumed, **common)
    assert result.data_status == "snapshot_triggered"
    assert result.confirm_time == "09:41:03"
    assert result.status != "filled"
    clock[0] += timedelta(seconds=3)
    fresh = pd.concat([resumed, _ticks(1, clock[0])], ignore_index=True)
    result = service.evaluate(snapshots=fresh, **common)
    assert result.status == "filled"
    assert result.confirm_time == "09:41:03"


def test_snapshot_signal_does_not_confirm_without_sector_sync():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 9, 35, 12))
    result = service.evaluate(
        code="000001",
        trade_date="20260813",
        mode=ENTRY_WEAK,
        minute_bars=_minutes(),
        snapshots=_ticks(3),
        prev_close=10.0,
        open_gap=-0.02,
        sector_confirmed=False,
        is_leader=False,
        limit_price=11.0,
        minute_decision=EntryDecision("confirmed"),
    )

    assert result.status == "observing"
    assert "板块同步走强" in result.reason


def test_snapshot_signal_rejects_stale_ticks():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 9, 36, 0))
    result = service.evaluate(
        code="000001",
        trade_date="20260813",
        mode=ENTRY_WEAK,
        minute_bars=_minutes(),
        snapshots=_ticks(3),
        prev_close=10.0,
        open_gap=-0.02,
        sector_confirmed=True,
        is_leader=False,
        limit_price=11.0,
        minute_decision=EntryDecision("confirmed"),
    )

    assert result.status == "observing"
    assert result.data_status == "snapshot_stale"


@pytest.mark.parametrize("deadline,expected", [("10:30:00", "snapshot_triggered"), ("10:00:00", "cancelled")])
def test_snapshot_uses_strategy_deadline_after_ten(deadline, expected):
    now = datetime(2026, 8, 13, 10, 10, 12)
    service = SnapshotSignalService(clock=lambda: now)
    result = service.evaluate(
        code="000001", trade_date="20260813", mode=ENTRY_WEAK,
        minute_bars=_minutes(), snapshots=_ticks(3, now - timedelta(seconds=6)),
        prev_close=10.0, open_gap=-0.02, sector_confirmed=True,
        is_leader=False, limit_price=11.0, minute_decision=EntryDecision("confirmed"),
        confirmation_deadline=deadline,
    )
    if expected == "cancelled":
        assert result.status == expected
        assert "10:00" in result.reason
    else:
        assert result.data_status == expected


def test_snapshot_deadline_does_not_relax_freshness():
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 10, 11))
    result = service.evaluate(
        code="000001", trade_date="20260813", mode=ENTRY_WEAK,
        minute_bars=_minutes(), snapshots=_ticks(3, datetime(2026, 8, 13, 10, 10)),
        prev_close=10.0, open_gap=-0.02, sector_confirmed=True,
        is_leader=False, limit_price=11.0, minute_decision=EntryDecision("confirmed"),
        confirmation_deadline="10:30:00",
    )
    assert result.data_status == "snapshot_stale"


@pytest.mark.parametrize("low_time,cancelled", [("09:30:03", False), ("09:35:03", True)])
def test_opening_low_break_is_only_checked_after_structure_forms(low_time, cancelled):
    ticks = _ticks(3)
    early = ticks.iloc[0].copy()
    early["time"] = low_time
    early["received_at"] = "2026-08-13T" + low_time
    early["last_price"] = 9.5
    ticks = pd.concat([pd.DataFrame([early]), ticks], ignore_index=True)
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 9, 35, 12))
    result = service.evaluate(
        code="000001", trade_date="20260813", mode=ENTRY_WEAK,
        minute_bars=_minutes(), snapshots=ticks, prev_close=10.0,
        open_gap=-0.02, sector_confirmed=True, is_leader=False,
        limit_price=11.0, minute_decision=EntryDecision("confirmed"),
    )
    assert (result.status == "cancelled") == cancelled
    if not cancelled:
        assert result.data_status == "snapshot_triggered"


def test_optional_snapshot_columns_do_not_raise():
    ticks = _ticks(3).drop(columns=["ask1", "delta_volume"])
    result = SnapshotSignalService._normalize_ticks(ticks, "20260813")
    assert len(result) == 3
    assert result["ask1"].eq(0).all()
    assert result["delta_volume"].eq(0).all()


@pytest.mark.parametrize('status', ['observing', 'data_insufficient', 'quality_pending', 'quality_rejected', 'signal_unfilled'])
def test_snapshot_cannot_override_common_confirmation_gates(status):
    minute = EntryDecision(status, reason='common gate not passed', data_status='missing_amount_profile')
    service = SnapshotSignalService(clock=lambda: datetime(2026, 8, 13, 9, 35, 12))
    result = service.evaluate(code='000001', trade_date='20260813', mode=ENTRY_WEAK,
        minute_bars=_minutes(), snapshots=_ticks(3), prev_close=10, open_gap=-.02,
        sector_confirmed=True, is_leader=False, limit_price=11, minute_decision=minute)
    assert result is minute
    assert not service._pending
