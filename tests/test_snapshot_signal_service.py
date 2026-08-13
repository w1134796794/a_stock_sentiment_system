from datetime import datetime, timedelta

import pandas as pd

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
            "time": (start + timedelta(seconds=index * 3)).strftime("%H:%M:%S"),
            "last_price": 10.05 + index * 0.01,
            "ask1": 10.06 + index * 0.01,
            "delta_volume": 10,
        }
        for index in range(size)
    ])


def test_snapshot_signal_waits_for_next_tick_then_fills():
    service = SnapshotSignalService()
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
        "minute_decision": EntryDecision("observing"),
    }

    first = service.evaluate(snapshots=_ticks(2), **common)
    second = service.evaluate(snapshots=_ticks(3), **common)

    assert first.status == "observing"
    assert first.data_status == "snapshot_triggered"
    assert second.status == "filled"
    assert second.entry_time == "09:35:12"


def test_snapshot_signal_does_not_confirm_without_sector_sync():
    service = SnapshotSignalService()
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
        minute_decision=EntryDecision("observing"),
    )

    assert result.status == "observing"
    assert "板块同步走强" in result.reason
