import json

from core.agent.evidence_service import AgentEvidenceService
from core.agent.review_assistant import ReviewAssistantService
from core.agent.style_skills import StyleSkillRegistry


def test_candidate_evidence_is_read_only_and_structured(tmp_path):
    screening = tmp_path / "screening"
    screening.mkdir()
    (screening / "screening_20260703.json").write_text(json.dumps({
        "candidate_pool": [{
            "code": "000001", "name": "测试", "score": 80, "rank": 1,
            "decision_label": "具备优势，等待盘中确认",
            "confidence": {"candidate_probability": 60},
            "trust_layers": {"data": {"score": 100}},
            "reasons": ["测试理由"], "shap_explanation": [{"factor": "x", "contribution": 1}],
        }],
        "weight_metadata": {"model_type": "test"},
    }), encoding="utf-8")
    result = AgentEvidenceService(web_data_dir=tmp_path, duckdb_path=tmp_path / "missing.db").get_candidate_evidence("20260703", "000001")
    assert result["ok"] is True
    assert result["evidence_type"] == "candidate"
    assert result["trust_layers"]["data"]["score"] == 100


def test_style_skills_are_versioned_and_can_abstain():
    registry = StyleSkillRegistry()
    skill = registry.get("weak_to_strong")
    assert skill["version"] == "1.0.0"
    result = registry.evaluate_readiness("weak_to_strong", {"candidate": True})
    assert result["status"] == "data_insufficient"
    assert "minute_quote" in result["missing"]


def test_review_assistant_translates_model_fields_into_plain_actions(tmp_path):
    screening = tmp_path / "screening"
    screening.mkdir()
    (screening / "screening_20260703.json").write_text(json.dumps({
        "final": [
            {
                "code": "000001", "name": "优势股", "rank": 1,
                "confidence_grade": "B", "decision_status": "eligible",
                "candidate_probability": 58, "baseline_probability": 40,
                "expected_gross_return_pct": 3.2, "expected_excess_return_pct": 1.1,
                "stop_probability": 42, "resonance_sectors": "机器人,汽车零部件",
                "reasons": ["板块与量价同步增强"],
            },
            {
                "code": "000002", "name": "观察股", "rank": 2,
                "confidence_grade": "D", "decision_status": "model_degraded",
                "candidate_probability": 25, "baseline_probability": 20,
                "expected_gross_return_pct": 1.0, "expected_excess_return_pct": 0.2,
                "stop_probability": 70, "reasons": ["模型漂移"],
                "model_drift_status": "degraded",
            },
        ],
        "weight_metadata": {"market_regime": "neutral"},
    }), encoding="utf-8")
    evidence = AgentEvidenceService(web_data_dir=tmp_path, duckdb_path=tmp_path / "missing.db")
    assistant = ReviewAssistantService(evidence)

    brief = assistant.build_brief("20260703", capital=50_000)
    assert brief["market_regime"] == "震荡"
    assert brief["actionable_count"] == 1
    assert brief["candidates"][0]["action"] == "重点观察"
    assert brief["candidates"][1]["action"] == "只观察"
    assert brief["capital_preset"]["max_positions"] == 3
    assert "确认后参考仓位" in brief["candidates"][0]["one_line"]

    answer = assistant.answer("20260703", "000001怎么看")
    assert "未来3日总收益 +3.20%" in answer["answer"]
    assert "超额收益 +1.10%" in answer["answer"]
