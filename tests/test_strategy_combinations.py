from __future__ import annotations

import yaml

from core.screening.strategy_profiles import StrategyProfileRepository
from desktop.runner import RunController


def _base_profiles(path):
    path.write_text(
        yaml.safe_dump({
            "screening_profiles": {
                "default": {
                    "hard_filters": [],
                    "ranking": {
                        "prior_weights": {"tech_score": 0.6, "stk_amount_ratio_5d": 0.4},
                        "top_n": 10,
                    },
                    "lhb_enhancement": {"enabled": True},
                }
            }
        }),
        encoding="utf-8",
    )


def test_strategy_repository_resolves_engine_configuration(tmp_path):
    base_path = tmp_path / "screening_profiles.yaml"
    strategy_path = tmp_path / "strategy_combinations.yaml"
    _base_profiles(base_path)
    repository = StrategyProfileRepository(strategy_path, base_path)

    saved = repository.save("weak_repair", {
        "name": "弱转强",
        "enabled": True,
        "primary": True,
        "base_profile": "default",
        "stock_pool": "liquid",
        "training_scope": "near_limit",
        "required_filters": [
            {"name": "板块共振", "factor": "stk_sector_resonance_score", "op": ">=", "value": 60}
        ],
        "exclusion_filters": [
            {"name": "拥挤排除", "factor": "stk_attention_crowding_risk", "op": ">=", "value": 80}
        ],
        "ranking_factors": [
            {"factor": "tech_score", "weight": 0.4},
            {"factor": "stk_sector_resonance_score", "weight": 0.6},
        ],
        "weight_source": "manual",
        "enhancements": {"lhb": False, "capital_flow": True},
        "market_regimes": ["strong", "neutral"],
        "top_n": 6,
    })

    assert saved["id"] == "weak_repair"
    resolved = repository.resolve("weak_repair")
    assert resolved["strategy_weight_source"] == "manual"
    assert saved["training_scope"] == "near_limit"
    assert resolved["strategy_training_scope"] == "near_limit"
    assert resolved["ranking"]["top_n"] == 6
    assert resolved["ranking"]["weights"]["stk_sector_resonance_score"] == 0.6
    assert resolved["lhb_enhancement"]["enabled"] is False
    assert resolved["enhancements"]["enabled"] == ["capital_flow"]
    assert resolved["allowed_market_regimes"] == ["strong", "neutral"]
    assert resolved["hard_filters"][0]["name"] == "股票池流动性门槛"
    assert resolved["exclusion_filters"][0]["name"] == "拥挤排除"


def test_strategy_selection_rejects_disabled_profile(tmp_path):
    base_path = tmp_path / "screening_profiles.yaml"
    strategy_path = tmp_path / "strategy_combinations.yaml"
    _base_profiles(base_path)
    strategy_path.write_text(
        yaml.safe_dump({
            "version": 1,
            "strategies": {
                "disabled_one": {
                    "name": "已停用",
                    "enabled": False,
                    "base_profile": "default",
                }
            },
        }),
        encoding="utf-8",
    )
    repository = StrategyProfileRepository(strategy_path, base_path)

    try:
        repository.validate_selection(["disabled_one"])
        raised = False
    except ValueError as exc:
        raised = "已停用" in str(exc)
    assert raised


def test_screening_controller_carries_strategy_options():
    controller = RunController("screening")
    controller.options = {
        "strategy_ids": ["default", "momentum_repair"],
        "primary_strategy": "default",
    }

    status = controller.status()

    assert status["options"]["strategy_ids"] == ["default", "momentum_repair"]
    assert status["options"]["primary_strategy"] == "default"


def test_bundled_strategy_templates_are_resolvable():
    repository = StrategyProfileRepository()
    profiles = {row["id"]: row for row in repository.list_profiles()}

    expected = {
        "trend_follow", "first_board_launch", "ultra_short_board",
        "weak_to_strong", "mainline_leader", "capital_resonance",
        "defensive_quality", "weak_market_probe", "short_term_alpha",
    }
    assert expected.issubset(profiles)
    for profile_id in expected:
        resolved = repository.resolve(profile_id)
        assert resolved["ranking"]["weights"]
        assert resolved["ranking"]["top_n"] >= 1

    crowding_rules = repository.get_profile("ultra_short_board")["exclusion_filters"]
    crowding = next(row for row in crowding_rules if row["factor"] == "stk_lhb_crowding_risk")
    assert crowding["op"] == "<="

    weak_probe = repository.get_profile("weak_market_probe")
    assert weak_probe["market_regimes"] == ["weak"]
    assert weak_probe["top_n"] == 2
    assert weak_probe["position_cap_pct"] == 8.0
    assert repository.resolve("weak_market_probe")["position_cap_pct"] == 8.0

    alpha = repository.get_profile("short_term_alpha")
    assert alpha["training_scope"] == "near_limit"
    assert alpha["weight_source"] == "lightgbm"
    assert alpha["top_n"] == 5
