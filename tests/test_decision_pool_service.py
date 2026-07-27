import json

from core.portfolio.decision_pool_service import DecisionPoolService


def _profile(strategy_id: str, name: str):
    return {
        "id": strategy_id,
        "name": name,
        "position_cap_pct": 12,
        "execution": {"allowed_entry_modes": ["weak_to_strong", "continuation"]},
    }


def _row(code: str, name: str, *, grade: str = "B", status: str = "usable"):
    return {
        "code": code,
        "name": name,
        "score": 80,
        "rank": 1,
        "confidence_grade": grade,
        "decision_status": status,
        "stop_probability": 30,
        "expected_return_pct": 0.5,
        "position_budget_pct": 10,
        "resonance_sectors": "机器人,自动化",
        "context": {"sector_mainline_score": 72, "sector_resonance_score": 68},
    }


def test_strong_market_deduplicates_three_production_strategies():
    profiles = {
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
        "first_board_launch": _profile("first_board_launch", "首板启动"),
    }
    payloads = {
        "mainline_leader": {"final": [_row("000001", "甲"), _row("000002", "乙")]},
        "weak_to_strong": {"final": [_row("000001", "甲"), _row("000004", "丁")]},
        "first_board_launch": {"final": [_row("000001", "甲"), _row("000003", "丙")]},
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=75)

    assert result["regime"] == "strong"
    assert result["active_strategy_ids"] == ["mainline_leader", "weak_to_strong", "first_board_launch"]
    assert result["hidden_strategy_names"] == []
    assert len(result["rows"]) == 4
    first = next(row for row in result["rows"] if row["code"] == "000001")
    assert first["策略共识显示"] == "3/3"
    assert first["行动分组"] == "重点确认"
    assert first["所属主线"] == "机器人"
    assert first["execution_eligible"] is True
    assert first["allowed_entry_modes"] == ["weak_to_strong", "continuation"]
    assert first["strategy_execution"]["source_strategies"] == [
        "mainline_leader", "weak_to_strong", "first_board_launch",
    ]


def test_security_attributes_cannot_be_promoted_to_the_mainline():
    profiles = {
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
        "first_board_launch": _profile("first_board_launch", "首板启动"),
    }
    row = _row("920088", "科力股份")
    row["resonance_sectors"] = "融资融券,空气能热泵,专精特新"
    payloads = {
        "mainline_leader": {"final": [row]},
        "weak_to_strong": {"final": [row]},
        "first_board_launch": {"final": [row]},
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=75)
    candidate = result["rows"][0]

    assert candidate["所属主线"] == "空气能热泵"
    assert candidate["相关题材"] == ["空气能热泵", "专精特新"]
    assert candidate["证券属性标签"] == ["融资融券"]
    assert candidate["共振板块"] == ["空气能热泵", "专精特新"]


def test_attribute_only_memberships_leave_mainline_unconfirmed():
    profiles = {
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
        "first_board_launch": _profile("first_board_launch", "首板启动"),
    }
    row = _row("000001", "甲")
    row["resonance_sectors"] = "融资融券,深股通"
    payloads = {
        "mainline_leader": {"final": [row]},
        "weak_to_strong": {"final": [row]},
        "first_board_launch": {"final": [row]},
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=75)
    candidate = result["rows"][0]

    assert candidate["所属主线"] == "主线待确认"
    assert candidate["主线确认"] is False
    assert candidate["板块强度"] == 0
    assert candidate["相关题材"] == []
    assert candidate["证券属性标签"] == ["融资融券", "深股通"]


def test_related_theme_below_strength_threshold_is_not_called_mainline():
    profiles = {
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
        "first_board_launch": _profile("first_board_launch", "首板启动"),
    }
    row = _row("000001", "甲")
    row["resonance_sectors"] = "机器人,自动化"
    row["context"] = {"sector_mainline_score": 52, "sector_resonance_score": 50}
    payloads = {
        "mainline_leader": {"final": [row]},
        "weak_to_strong": {"final": [row]},
        "first_board_launch": {"final": [row]},
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=75)
    candidate = result["rows"][0]

    assert candidate["所属主线"] == "主线待确认"
    assert candidate["主线确认"] is False
    assert candidate["相关题材"] == ["机器人", "自动化"]
    assert candidate["板块强度"] == 52
    assert "主线题材待确认" in candidate["一句话结论"]
    assert "主线主线待确认" not in candidate["一句话结论"]


def test_supplied_relative_regime_is_corrected_by_the_absolute_market_score():
    profiles = {
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
    }
    payloads = {
        "mainline_leader": {"final": [_row("000001", "甲")]},
        "weak_to_strong": {"final": [_row("000002", "乙")]},
    }

    result = DecisionPoolService().build(
        payloads, profiles, market_score=18.2, market_regime="strong",
    )

    assert result["regime"] == "weak"
    assert result["supplied_regime"] == "strong"
    assert result["regime_corrected"] is True


