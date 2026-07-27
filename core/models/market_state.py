"""Single point-in-time market-state contract shared by rules and models."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Dict, Mapping, Tuple


EMOTION_PHASES = ("freeze", "warm", "active", "boom", "decline")
EMOTION_PHASE_LABELS = {
    "freeze": "情绪冰点",
    "warm": "情绪回暖",
    "active": "情绪活跃",
    "boom": "情绪高潮",
    "decline": "情绪退潮",
}
PHASE_ALLOWED_STRATEGIES = {
    "freeze": ("weak_to_strong", "mainline_leader"),
    "warm": ("first_board_launch", "weak_to_strong"),
    "active": ("mainline_leader", "first_board_launch", "weak_to_strong"),
    "boom": ("mainline_leader", "first_board_launch"),
    "decline": ("weak_to_strong",),
}
PHASE_ACCOUNT_EXPOSURE = {
    "freeze": 0.16,
    "warm": 0.45,
    "active": 0.80,
    "boom": 0.55,
    "decline": 0.12,
}


def _optional_number(context: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in context:
            continue
        try:
            value = float(context.get(key))
        except (TypeError, ValueError):
            continue
        if value == value:
            return value
    return None


def classify_emotion_phase(
    score: Any,
    context: Mapping[str, Any] | None = None,
) -> Tuple[str, Tuple[str, ...]]:
    """Classify the trading cycle without replacing the model's 3-regime state."""
    values = dict(context or {})
    try:
        market_score = float(score)
    except (TypeError, ValueError):
        market_score = 50.0
    score_change = _optional_number(values, "market_score_change")
    cycle_days = _optional_number(values, "cycle_duration", "F1_cycle_duration")
    divergence = _optional_number(
        values, "market_emotion_divergence", "F2_market_emotion_divergence",
    )
    limit_up = _optional_number(values, "limit_up_count", "mkt_limit_up_count")
    limit_down = _optional_number(values, "limit_down_count", "mkt_limit_down_count")
    broken_rate = _optional_number(values, "broken_rate", "mkt_broken_rate")
    broken_rate_ratio = (
        broken_rate / 100.0
        if broken_rate is not None and broken_rate > 1.0
        else broken_rate
    )

    reasons = []
    rapid_decline = score_change is not None and score_change <= -8.0
    divergence_decline = divergence is not None and divergence > 30.0 and market_score < 50.0
    board_decline = (
        broken_rate_ratio is not None
        and broken_rate_ratio >= 0.35
        and market_score < 60.0
    )
    if rapid_decline or divergence_decline or board_decline:
        phase = "decline"
        if rapid_decline:
            reasons.append(f"市场分较前日快速回落{score_change:.1f}分")
        if divergence_decline:
            reasons.append(f"大盘与短线情绪背离{divergence:.1f}分")
        if board_decline:
            reasons.append(f"炸板率升至{broken_rate_ratio:.0%}")
    elif (
        market_score < 30.0
        or (
            limit_down is not None
            and limit_up is not None
            and limit_down >= max(limit_up * 1.2, 30.0)
        )
    ):
        phase = "freeze"
        reasons.append(f"市场情绪分仅{market_score:.1f}")
    elif (
        market_score > 70.0
        and cycle_days is not None
        and cycle_days > 5.0
        and limit_up is not None
        and limit_up > 80.0
    ):
        phase = "boom"
        reasons.append(
            f"强周期已持续{int(cycle_days)}天且涨停{int(limit_up)}家"
        )
    elif market_score >= 50.0:
        phase = "active"
        reasons.append(f"市场情绪分{market_score:.1f}，赚钱效应活跃")
    else:
        phase = "warm"
        reasons.append(f"市场情绪分{market_score:.1f}，处于修复回暖区")
    return phase, tuple(reasons)


@lru_cache(maxsize=1)
def _risk_thresholds() -> tuple[float, float]:
    """Load immutable thresholds once per worker/training process."""
    from risk.risk_config import RiskConfig

    risk = RiskConfig.load()
    return float(risk.market_entry_threshold), float(risk.market_strong_threshold)


