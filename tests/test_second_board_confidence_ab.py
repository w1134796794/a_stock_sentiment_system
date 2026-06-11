"""
Phase 3 A/B：二板定龙 legacy vs deduction 置信度对比。

目的：
1. 验证默认 mode="legacy" 时 _second_board_confidence 与旧函数逐位一致（行为不变），
   且不产生 breakdown。
2. 验证 mode="deduction" 走新引擎、产出 breakdown，且在因子网格上与 legacy 单调一致
   （越好的因子→两者都给越高分），分布合理（均落在 [0.40, 0.95]）。
"""
import os
import sys
import itertools

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.pattern.pattern_recognition import PatternRecognition


@pytest.fixture
def pr():
    # 仅用其纯方法，无需 dm/repo
    return PatternRecognition(data_manager=None)


def _q(score):
    return {"score": score, "type": "硬板"}


def test_legacy_mode_matches_old_and_no_breakdown(pr):
    cases = [
        (_q(90), 0.06, True, 0.06),
        (_q(72), 0.03, False, 0.025),
        (_q(50), 0.0, False, 0.0),
        (_q(80), 0.05, True, 0.05),
    ]
    for quality, gap, is_fast, seal in cases:
        conf, breakdown = pr._second_board_confidence("legacy", quality, gap, is_fast, seal)
        expected = pr._calculate_second_board_confidence(quality, gap, is_fast, seal)
        assert conf == expected
        assert breakdown is None


def test_deduction_mode_produces_breakdown(pr):
    conf, breakdown = pr._second_board_confidence("deduction", _q(90), 0.06, True, 0.08)
    assert breakdown is not None
    assert conf == pytest.approx(0.95)
    assert breakdown["raw"] == 95
    assert {d["factor"] for d in breakdown["breakdown"]} == {
        "seal_ratio", "gap_ratio", "first_board_score", "is_fast"
    }


def test_deduction_in_valid_range_and_monotonic(pr):
    scores = [40, 65, 75, 85]
    gaps = [0.0, 0.025, 0.04, 0.06]
    seals = [0.0, 0.015, 0.03, 0.06]
    fasts = [False, True]

    best = pr._second_board_confidence("deduction", _q(85), 0.06, True, 0.06)[0]
    worst = pr._second_board_confidence("deduction", _q(40), 0.0, False, 0.0)[0]
    assert best == pytest.approx(0.95)
    assert worst == pytest.approx(0.40)  # floor

    for s, g, se, f in itertools.product(scores, gaps, seals, fasts):
        conf, _ = pr._second_board_confidence("deduction", _q(s), g, f, se)
        assert 0.40 <= conf <= 0.95
        # 不会优于全好、不会差于全坏
        assert worst <= conf <= best


def test_unknown_mode_falls_back_to_legacy(pr):
    conf, breakdown = pr._second_board_confidence("garbage", _q(80), 0.05, True, 0.05)
    assert breakdown is None
    assert conf == pr._calculate_second_board_confidence(_q(80), 0.05, True, 0.05)