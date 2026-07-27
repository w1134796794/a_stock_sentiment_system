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
    assert weak_probe["emotion_phases"] == ["freeze"]
    assert weak_probe["top_n"] == 2
    assert weak_probe["position_cap_pct"] == 8.0
    assert repository.resolve("weak_market_probe")["position_cap_pct"] == 8.0

    alpha = repository.get_profile("short_term_alpha")
    assert alpha["training_scope"] == "near_limit"
    assert alpha["weight_source"] == "lightgbm"
    assert alpha["top_n"] == 5

    default_selection = repository.default_selection()
    assert default_selection[0] == "mainline_leader"
    assert set(default_selection) == {
        "mainline_leader", "weak_to_strong", "first_board_launch",
    }
    assert all(
        repository.get_profile(strategy_id)["scope"] == "production"
        for strategy_id in default_selection
    )
    assert all(
        repository.get_profile(strategy_id)["weight_source"] == "manual"
        for strategy_id in default_selection
    )
    mainline = repository.get_profile("mainline_leader")
    first_board = repository.get_profile("first_board_launch")
    weak_repair = repository.get_profile("weak_to_strong")
    assert mainline["primary"] is True
    assert mainline["weight_profile"] == "mainline_leader"
    assert "acceleration" not in mainline["execution"]["allowed_entry_modes"]
    assert mainline["execution"]["max_positions"] == 1
    assert first_board["training_scope"] == "near_limit"
    assert first_board["weight_profile"] == "first_board_launch"
    assert first_board["execution"]["max_positions"] == 1
    assert weak_repair["weight_profile"] == "weak_to_strong"
    assert weak_repair["execution"]["max_positions"] == 1
    assert repository.get_profile("capital_resonance")["enabled"] is False


def test_production_strategies_use_the_core_factor_contract():
    repository = StrategyProfileRepository()
    first_board = repository.get_profile("first_board_launch")
    weak = repository.get_profile("weak_to_strong")
    mainline = repository.get_profile("mainline_leader")

    first_required = {
        row["factor"]: (row["op"], row["value"])
        for row in first_board["required_filters"]
    }
    assert first_required["limit_progress"] == (">=", 0.95)
    assert first_required["stk_behavior_attention"] == (">=", 55)
    assert first_required["stk_liquidity_percentile"] == (">=", 35)
    assert first_required["mkt_market_score"] == (">=", 35)
    assert first_required["mkt_limit_up_count"] == (">=", 20)

    weak_required = {
        row["factor"]: (row["op"], row["value"])
        for row in weak["required_filters"]
    }
    assert weak_required["stk_behavior_repair"] == (">=", 55)
    assert weak_required["stk_sector_resonance_score"] == (">=", 50)
    assert weak_required["stk_liquidity_percentile"] == (">=", 40)
    assert weak_required["mkt_market_score"] == (">=", 25)

    main_required = {
        row["factor"]: (row["op"], row["value"])
        for row in mainline["required_filters"]
    }
    assert main_required["stk_sector_mainline_score"] == (">=", 55)
    assert main_required["stk_sector_resonance_score"] == (">=", 55)
    assert main_required["stk_relative_strength_sector"] == (">=", 50)
    assert main_required["mkt_market_score"] == (">=", 40)
    assert main_required["mkt_limit_down_count"] == ("<=", 30)
    assert main_required["mkt_broken_rate"] == ("<=", 35)
    main_excluded = {
        row["factor"]: (row["op"], row["value"])
        for row in (
            list(mainline.get("exclusion_filters") or [])
            + list(mainline.get("veto_rules") or [])
        )
    }
    assert main_excluded["stk_behavior_decay"] == (">=", 70)
    assert main_excluded["stk_lhb_crowding_risk"] == (">=", 80)
    main_weights = {
        row["factor"]: row["weight"]
        for row in mainline["ranking_factors"]
    }
    assert main_weights == {
        "stk_sector_mainline_score": 0.15,
        "stk_sector_resonance_score": 0.14,
        "stk_kpl_leader_quality": 0.14,
        "stk_sector_persistence_score": 0.12,
        "stk_relative_strength_sector": 0.12,
        "stk_board_position": 0.11,
        "stk_intraday_seal_quality": 0.10,
        "stk_behavior_acceleration": 0.07,
        "stk_lhb_sector_resonance": 0.05,
    }


def test_production_strategy_edit_cannot_restore_model_runtime(tmp_path):
    base_path = tmp_path / "screening_profiles.yaml"
    strategy_path = tmp_path / "strategy_combinations.yaml"
    _base_profiles(base_path)
    strategy_path.write_text(
        yaml.safe_dump({
            "version": 1,
            "strategies": {
                "mainline_leader": {
                    "name": "主线龙头",
                    "enabled": True,
                    "primary": True,
                    "protected": True,
                    "scope": "production",
                    "base_profile": "default",
                    "stock_pool": "all",
                    "training_scope": "all",
                    "required_filters": [],
                    "exclusion_filters": [],
                    "evidence_rules": [
                        {
                            "name": "资金证据",
                            "factor": "stk_capital_consensus_score",
                            "op": ">=",
                            "value": 50,
                        }
                    ],
                    "veto_rules": [
                        {
                            "name": "衰退否决",
                            "factor": "stk_behavior_decay",
                            "op": ">=",
                            "value": 70,
                        }
                    ],
                    "ranking_factors": [{"factor": "tech_score", "weight": 1.0}],
                    "weight_source": "manual",
                    "market_regimes": ["strong", "neutral", "weak"],
                    "top_n": 3,
                }
            },
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    repository = StrategyProfileRepository(strategy_path, base_path)
    edit_payload = dict(repository.get_profile("mainline_leader") or {})
    edit_payload.pop("scope", None)
    edit_payload.pop("evidence_rules", None)
    edit_payload.pop("veto_rules", None)
    edit_payload["weight_source"] = "lightgbm"

    saved = repository.save("mainline_leader", edit_payload)

    assert saved["scope"] == "production"
    assert saved["weight_source"] == "manual"
    assert saved["evidence_rules"][0]["factor"] == "stk_capital_consensus_score"
    assert saved["veto_rules"][0]["factor"] == "stk_behavior_decay"