def test_emotion_phase_is_the_single_strategy_gate_and_is_exposed_to_ui():
    profiles = {
        strategy_id: _profile(strategy_id, strategy_id)
        for strategy_id in (
            "mainline_leader",
            "weak_to_strong",
            "ultra_short_board",
            "first_board_launch",
            "weak_market_probe",
        )
    }
    payloads = {
        strategy_id: {"final": [_row(f"00000{index}", strategy_id)]}
        for index, strategy_id in enumerate(profiles, start=1)
    }

    result = DecisionPoolService().build(
        payloads,
        profiles,
        market_score=78,
        market_state={
            "phase": "boom",
            "position_scale": 0.55,
            "risk_flags": ["cycle_overheated", "market_emotion_divergence"],
            "phase_reasons": ["强周期已持续7天且涨停95家"],
        },
    )

    assert result["emotion_phase"] == "boom"
    assert result["emotion_phase_label"] == "情绪高潮"
    assert result["market_total_position_cap_pct"] == 55
    assert result["active_strategy_ids"] == ["mainline_leader", "first_board_launch"]
    assert "强周期持续过久，接近退潮窗口" in result["market_risk_labels"]
    assert all(row["情绪阶段"] == "情绪高潮" for row in result["rows"])


def test_model_grade_d_does_not_block_the_rules_only_decision_pool():
    profiles = {"weak_to_strong": _profile("weak_to_strong", "弱转强")}
    payloads = {
        "weak_to_strong": {
            "final": [_row("000001", "甲", grade="D", status="data_insufficient")]
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)

    candidate = result["rows"][0]
    assert result["decision_count"] == 1
    assert candidate["规则等级"] in {"B", "C"}
    assert candidate["行动分组"] in {"重点确认", "盘中观察"}
    assert "模型状态" not in candidate


def test_old_expected_return_field_does_not_control_rule_action():
    profiles = {"weak_to_strong": _profile("weak_to_strong", "弱转强")}
    row = _row("000001", "甲", grade="A")
    row["expected_return_pct"] = 0.29
    payloads = {"weak_to_strong": {"final": [row]}}

    result = DecisionPoolService().build(payloads, profiles, market_score=40)

    candidate = result["rows"][0]
    assert candidate["规则等级"] in {"B", "C"}
    assert candidate["行动分组"] in {"重点确认", "盘中观察"}


def test_fallback_model_metadata_is_ignored_by_production_rules():
    profiles = {"weak_to_strong": _profile("weak_to_strong", "弱转强")}
    payloads = {
        "weak_to_strong": {
            "weight_metadata": {"candidate_model_runtime": "fallback_drift"},
            "final": [_row("000001", "甲", grade="B")],
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)
    row = result["rows"][0]

    assert "模型状态" not in row
    assert row["规则等级"] == "B"
    assert row["行动分组"] == "重点确认"


def test_rule_evidence_controls_action_when_old_model_fields_are_d():
    profiles = {"weak_to_strong": _profile("weak_to_strong", "弱转强")}
    row = _row("000001", "甲", grade="D", status="no_edge")
    row["metrics"] = {
        "stk_sector_persistence_score": 72,
        "stk_capital_flow_consensus": 68,
        "stk_behavior_repair": 75,
    }
    payloads = {
        "weak_to_strong": {
            "weight_metadata": {"candidate_model_runtime": "fallback_drift"},
            "final": [row],
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)
    candidate = result["rows"][0]

    assert "模型状态" not in candidate
    assert candidate["规则等级"] == "B"
    assert candidate["行动分组"] == "重点确认"
    assert "资金共振" in candidate["增强证据"]


def test_model_sample_shortage_does_not_block_complete_rule_evidence():
    profiles = {"weak_to_strong": _profile("weak_to_strong", "弱转强")}
    row = _row("000001", "甲", grade="D", status="data_insufficient")
    row["data_completeness"] = 100
    row["metrics"] = {
        "stk_sector_persistence_score": 72,
        "stk_capital_flow_consensus": 68,
        "stk_behavior_repair": 75,
    }
    payloads = {
        "weak_to_strong": {
            "weight_metadata": {"candidate_model_runtime": "fallback_drift"},
            "final": [row],
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)
    candidate = result["rows"][0]

    assert candidate["规则等级"] == "B"
    assert candidate["行动分组"] == "重点确认"
    assert candidate["_blocked_reasons"] == []


def test_decision_pool_persists_as_the_production_execution_artifact(tmp_path):
    payload = {
        "schema_version": 1,
        "decision_count": 1,
        "rows": [{"code": "000001", "execution_eligible": True}],
    }

    path = DecisionPoolService.persist(payload, tmp_path / "screening", "20260720")
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "decision_pool_20260720.json"
    assert stored["trade_date"] == "20260720"
    assert stored["rows"][0]["execution_eligible"] is True
