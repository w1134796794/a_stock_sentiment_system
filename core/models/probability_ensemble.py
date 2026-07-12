"""Blend independent candidate-probability anchors without hiding missing models."""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional


DEFAULT_WEIGHTS = {
    "lightgbm_meta": 0.40,
    "ic_ir_percentile": 0.35,
    "similar_history": 0.25,
}


def _probability(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def blend_candidate_probability(
    *,
    lightgbm_meta: Any = None,
    ic_ir_percentile: Any = None,
    similar_history: Any = None,
    weights: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Blend available anchors and redistribute unavailable model weights.

    IC/IR contributes a relative percentile anchor, while the other two inputs
    are calibrated event probabilities. The component breakdown is persisted so
    the page can explain why a fallback probability changed.
    """
    configured = dict(DEFAULT_WEIGHTS)
    configured.update(dict(weights or {}))
    values = {
        "lightgbm_meta": _probability(lightgbm_meta),
        "ic_ir_percentile": _probability(ic_ir_percentile),
        "similar_history": _probability(similar_history),
    }
    available = {
        key: value
        for key, value in values.items()
        if value is not None and float(configured.get(key, 0.0)) > 0.0
    }
    total_weight = sum(float(configured[key]) for key in available)
    if not available or total_weight <= 0.0:
        return {
            "available": False,
            "probability": None,
            "components": values,
            "effective_weights": {},
            "mode": "unavailable",
        }
    effective = {
        key: float(configured[key]) / total_weight
        for key in available
    }
    probability = sum(available[key] * effective[key] for key in available)
    return {
        "available": True,
        "probability": float(probability),
        "components": values,
        "effective_weights": effective,
        "mode": (
            "meta_icir_similarity"
            if "lightgbm_meta" in available
            else "icir_similarity_fallback"
        ),
    }


__all__ = ["DEFAULT_WEIGHTS", "blend_candidate_probability"]
