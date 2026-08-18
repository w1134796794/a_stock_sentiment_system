"""Resolve a visible, reproducible protection price for every position."""
from __future__ import annotations

from typing import Any, Mapping, Tuple


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "--") else default
    except (TypeError, ValueError):
        return default


def resolve_protection_price(position: Mapping[str, Any]) -> Tuple[float, str]:
    """Prefer a real price structure and fall back to the account risk floor."""
    entry = _number(position.get("entry_price"))
    if entry <= 0:
        return 0.0, "数据不足"
    metadata = position.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}

    candidates = (
        (
            "structural_stop",
            position.get("structural_stop"),
            str(metadata.get("protection_price_source") or "手工结构价"),
        ),
        ("stop_loss_price", metadata.get("stop_loss_price"), "策略结构价"),
        ("confirmation_low", metadata.get("confirmation_low"), "确认K线低点"),
        ("first_5m_low", metadata.get("first_5m_low"), "开盘前5分钟低点"),
        ("opening_low", metadata.get("opening_low"), "开盘结构低点"),
        ("signal_low", metadata.get("signal_low"), "信号结构低点"),
        ("prev_low", metadata.get("prev_low"), "前一交易日低点"),
    )
    valid = [
        (value, label)
        for _, raw, label in candidates
        if 0 < (value := _number(raw)) < entry
    ]
    if valid:
        # The nearest valid support controls risk; distant historical lows are
        # informative but should not silently widen the stop.
        value, label = max(valid, key=lambda item: item[0])
        return round(value, 4), label

    emergency_loss = max(2.0, _number(position.get("emergency_loss_pct"), 6.0))
    return round(entry * (1.0 - emergency_loss / 100.0), 4), "账户风险底线"


__all__ = ["resolve_protection_price"]
