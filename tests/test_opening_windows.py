from datetime import datetime, timedelta

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig
from backtest.minute_entry import ENTRY_ACCELERATION, ENTRY_CONTINUATION, ENTRY_WEAK, MinuteEntryEvaluator
from core.realtime.snapshot_signal_service import SnapshotSignalService
from core.signals.production_rules import entry_parameters, minute_evaluator
from risk.risk_config import RiskConfig


def test_new_defaults_and_old_frozen_config_are_distinct():
    modes = (ENTRY_ACCELERATION, ENTRY_CONTINUATION, ENTRY_WEAK)
    for evaluator in (MinuteEntryEvaluator(), minute_evaluator(RiskConfig())):
        assert [evaluator.opening_window_minutes(m) for m in modes] == [1, 3, 5]
    old = minute_evaluator(RiskConfig.from_dict({}))
    assert [old.opening_window_minutes(m) for m in modes] == [5, 5, 5]
    config = RiskConfig.from_dict(dict(acceleration_opening_minutes=2,
                                     continuation_opening_minutes=4, weak_opening_minutes=3))
    projected = BacktestConfig.from_risk_config(config)
    assert [projected.acceleration_opening_minutes, projected.continuation_opening_minutes,
            projected.weak_opening_minutes] == [2, 4, 3]
    params = entry_parameters(minute_evaluator(config))
    assert [params[k] for k in ("acceleration_opening_minutes", "continuation_opening_minutes",
                               "weak_opening_minutes")] == [2, 4, 3]


@pytest.mark.parametrize("field", ["acceleration_opening_minutes", "continuation_opening_minutes", "weak_opening_minutes"])
@pytest.mark.parametrize("bad", [True, 0, 6, 2.5])
def test_invalid_opening_window_rejected(field, bad):
    with pytest.raises(ValueError, match=field):
        MinuteEntryEvaluator(**{field: bad})


@pytest.mark.parametrize("mode,field,count,gap", [
    (ENTRY_ACCELERATION, "acceleration_opening_minutes", 1, .06),
    (ENTRY_CONTINUATION, "continuation_opening_minutes", 3, .02),
    (ENTRY_WEAK, "weak_opening_minutes", 5, -.01),
    (ENTRY_ACCELERATION, "acceleration_opening_minutes", 2, .06),
    (ENTRY_CONTINUATION, "continuation_opening_minutes", 4, .02),
    (ENTRY_WEAK, "weak_opening_minutes", 2, -.01),
])
def test_snapshot_requires_exact_configured_complete_window(mode, field, count, gap):
    now = datetime(2026, 10, 26, 9, 31 + count, 3)
    evaluator = MinuteEntryEvaluator(**{field: count})
    service = SnapshotSignalService(clock=lambda: now)
    bars = pd.DataFrame([dict(time=f"09:{m}:00", open=10, high=10.1, low=9.8,
                              close=10, volume=10000, amount=100000) for m in range(31, 36)])
    ticks = pd.DataFrame([dict(code="000001", date="20261026", time=at.strftime("%H:%M:%S"),
        received_at=at.isoformat(), last_price=10.2, ask1=10.21, open_price=10*(1+gap),
        pre_close=10, vol_hand=20000+i*10, amount_yuan=20000000+i*10200,
        delta_volume=10, source="eltdx_batch", quality_ok=True)
        for i, at in enumerate((now-timedelta(seconds=3), now))])
    args = dict(code="000001", trade_date="20261026", mode=mode, snapshots=ticks,
        prev_close=10, open_gap=gap, sector_sync=lambda at: True, is_leader=True,
        limit_price=11, evaluator=evaluator, previous_amount=100000000,
        expected_amount_fraction=lambda at: .2)
    decision, fallback = service.evaluate_primary(minute_bars=bars, **args)
    assert not fallback
    checks = decision.condition_checks["latest"]["checks"]
    assert checks["opening_minutes_required"] == count
    assert checks["opening_minutes_available"] == count
    assert decision.data_status != "snapshot_structure_pending"
    # Later bars cannot fill an absent opening minute.
    decision, fallback = service.evaluate_primary(minute_bars=bars.iloc[1:], **args)
    assert not fallback
    assert decision.data_status == "snapshot_structure_pending"
