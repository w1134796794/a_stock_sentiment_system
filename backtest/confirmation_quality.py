"""Optional causal confirmation experiments; legacy execution stays unchanged."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConfirmationPolicy:
    mode: str = "legacy"
    sustain_minutes: int = 2
    pullback_window: int = 10
    pullback_tolerance: float = 0.002

    def __post_init__(self):
        if self.mode not in {"legacy", "close_breakout", "sustain", "pullback"}:
            raise ValueError("Unknown confirmation policy")
        if any(type(x) is not int or x < 1 for x in (self.sustain_minutes, self.pullback_window)):
            raise ValueError("Confirmation windows must be positive integers")
        if not math.isfinite(self.pullback_tolerance) or not 0 <= self.pullback_tolerance <= 0.01:
            raise ValueError("Invalid pullback tolerance")


def confirmation_index(data, index, *, level, protection, deadline, policy, sector_sync=None):
    """Return (index, status, reason), using only bars through that index."""
    if policy.mode == "legacy":
        return index, "ready", ""
    if not math.isfinite(level) or level <= 0:
        return index, "data_insufficient", "缺少突破关键价"
    initial = data.iloc[index]
    if float(initial.close) <= level or float(initial.close) < float(initial.vwap):
        return index, "quality_rejected", "最高价突破但分钟收盘未站上关键价和均价线"
    if policy.mode == "close_breakout":
        return index, "ready", "分钟收盘真突破"
    consecutive = 0
    touched = False
    maximum = policy.sustain_minutes if policy.mode == "sustain" else policy.pullback_window
    previous = str(initial.time)
    for offset in range(1, maximum + 1):
        at = index + offset
        if at >= len(data) or str(data.iloc[at].time) > deadline:
            return index, "quality_pending", "尚未完成持续或回踩确认"
        row = data.iloc[at]
        elapsed = (datetime.strptime(str(row.time), "%H:%M:%S")
                   - datetime.strptime(previous, "%H:%M:%S")).total_seconds()
        if elapsed != 60:
            if previous == "11:30:00" and str(row.time) in {"13:00:00", "13:01:00"}:
                return index, "quality_rejected", "午间休市取消待确认，不跨休市累计分钟"
            return index, "data_insufficient", "确认窗口分钟不连续"
        previous = str(row.time)
        if not math.isfinite(float(row.volume)) or float(row.volume) <= 0:
            return at, "quality_rejected", "等待确认分钟没有有效成交量"
        if protection > 0 and float(row.low) < protection:
            return at, "cancelled", "等待确认期间跌破结构保护价"
        held = float(row.close) >= level and float(row.close) >= float(row.vwap)
        if not held:
            return at, "quality_rejected", "突破后未保持关键价和均价线"
        if policy.mode == "sustain":
            consecutive += 1
            ready = consecutive >= policy.sustain_minutes
        else:
            # A touch and a subsequent new breakout must be different completed bars.
            ready = touched and float(row.close) > float(data.iloc[at-1].high)
            touched = touched or (
                float(row.low) <= level * (1 + policy.pullback_tolerance)
                and float(row.low) >= level * (1 - policy.pullback_tolerance))
        if ready:
            state = sector_sync(str(row.time)) if sector_sync else None
            if state is None:
                return at, "data_insufficient", "最终确认时点缺少板块证据"
            if state is not True:
                return at, "quality_rejected", "最终确认时点板块未同步"
            return at, "ready", "连续分钟站稳" if policy.mode == "sustain" else "回踩关键价后再次突破"
    return index, "quality_rejected", "确认窗口内未完成回踩再突破"
