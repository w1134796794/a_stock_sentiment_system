"""Causal support/reversal confirmation; never inspect bars after the signal."""
from __future__ import annotations

import math

STRUCTURAL_MODES = ("limit_pullback", "limit_reversal")
STRUCTURAL_LABELS = {"limit_pullback": "涨停回踩转强", "limit_reversal": "跌停反包确认"}


def evaluate_structure(evaluator, *, mode, data, structure, deadline, gap,
                       prev_close, limit_price, sector_sync, live):
    from backtest.minute_entry import EntryDecision

    label = STRUCTURAL_LABELS[mode]
    def result(status, reason, time=""):
        return EntryDecision(status, label, reason, time, open_gap_pct=gap)

    anchor = structure.get("support") if mode == "limit_pullback" else structure.get("target")
    values = [anchor, structure.get("atr"), structure.get("protection"), structure.get("reference_close")]
    try:
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in values):
            raise ValueError
    except (ValueError, TypeError):
        return result("data_insufficient", "缺少盘后事件锚点或结构保护价")
    anchor, atr, protection, reference = map(float, values)
    if protection >= anchor:
        return result("data_insufficient", "结构保护价必须低于关键价")
    if mode == "limit_pullback":
        try:
            low, high = float(structure["support_low"]), float(structure["support_high"])
            if not (math.isfinite(low) and math.isfinite(high) and 0 < low <= anchor <= high):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return result("data_insufficient", "支撑区上下界缺失或无效")
    if abs(prev_close/reference-1) > 0.005:
        return result("data_insufficient", "昨收与结构基准不一致，请重新计算候选")
    touched = None
    sector_missing = False
    for index, row in data.iterrows():
        time = str(row["time"])
        if time > deadline:
            break
        close = float(row["close"])
        if close < protection:
            return result("cancelled", "跌破结构保护价，形态失效", time)
        if mode == "limit_pullback" and touched is None:
            if float(row["low"]) <= float(structure["support_high"]) and float(row["high"]) >= float(structure["support_low"]):
                touched = index
            continue
        if index < 5 or (mode == "limit_pullback" and (touched is None or index-touched < 3)):
            continue
        prior = data.iloc[index-3:index]
        if float(prior.volume.mean()) <= 0:
            continue
        held = (prior.close >= (anchor-0.35*atr if mode == "limit_pullback" else anchor)).all()
        raised_low = float(prior.iloc[-1].low) >= float(prior.iloc[0].low)
        volume_ok = float(row.volume) >= float(prior.volume.mean())*1.2
        if not (held and raised_low and close > float(prior.high.max()) and close >= float(row.vwap)
                and close >= anchor and volume_ok):
            continue
        if live and index < len(data)-2:
            continue
        if close-anchor > atr or close-protection > 2.5*atr:
            continue
        if mode == "limit_pullback" and (float(structure.get("resistance", 0))-close) < 1.2*(close-protection):
            continue
        sector = sector_sync(time) if sector_sync else None
        if sector is None:
            sector_missing = True
        if not sector:
            continue
        reason = f"{label}：关键价{anchor:.2f}，放量突破承接高点，结构保护{protection:.2f}"
        following = data.iloc[index+1:]
        if not following.empty:
            import datetime
            elapsed = (datetime.datetime.strptime(str(following.iloc[0].time), "%H:%M:%S")
                       - datetime.datetime.strptime(time, "%H:%M:%S")).total_seconds()
            if elapsed != 60:
                return result("signal_unfilled", "确认后下一分钟缺失，不能假定成交", time)
        decision = evaluator._next_minute_fill(data, index, label, reason, gap,
                                              float(row.volume/prior.volume.mean()), True,
                                              limit_price, unfilled_when_locked=True, live=live)
        if decision.filled and (decision.entry_price <= protection or decision.entry_price > anchor+atr
                                or decision.entry_time > deadline):
            return result("signal_unfilled", "下一分钟价格超出结构买入范围或截止时间", time)
        return decision
    if sector_missing:
        return result("data_insufficient", "结构已出现，板块实时证据不足，继续观察")
    if live and (data.empty or str(data.iloc[-1].time) < deadline):
        return result("observing", "等待回踩承接后放量突破" if mode == "limit_pullback" else "等待反包目标站稳后放量突破")
    return result("cancelled", "超过策略确认截止时间，未形成结构买点")
