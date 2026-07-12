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


def test_neutral_market_deduplicates_and_hides_inapplicable_strategies():
    profiles = {
        "capital_resonance": _profile("capital_resonance", "资金共振"),
        "momentum_repair": _profile("momentum_repair", "量价修复"),
        "weak_to_strong": _profile("weak_to_strong", "弱转强"),
        "mainline_leader": _profile("mainline_leader", "主线龙头"),
    }
    payloads = {
        "capital_resonance": {"final": [_row("000001", "甲"), _row("000002", "乙")]},
        "momentum_repair": {"final": [_row("000001", "甲"), _row("000003", "丙")]},
        "weak_to_strong": {"final": [_row("000001", "甲"), _row("000004", "丁")]},
        "mainline_leader": {"final": [_row("000005", "强市股票")]},
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=55)

    assert result["regime"] == "neutral"
    assert result["active_strategy_ids"] == ["capital_resonance", "momentum_repair", "weak_to_strong"]
    assert result["hidden_strategy_names"] == ["主线龙头"]
    assert len(result["rows"]) == 4
    first = next(row for row in result["rows"] if row["code"] == "000001")
    assert first["策略共识显示"] == "3/3"
    assert first["行动分组"] == "重点确认"
    assert first["所属主线"] == "机器人"


def test_invalid_model_is_never_promoted_to_decision_pool():
    profiles = {"weak_market_probe": _profile("weak_market_probe", "弱市试仓")}
    payloads = {
        "weak_market_probe": {
            "final": [_row("000001", "甲", grade="D", status="data_insufficient")]
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)

    assert result["decision_count"] == 0
    assert len(result["groups"][2]["rows"]) == 1
    assert result["rows"][0]["行动分组"] == "暂不参与"
    assert result["rows"][0]["建议仓位"] == "0%"


def test_low_expected_return_caps_high_grade_at_watch():
    profiles = {"weak_market_probe": _profile("weak_market_probe", "弱市试仓")}
    row = _row("000001", "甲", grade="A")
    row["expected_return_pct"] = 0.29
    payloads = {"weak_market_probe": {"final": [row]}}

    result = DecisionPoolService().build(payloads, profiles, market_score=40)

    assert not result["groups"][0]["rows"]
    assert result["groups"][1]["rows"][0]["行动分组"] == "盘中观察"
    assert result["groups"][1]["rows"][0]["股票等级"] == "A"


def test_fallback_model_status_is_separate_from_stock_grade():
    profiles = {"weak_market_probe": _profile("weak_market_probe", "弱市试仓")}
    payloads = {
        "weak_market_probe": {
            "weight_metadata": {"candidate_model_runtime": "fallback_drift"},
            "final": [_row("000001", "甲", grade="B")],
        }
    }

    result = DecisionPoolService().build(payloads, profiles, market_score=40)
    row = result["rows"][0]

    assert row["模型状态"] == "已回退"
    assert row["股票等级"] == "B"
    assert row["行动分组"] == "重点确认"
