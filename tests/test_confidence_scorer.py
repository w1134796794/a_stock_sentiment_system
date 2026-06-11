"""
Phase 3 验收：ConfidenceScorer 扣分制引擎。

覆盖：分段命中、满分(无扣)、缺失因子重扣、floor 封底、ceiling 封顶、
enabled 开关、breakdown 明细、归一化 0~1。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.scoring.confidence_scorer import ConfidenceScorer, get_scorer


RULESET = {
    "ceiling": 95,
    "floor": 40,
    "rules": [
        {"factor": "seal_ratio", "enabled": True,
         "bands": [[0.05, 0], [0.03, 3], [0.02, 5], [0.01, 10], [0.0, 15]]},
        {"factor": "gap_ratio", "enabled": True,
         "bands": [[0.05, 0], [0.03, 4], [0.02, 8], [0.0, 15]]},
        {"factor": "first_board_score", "enabled": True,
         "bands": [[80, 0], [70, 5], [60, 10], [0, 20]]},
        {"factor": "is_fast", "enabled": True,
         "bands": [[1, 0], [0, 10]]},
    ],
}


def test_perfect_hits_ceiling():
    s = ConfidenceScorer(RULESET)
    r = s.score({"seal_ratio": 0.08, "gap_ratio": 0.06, "first_board_score": 90, "is_fast": 1})
    assert r.raw == 95
    assert r.value == pytest.approx(0.95)
    assert r.total_penalty == 0


def test_band_selection_mid():
    s = ConfidenceScorer(RULESET)
    # seal 0.03→3, gap 0.03→4, score 70→5, fast 0→10  => 22 penalty
    r = s.score({"seal_ratio": 0.03, "gap_ratio": 0.03, "first_board_score": 70, "is_fast": 0})
    assert r.total_penalty == 22
    assert r.raw == 95 - 22
    assert r.value == pytest.approx((95 - 22) / 100.0)


def test_floor_clamps_worst_case():
    s = ConfidenceScorer(RULESET)
    # 全踩最差: 15+15+20+10 = 60 → 95-60=35 → floor 40
    r = s.score({"seal_ratio": 0.0, "gap_ratio": 0.0, "first_board_score": 0, "is_fast": 0})
    assert r.total_penalty == 60
    assert r.raw == 40
    assert r.value == pytest.approx(0.40)


def test_missing_factor_worst_penalty():
    s = ConfidenceScorer(RULESET)
    # seal 缺失 → 取该规则最大扣分 15；其余满分
    r = s.score({"gap_ratio": 0.06, "first_board_score": 90, "is_fast": 1})
    seal_detail = next(d for d in r.breakdown if d.factor == "seal_ratio")
    assert seal_detail.missing is True
    assert seal_detail.penalty == 15
    assert r.raw == 95 - 15


def test_disabled_rule_no_penalty():
    rs = {
        "ceiling": 95, "floor": 40,
        "rules": [
            {"factor": "seal_ratio", "enabled": False,
             "bands": [[0.05, 0], [0.0, 15]]},
            {"factor": "gap_ratio", "enabled": True,
             "bands": [[0.05, 0], [0.0, 15]]},
        ],
    }
    s = ConfidenceScorer(rs)
    r = s.score({"seal_ratio": 0.0, "gap_ratio": 0.0})
    # seal 被禁用→不扣；只有 gap 扣 15
    assert r.total_penalty == 15
    assert all(d.factor != "seal_ratio" for d in r.breakdown)


def test_breakdown_shape_and_value_normalized():
    s = ConfidenceScorer(RULESET)
    r = s.score({"seal_ratio": 0.02, "gap_ratio": 0.02, "first_board_score": 60, "is_fast": 1})
    assert 0.0 <= r.value <= 1.0
    assert len(r.breakdown) == 4
    d = r.to_dict()
    assert set(d) == {"value", "raw", "ceiling", "floor", "total_penalty", "breakdown"}


def test_unsorted_bands_are_handled():
    # bands 乱序输入也应正确（内部按阈值降序）
    rs = {"ceiling": 100, "floor": 0, "rules": [
        {"factor": "x", "enabled": True, "bands": [[0.0, 15], [0.05, 0], [0.02, 5]]},
    ]}
    s = ConfidenceScorer(rs)
    assert s.score({"x": 0.05}).total_penalty == 0
    assert s.score({"x": 0.02}).total_penalty == 5
    assert s.score({"x": 0.0}).total_penalty == 15


def test_categorical_mapping_rule():
    rs = {"ceiling": 100, "floor": 0, "rules": [
        {"factor": "wtype", "mapping": {"断板": 0, "放量滞涨": 8}, "default": 5},
    ]}
    s = ConfidenceScorer(rs)
    assert s.score({"wtype": "断板"}).total_penalty == 0
    assert s.score({"wtype": "放量滞涨"}).total_penalty == 8
    assert s.score({"wtype": "其它"}).total_penalty == 5          # default
    # 缺失类别 → 最差（max(mapping ∪ default)）
    assert s.score({}).total_penalty == 8


def test_lower_better_direction():
    rs = {"ceiling": 100, "floor": 0, "rules": [
        {"factor": "days", "direction": "lower_better",
         "bands": [[7, 0], [10, 3], [99999, 8]]},
    ]}
    s = ConfidenceScorer(rs)
    assert s.score({"days": 5}).total_penalty == 0
    assert s.score({"days": 9}).total_penalty == 3
    assert s.score({"days": 30}).total_penalty == 8


def test_all_strategy_rulesets_load():
    for name in ["second_board_dragon", "weak_to_strong",
                 "first_board_breakout", "dragon_second_wave"]:
        assert get_scorer(name) is not None, f"{name} 规则缺失"


def test_get_scorer_loads_yaml():
    s = get_scorer("second_board_dragon")
    assert s is not None
    r = s.score({"seal_ratio": 0.08, "gap_ratio": 0.06, "first_board_score": 90, "is_fast": 1})
    assert r.value == pytest.approx(0.95)
    # 未知策略 → None（调用方回退旧逻辑）
    assert get_scorer("不存在的策略") is None