@dataclass(frozen=True)
class MarketStateSnapshot:
    trade_date: str
    score: float
    regime: str
    method: str
    weak_threshold: float
    strong_threshold: float
    model_status: str = "not_used"
    phase: str = "warm"
    phase_label: str = "情绪回暖"
    phase_reasons: Tuple[str, ...] = ()
    position_scale: float = 0.45
    allowed_strategies: Tuple[str, ...] = ()
    risk_flags: Tuple[str, ...] = ()
    context: Dict[str, Any] | None = None

    @classmethod
    def resolve(
        cls, score: Any, *, trade_date: str = "",
        regime_model: Mapping[str, Any] | None = None,
        weak_threshold: float | None = None,
        strong_threshold: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> "MarketStateSnapshot":
        default_weak, default_strong = _risk_thresholds()
        weak = float(default_weak if weak_threshold is None else weak_threshold)
        strong = float(default_strong if strong_threshold is None else strong_threshold)
        try:
            numeric = float(score)
        except (TypeError, ValueError):
            numeric = (weak + strong) / 2.0
        model = dict(regime_model or {})
        model_regime = ""
        if model.get("status") == "trained":
            from core.models.market_regime import MarketRegimeDetector

            model_regime = MarketRegimeDetector.predict_current(model, numeric)
            method = f"{str(model.get('method') or 'model')}+absolute_score_guard"
            model_status = "trained"
        else:
            method = "risk_thresholds"
            model_status = str(model.get("status") or "not_used")
        # The displayed score and regime must share one contract. A relative
        # regime model may still be useful as a diagnostic, but it must never
        # label an 18-point market as strong (or a 75-point market as weak).
        regime = "strong" if numeric >= strong else "weak" if numeric < weak else "neutral"
        phase_context = dict(context or {})
        if model_regime:
            phase_context["model_regime"] = model_regime
            phase_context["model_regime_disagrees"] = model_regime != regime
        phase, phase_reasons = classify_emotion_phase(numeric, phase_context)
        risk_flags = []
        cycle_days = _optional_number(
            phase_context, "cycle_duration", "F1_cycle_duration",
        )
        divergence = _optional_number(
            phase_context,
            "market_emotion_divergence",
            "F2_market_emotion_divergence",
        )
        echelon = _optional_number(phase_context, "echelon_integrity")
        premium = _optional_number(phase_context, "prev_limit_up_premium")
        positive = _optional_number(phase_context, "prev_limit_up_positive")
        first_board_gap = _optional_number(
            phase_context, "prev_first_board_gap_up",
        )
        if phase == "boom" and cycle_days is not None and cycle_days > 7:
            risk_flags.append("cycle_overheated")
        if divergence is not None and divergence > 40:
            risk_flags.append("market_emotion_divergence")
        if echelon is not None and echelon < 0.60:
            risk_flags.append("echelon_broken")
        if premium is not None and premium < -2.0:
            risk_flags.append("prev_limit_premium_weak")
        if positive is not None and positive < 0.40:
            risk_flags.append("prev_limit_positive_weak")
        if first_board_gap is not None and first_board_gap < 0.40:
            risk_flags.append("first_board_no_premium")
        return cls(
            trade_date=str(trade_date), score=numeric, regime=regime, method=method,
            weak_threshold=weak, strong_threshold=strong, model_status=model_status,
            phase=phase,
            phase_label=EMOTION_PHASE_LABELS[phase],
            phase_reasons=phase_reasons,
            position_scale=PHASE_ACCOUNT_EXPOSURE[phase],
            allowed_strategies=PHASE_ALLOWED_STRATEGIES[phase],
            risk_flags=tuple(risk_flags),
            context=phase_context,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def strategy_position_multiplier(self, strategy_id: str) -> float:
        """Return the phase/risk multiplier applied to a strategy's stock cap."""
        strategy = str(strategy_id or "")
        if strategy and strategy not in self.allowed_strategies:
            return 0.0
        multiplier = {
            "freeze": 1.0,
            "warm": 0.75,
            "active": 1.0,
            "boom": 0.70,
            "decline": 0.40,
        }.get(self.phase, 0.50)
        flags = set(self.risk_flags)
        if "cycle_overheated" in flags and strategy == "mainline_leader":
            multiplier = min(multiplier, 0.60)
        if "market_emotion_divergence" in flags:
            multiplier *= 0.50
        if "echelon_broken" in flags:
            if strategy == "ultra_short_board":
                return 0.0
            if strategy in {"mainline_leader", "first_board_launch"}:
                multiplier *= 0.70
        if "prev_limit_premium_weak" in flags:
            if strategy in {"mainline_leader", "first_board_launch"}:
                return 0.0
            if strategy == "ultra_short_board":
                multiplier *= 0.40
        if "prev_limit_positive_weak" in flags:
            if strategy == "ultra_short_board":
                multiplier *= 0.60
            elif strategy == "first_board_launch":
                multiplier *= 0.75
        if "first_board_no_premium" in flags and strategy == "first_board_launch":
            return 0.0
        return round(max(0.0, min(multiplier, 1.0)), 4)


def classify_market_score(score: Any) -> str:
    return MarketStateSnapshot.resolve(score).regime


__all__ = [
    "EMOTION_PHASES",
    "EMOTION_PHASE_LABELS",
    "PHASE_ALLOWED_STRATEGIES",
    "MarketStateSnapshot",
    "classify_emotion_phase",
    "classify_market_score",
]
