"""Behavior-cycle factors built from point-in-time market evidence."""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping


BEHAVIOR_STATES = (
    "attention",
    "acceleration",
    "divergence",
    "repair",
    "decay",
)

BEHAVIOR_STATE_LABELS = {
    "attention": "注意形成",
    "acceleration": "一致加速",
    "divergence": "分歧释放",
    "repair": "弱转强修复",
    "decay": "拥挤衰退",
    "uncertain": "状态不明确",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clip(value: Any) -> float:
    return max(0.0, min(100.0, _number(value, 50.0)))


def _weighted(parts: list[tuple[Any, float]]) -> float:
    total = sum(max(float(weight), 0.0) for _, weight in parts)
    if total <= 0:
        return 50.0
    return _clip(sum(_clip(value) * max(float(weight), 0.0) for value, weight in parts) / total)


def state_probabilities(scores: Mapping[str, Any], *, temperature: float = 18.0) -> Dict[str, Any]:
    """Turn five independent evidence scores into comparable state probabilities."""
    values = {state: _clip(scores.get(state, 50.0)) for state in BEHAVIOR_STATES}
    peak = max(values.values()) if values else 50.0
    scale = max(float(temperature), 1.0)
    exp_values = {state: math.exp((value - peak) / scale) for state, value in values.items()}
    denominator = sum(exp_values.values()) or 1.0
    probabilities = {state: exp_values[state] / denominator for state in BEHAVIOR_STATES}
    strongest = max(probabilities, key=probabilities.get)
    strongest_probability = probabilities[strongest]
    dominant = (
        strongest
        if values[strongest] >= 60.0 and strongest_probability >= 0.35
        else "uncertain"
    )
    return {
        "scores": {state: round(values[state], 4) for state in BEHAVIOR_STATES},
        "probabilities": {state: round(probabilities[state] * 100.0, 4) for state in BEHAVIOR_STATES},
        "dominant_state": dominant,
        "dominant_label": BEHAVIOR_STATE_LABELS[dominant],
        "dominant_probability": round(strongest_probability * 100.0, 4),
    }


def sector_behavior_cycle(
    *,
    pct_chg: Any,
    previous_pct_chg: Any,
    amount_ratio: Any,
    current_rank_percentile: Any,
    previous_rank_percentile: Any,
    persistence_score: Any,
    flow_score: Any = 50.0,
    positive_streak: Any = 0,
    breadth_acceleration_score: Any = None,
) -> Dict[str, Any]:
    """Build sector behavior evidence without using future observations."""
    current = _number(pct_chg)
    previous = _number(previous_pct_chg)
    ratio = max(_number(amount_ratio, 1.0), 0.0)
    rank_now = max(0.0, min(1.0, _number(current_rank_percentile, 0.5)))
    rank_previous = max(0.0, min(1.0, _number(previous_rank_percentile, 0.5)))
    rank_improvement = _clip(50.0 + (rank_now - rank_previous) * 100.0)
    momentum_acceleration = _clip(50.0 + (current - previous) * 8.0)
    amount_surprise = _clip((ratio - 0.5) / 2.0 * 100.0)
    amount_target = _clip(100.0 - abs(ratio - 1.35) / 1.35 * 100.0)
    amount_overheat = _clip((ratio - 1.5) / 1.5 * 100.0)
    first_activation = _clip(50.0 + current * 7.0 - max(previous, 0.0) * 4.0)
    if current > 0 >= previous:
        first_activation = max(first_activation, 90.0)
    if previous < 0 < current:
        reversal = 95.0
    elif previous <= 0 and current > previous:
        reversal = _clip(45.0 + (current - previous) * 6.0)
    else:
        reversal = min(_clip(35.0 + max(current - previous, 0.0) * 3.0), 48.0)
    streak = max(int(_number(positive_streak)), 0)
    fresh_stage = {0: 55.0, 1: 100.0, 2: 90.0, 3: 75.0, 4: 55.0}.get(streak, 35.0)
    breadth = _clip(breadth_acceleration_score) if breadth_acceleration_score is not None else 50.0
    persistence = _clip(persistence_score)
    flow = _clip(flow_score)

    evidence = {
        "attention": _weighted([
            (first_activation, 0.30), (rank_improvement, 0.25),
            (amount_surprise, 0.20), (momentum_acceleration, 0.15), (breadth, 0.10),
        ]),
        "acceleration": _weighted([
            (_clip(50.0 + current * 7.0), 0.25), (persistence, 0.20),
            (amount_target, 0.15), (breadth, 0.15), (flow, 0.10), (fresh_stage, 0.15),
        ]),
        "divergence": _weighted([
            (100.0 - momentum_acceleration, 0.25), (amount_overheat, 0.20),
            (100.0 - rank_improvement, 0.20), (100.0 - persistence, 0.15),
            (100.0 - flow, 0.10), (100.0 - breadth, 0.10),
        ]),
        "repair": _weighted([
            (reversal, 0.35), (rank_improvement, 0.20), (flow, 0.15),
            (amount_target, 0.15), (breadth, 0.15),
        ]),
        "decay": _weighted([
            (100.0 - momentum_acceleration, 0.25), (100.0 - rank_improvement, 0.20),
            (100.0 - flow, 0.20), (100.0 - amount_surprise, 0.15),
            (100.0 - fresh_stage, 0.20),
        ]),
    }
    if not (previous < 0 < current):
        evidence["repair"] = min(evidence["repair"], 48.0)
    result = state_probabilities(evidence)
    result["atomic"] = {
        "first_activation": round(first_activation, 4),
        "rank_improvement": round(rank_improvement, 4),
        "momentum_acceleration": round(momentum_acceleration, 4),
        "amount_surprise": round(amount_surprise, 4),
        "amount_target": round(amount_target, 4),
        "breadth_acceleration": round(breadth, 4),
        "reversal": round(reversal, 4),
        "fresh_stage": round(fresh_stage, 4),
    }
    result["data_completeness"] = 100.0 if breadth_acceleration_score is not None else 87.5
    return result


def stock_behavior_cycle(
    *,
    open_gap_pct: Any,
    close_pct_chg: Any,
    amount_ratio: Any,
    amount_ratio_score: Any,
    relative_strength_score: Any,
    seal_quality_score: Any,
    reseal_resilience_score: Any,
    crowding_safety_score: Any,
    late_seal_safety_score: Any,
    board_score: Any,
    sector_states: Mapping[str, Any],
) -> Dict[str, Any]:
    """Combine stock micro-behavior with its sector's behavior state."""
    gap = _number(open_gap_pct)
    close_change = _number(close_pct_chg)
    amount_ratio_value = max(_number(amount_ratio, 1.0), 0.0)
    amount_target = _clip(amount_ratio_score)
    amount_overheat = _clip((amount_ratio_value - 1.5) / 1.5 * 100.0)
    relative = _clip(relative_strength_score)
    seal = _clip(seal_quality_score)
    reseal = _clip(reseal_resilience_score)
    crowding = _clip(crowding_safety_score)
    late_seal = _clip(late_seal_safety_score)
    board = _clip(board_score)
    sector = {state: _clip(sector_states.get(state, 50.0)) for state in BEHAVIOR_STATES}
    if gap < 0 < close_change:
        repair_quality = 95.0
    elif gap <= 1.0 and close_change > max(gap + 1.0, 0.0):
        repair_quality = _clip(55.0 + (close_change - gap) * 4.0)
    else:
        repair_quality = min(_clip(25.0 + max(close_change - gap, 0.0) * 2.0), 45.0)
    divergence_resilience = _weighted([
        (relative, 0.35), (amount_target, 0.20), (reseal, 0.20),
        (sector["divergence"], 0.10), (sector["repair"], 0.15),
    ])

    evidence = {
        "attention": _weighted([
            (sector["attention"], 0.40), (relative, 0.20),
            (amount_target, 0.15), (board, 0.15), (crowding, 0.10),
        ]),
        "acceleration": _weighted([
            (seal, 0.25), (sector["acceleration"], 0.25), (relative, 0.20),
            (amount_target, 0.10), (board, 0.10), (reseal, 0.10),
        ]),
        "divergence": _weighted([
            (100.0 - reseal, 0.25), (amount_overheat, 0.20),
            (sector["divergence"], 0.25), (100.0 - late_seal, 0.15),
            (divergence_resilience, 0.15),
        ]),
        "repair": _weighted([
            (repair_quality, 0.35), (divergence_resilience, 0.25),
            (sector["repair"], 0.25), (amount_target, 0.15),
        ]),
        "decay": _weighted([
            (100.0 - crowding, 0.25), (100.0 - late_seal, 0.20),
            (sector["decay"], 0.25), (100.0 - relative, 0.20),
            (100.0 - reseal, 0.10),
        ]),
    }
    result = state_probabilities(evidence)
    result["atomic"] = {
        "repair_quality": round(repair_quality, 4),
        "divergence_resilience": round(divergence_resilience, 4),
        "reseal_resilience": round(reseal, 4),
        "late_seal_safety": round(late_seal, 4),
    }
    result["data_completeness"] = 100.0
    return result


def intraday_behavior_cycle(
    *,
    entry_mode: str,
    signal_status: str,
    amount_pace: Any,
    sector_confirmed: Any,
    hold_minutes: Any,
    false_break_count: Any,
    pullback_quality: Any,
    active_buy_ratio: Any,
) -> Dict[str, Any]:
    """Map minute confirmation evidence to the same behavior-state vocabulary."""
    confirmed = signal_status in {"confirmed", "filled", "signal_unfilled"}
    cancelled = signal_status in {"cancelled", "rejected"}
    pace = _clip(50.0 + (_number(amount_pace, 1.0) - 1.0) * 45.0)
    sector = 85.0 if bool(sector_confirmed) else 25.0
    hold = _clip(_number(hold_minutes) / 5.0 * 100.0)
    false_breaks = _clip(_number(false_break_count) / 3.0 * 100.0)
    pullback = _clip(_number(pullback_quality, 0.5) * 100.0)
    active_buy = _clip(_number(active_buy_ratio, 0.5) * 100.0)
    confirmation = 95.0 if confirmed else 15.0 if cancelled else 50.0
    weak_mode = str(entry_mode) == "weak_only"
    continuation_mode = str(entry_mode) in {"continuation_only", "acceleration_only"}
    evidence = {
        "attention": _weighted([(pace, 0.35), (sector, 0.30), (active_buy, 0.20), (50.0, 0.15)]),
        "acceleration": _weighted([
            (confirmation, 0.30), (sector, 0.20), (hold, 0.20),
            (active_buy, 0.15), (85.0 if continuation_mode else 45.0, 0.15),
        ]),
        "divergence": _weighted([
            (false_breaks, 0.35), (100.0 - pullback, 0.25),
            (100.0 - sector, 0.20), (pace, 0.10), (50.0, 0.10),
        ]),
        "repair": _weighted([
            (95.0 if weak_mode and confirmed else 35.0, 0.35),
            (pullback, 0.25), (sector, 0.20), (hold, 0.20),
        ]),
        "decay": _weighted([
            (90.0 if cancelled else 20.0, 0.35), (false_breaks, 0.25),
            (100.0 - sector, 0.25), (100.0 - active_buy, 0.15),
        ]),
    }
    result = state_probabilities(evidence)
    result["atomic"] = {
        "amount_pace": round(pace, 4),
        "sector_sync": round(sector, 4),
        "breakout_hold": round(hold, 4),
        "false_break_pressure": round(false_breaks, 4),
        "pullback_quality": round(pullback, 4),
        "active_buy": round(active_buy, 4),
    }
    return result


__all__ = [
    "BEHAVIOR_STATES",
    "BEHAVIOR_STATE_LABELS",
    "intraday_behavior_cycle",
    "sector_behavior_cycle",
    "state_probabilities",
    "stock_behavior_cycle",
]
