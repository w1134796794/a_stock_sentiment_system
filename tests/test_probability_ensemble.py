import pytest

from core.models.probability_ensemble import blend_candidate_probability


def test_probability_ensemble_uses_configured_weights():
    result = blend_candidate_probability(
        lightgbm_meta=0.60,
        ic_ir_percentile=0.80,
        similar_history=0.40,
    )

    assert result["mode"] == "meta_icir_similarity"
    assert result["probability"] == pytest.approx(0.62)
    assert sum(result["effective_weights"].values()) == pytest.approx(1.0)


def test_probability_ensemble_redistributes_missing_model_weight():
    result = blend_candidate_probability(
        lightgbm_meta=None,
        ic_ir_percentile=80,
        similar_history=40,
    )

    expected = 0.80 * (0.35 / 0.60) + 0.40 * (0.25 / 0.60)
    assert result["mode"] == "icir_similarity_fallback"
    assert result["probability"] == pytest.approx(expected)
    assert "lightgbm_meta" not in result["effective_weights"]
