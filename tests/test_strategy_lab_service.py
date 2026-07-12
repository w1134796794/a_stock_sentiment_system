import json

from core.portfolio.strategy_lab_service import StrategyLabService


def test_strategy_lab_previews_combined_candidates(tmp_path):
    screening_dir = tmp_path / "screening"
    for strategy_id, code, score in (
        ("default", "000001", 90),
        ("trend_follow", "000001", 75),
    ):
        path = screening_dir / "combinations" / strategy_id / "screening_20260701.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"final": [{
            "code": code, "name": "共识股", "score": score, "rank": 1,
            "resonance_sectors": "芯片",
        }]}), encoding="utf-8")

    service = StrategyLabService(screening_dir=screening_dir)
    result = service.build("20260701", ["default", "trend_follow"])

    assert result["summary"]["input_candidates"] == 2
    assert result["summary"]["unique_candidates"] == 1
    assert result["allocation"][0]["策略共识数"] == 2
    assert result["allocation"][0]["策略来源"] == "default,trend_follow"


def test_strategy_lab_reads_legacy_default_artifact(tmp_path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    (screening_dir / "screening_20260701.json").write_text(json.dumps({"final": [{
        "code": "600000", "name": "默认候选", "score": 80, "rank": 1,
    }]}), encoding="utf-8")

    result = StrategyLabService(screening_dir=screening_dir).build("20260701", ["default"])

    assert result["strategies"][0]["available"] is True
    assert result["summary"]["allocated_count"] == 1
