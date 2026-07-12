"""Account-size presets shared by backtests, reports and the review assistant."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class CapitalPreset:
    key: str
    label: str
    capital: float
    max_positions: int
    max_position_per_stock: float
    max_total_position: float
    fixed_risk_per_trade: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PRESETS = (
    CapitalPreset("50k", "5万账户", 50_000.0, 3, 0.35, 0.85, 0.0075),
    CapitalPreset("100k", "10万账户", 100_000.0, 4, 0.30, 0.85, 0.0065),
    CapitalPreset("200k", "20万账户", 200_000.0, 5, 0.25, 0.80, 0.0055),
    CapitalPreset("standard", "标准账户", 1_000_000.0, 8, 0.20, 0.80, 0.0050),
)


def resolve_capital_preset(capital: Any) -> CapitalPreset:
    try:
        amount = max(float(capital), 0.0)
    except (TypeError, ValueError):
        amount = 100_000.0
    if amount <= 75_000:
        return PRESETS[0]
    if amount <= 150_000:
        return PRESETS[1]
    if amount <= 350_000:
        return PRESETS[2]
    return PRESETS[3]


def apply_capital_preset(config: Any, capital: Any) -> CapitalPreset:
    preset = resolve_capital_preset(capital)
    config.initial_capital = float(capital or preset.capital)
    config.max_positions = preset.max_positions
    config.max_position_per_stock = preset.max_position_per_stock
    config.max_total_position = preset.max_total_position
    config.fixed_risk_per_trade = preset.fixed_risk_per_trade
    config.kelly_max_position = min(
        float(getattr(config, "kelly_max_position", preset.max_position_per_stock)),
        preset.max_position_per_stock,
    )
    return preset


def capital_presets_payload() -> list[Dict[str, Any]]:
    return [preset.to_dict() for preset in PRESETS[:3]]


__all__ = [
    "CapitalPreset", "PRESETS", "apply_capital_preset",
    "capital_presets_payload", "resolve_capital_preset",
]
