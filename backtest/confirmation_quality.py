"""Causal entry confirmation shared by production and research evaluators."""
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
    volume_lookback: int = 3
    min_volume_ratio: float = 1.2
    pullback_max_volume_ratio: float = 0.8
    max_vwap_distance: float = 0.02
    max_breakout_extension: float = 0.015

    def __post_init__(self):
        if self.mode not in {"legacy", "close_breakout", "sustain", "pullback", "volume_price"}:
            raise ValueError("Unknown confirmation policy")
        if any(type(x) is not int or x < 1 for x in (self.sustain_minutes, self.pullback_window)):
            raise ValueError("Confirmation windows must be positive integers")
        if not math.isfinite(self.pullback_tolerance) or not 0 <= self.pullback_tolerance <= 0.01:
            raise ValueError("Invalid pullback tolerance")
        if type(self.volume_lookback) is not int or not 1 <= self.volume_lookback <= 20:
            raise ValueError("Volume lookback must be an integer in [1, 20]")
        for name, low, high in (("min_volume_ratio", 1.0, 5.0),
                                ("pullback_max_volume_ratio", 0.01, 1.0),
                                ("max_vwap_distance", 0.001, 0.10),
                                ("max_breakout_extension", 0.001, 0.05)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid {name}")
        if self.mode == "volume_price" and not 2 <= self.sustain_minutes <= 10:
            raise ValueError("Volume-price confirmation needs 2 to 10 completed closes")
        if self.mode == "volume_price" and not 2 <= self.pullback_window <= 20:
            raise ValueError("Pullback window must be in [2, 20]")


def policy_from_config(config):
    frozen = getattr(config, "entry_confirmation_policy", None)
    if frozen is not None:
        return ConfirmationPolicy(**frozen)
    return ConfirmationPolicy(**{
        "mode": getattr(config, "entry_confirmation_mode", "legacy"),
        "sustain_minutes": getattr(config, "entry_sustain_minutes", 2),
        "pullback_window": getattr(config, "entry_pullback_window", 5),
        "pullback_tolerance": getattr(config, "entry_pullback_tolerance", 0.002),
        "volume_lookback": getattr(config, "entry_volume_lookback", 3),
        "min_volume_ratio": getattr(config, "entry_min_local_volume_ratio", 1.2),
        "pullback_max_volume_ratio": getattr(config, "entry_pullback_max_volume_ratio", 0.8),
        "max_vwap_distance": getattr(config, "entry_max_vwap_distance", 0.02),
        "max_breakout_extension": getattr(config, "entry_max_breakout_extension", 0.015),
    })


def price_quality_reason(price, vwap, level, policy):
    if policy.mode != "volume_price":
        return ""
    if any(not math.isfinite(value) or value <= 0 for value in (price, vwap, level)):
        return "缺少有效确认价、VWAP或突破关键价"
    if price < level or price < vwap:
        return "价格未守住确认关键价和VWAP"
    if price / vwap - 1 > policy.max_vwap_distance + 1e-10:
        return "距离VWAP过远，不追价"
    if price / level - 1 > policy.max_breakout_extension + 1e-10:
        return "距离突破关键价过远，等待回踩"
    return ""


def _volume_value(row):
    return float(row.get("volume_shares", row["volume"]))


def _local_volume_ratio(data, index, window):
    if index < window:
        return None
    rows = data.iloc[index-window:index+1]
    volumes = [_volume_value(row) for _, row in rows.iterrows()]
    if any(not math.isfinite(value) or value <= 0 for value in volumes):
        return None
    times = [datetime.strptime(str(value), "%H:%M:%S") for value in rows["time"]]
    if any((after - before).total_seconds() != 60 for before, after in zip(times, times[1:])):
        return None
    return volumes[-1] / (sum(volumes[:-1]) / window)


def _volume_price_index(data, index, *, level, protection, deadline, policy, sector_sync):
    initial = data.iloc[index]
    if protection > 0 and float(initial.low) < protection:
        return index, "cancelled", "突破分钟跌破结构保护价"
    ratio = _local_volume_ratio(data, index, policy.volume_lookback)
    if ratio is None:
        return index, "data_insufficient", "缺少连续有效分钟成交量，无法判断局部放量"
    denial = price_quality_reason(float(initial.close), float(initial.vwap), level, policy)
    if denial:
        return index, "quality_rejected", denial
    expanded = ratio >= policy.min_volume_ratio
    sustain_alive = expanded
    touched = False
    previous = str(initial.time)
    for offset in range(1, max(policy.pullback_window, policy.sustain_minutes - 1) + 1):
        at = index + offset
        if at >= len(data) or str(data.iloc[at].time) > deadline:
            return index, "quality_pending", "等待放量突破后连续收盘站稳，或缩量回踩再突破"
        row = data.iloc[at]
        elapsed = (datetime.strptime(str(row.time), "%H:%M:%S")
                   - datetime.strptime(previous, "%H:%M:%S")).total_seconds()
        if elapsed != 60:
            status = "quality_rejected" if previous == "11:30:00" else "data_insufficient"
            return at, status, "确认窗口不连续，不跨缺失分钟或午间休市累计"
        previous = str(row.time)
        if (any(not math.isfinite(float(row[key])) or float(row[key]) <= 0
                for key in ("close", "low", "high", "vwap"))
                or not math.isfinite(_volume_value(row)) or _volume_value(row) <= 0):
            return at, "data_insufficient", "确认窗口缺少有效价格或成交量"
        if protection > 0 and float(row.low) < protection:
            return at, "cancelled", "等待确认期间跌破结构保护价"
        if float(row.close) < level * (1 - policy.pullback_tolerance) or float(row.close) < float(row.vwap):
            return at, "quality_rejected", "突破后失守关键价或VWAP，承接未确认"
        held = float(row.close) >= level
        sustain_alive = sustain_alive and held
        sustained = sustain_alive and offset + 1 >= policy.sustain_minutes
        local_ratio = _local_volume_ratio(data, at, policy.volume_lookback)
        rebreak = (offset <= policy.pullback_window and touched and held
                   and float(row.close) > float(data.iloc[at-1].high)
                   and local_ratio is not None and local_ratio >= policy.min_volume_ratio)
        if sustained or rebreak:
            denial = price_quality_reason(float(row.close), float(row.vwap), level, policy)
            if denial:
                return at, "quality_rejected", denial
            state = sector_sync(str(row.time)) if sector_sync else None
            if state is None:
                return at, "data_insufficient", "最终确认时点缺少板块证据"
            if state is not True:
                return at, "quality_rejected", "最终确认时点板块未同步"
            reason = (f"局部放量{ratio:.2f}倍，连续{policy.sustain_minutes}根分钟收盘站稳" if sustained else
                      f"缩量回踩关键价后再放量{local_ratio:.2f}倍突破")
            return at, "ready", reason
        # A retest and rebreak must be different completed observations.
        if offset <= policy.pullback_window and (
            level * (1 - policy.pullback_tolerance) <= float(row.low) <= level * (1 + policy.pullback_tolerance)
            and _volume_value(row) <= _volume_value(initial) * policy.pullback_max_volume_ratio
        ):
            touched = True
            sustain_alive = False
    return index, "quality_rejected", "确认窗口内未形成放量站稳或缩量回踩再突破"


def confirmation_index(data, index, *, level, protection, deadline, policy, sector_sync=None):
    """Return (index, status, reason), using only bars through that index."""
    if policy.mode == "legacy":
        return index, "ready", ""
    if not math.isfinite(level) or level <= 0:
        return index, "data_insufficient", "缺少突破关键价"
    initial = data.iloc[index]
    if float(initial.close) <= level or float(initial.close) < float(initial.vwap):
        return index, "quality_rejected", "最高价突破但分钟收盘未站上关键价和均价线"
    if policy.mode == "volume_price":
        return _volume_price_index(data, index, level=level, protection=protection,
                                   deadline=deadline, policy=policy, sector_sync=sector_sync)
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
