"""
Phase 3 A/B：弱转强 / 首板突破 / 龙二波 置信度扣分制接入测试。

验证目标：
- 三套规则集 best-case → ceiling、worst-case → floor 附近，且值恒在 [floor, ceiling] 内；
- 弱转强 `_auction_recovery_confidence` 默认 legacy（无 breakdown、行为不变），
  deduction 模式产出 breakdown 且值合理；
- best-case 在 deduction 下命中 ceiling，验证 0~1 归一化口径正确。
"""
from types import SimpleNamespace

import pytest

from config.pattern_params import get_params
from core.scoring.confidence_scorer import get_scorer


# ----------------------------- 规则集 best/worst 合理性 -----------------------------

STRATEGY_CASES = {
    "weak_to_strong": (
        {"gap_pct": 0.05, "auction_vol_ratio": 0.2, "flexible_score": 90, "weakening_type": "断板"},
        {"gap_pct": 0.0, "auction_vol_ratio": 0.0, "flexible_score": 0, "weakening_type": "放量滞涨"},
    ),
    "first_board_breakout": (
        {"breakout_type": "前高突破", "seal_ratio": 0.05, "volume_ratio_excess": 0.6,
         "break_count": 0, "early_seal": 1, "sector_score": 0.09},
        {"breakout_type": "其他", "seal_ratio": 0.0, "volume_ratio_excess": 0.0,
         "break_count": 2, "early_seal": 0, "sector_score": 0.0},
    ),
    "dragon_second_wave": (
        {"max_boards": 6, "days_since_peak": 3, "seal_ratio": 0.05, "break_count": 0,
         "early_seal": 1, "sector_score": 0.09, "layer2_clean": 1},
        {"max_boards": 1, "days_since_peak": 30, "seal_ratio": 0.0, "break_count": 2,
         "early_seal": 0, "sector_score": 0.0, "layer2_clean": 0},
    ),
}


@pytest.mark.parametrize("name", list(STRATEGY_CASES.keys()))
def test_ruleset_best_worst_bounds(name):
    scorer = get_scorer(name)
    assert scorer is not None, f"{name} 规则缺失"
    best_factors, worst_factors = STRATEGY_CASES[name]

    best = scorer.score(best_factors)
    worst = scorer.score(worst_factors)

    ceiling01 = scorer.ceiling / 100.0
    floor01 = scorer.floor / 100.0

    # best 命中天花板；worst 不低于地板；二者皆在合法区间且 worst < best
    assert best.value == pytest.approx(ceiling01)
    assert floor01 <= worst.value <= ceiling01
    assert worst.value < best.value
    # breakdown 完整：每条启用规则各一项
    assert len(best.breakdown) == len([r for r in scorer.rules if r["enabled"]])


# ----------------------------- 弱转强方法级 A/B -----------------------------

def _make_wts():
    """绕过重型 __init__，仅注入 params 以测试置信度辅助方法。"""
    from core.pattern.weak_to_strong import WeakToStrongStrategy
    s = WeakToStrongStrategy.__new__(WeakToStrongStrategy)
    s.params = dict(get_params("weak_to_strong"))
    return s


def _dyn(params):
    return {
        "min_gap": params["min_gap"],
        "max_gap": params["max_gap"],
        "min_auction_vol_ratio": params["min_auction_vol_ratio"],
    }


def test_wts_default_is_legacy_no_breakdown():
    s = _make_wts()
    s.params["confidence_mode"] = "legacy"   # 固定 legacy，独立于运行期覆盖
    weak = SimpleNamespace(weakening_type="断板", stock_code="000001")
    val, bd = s._auction_recovery_confidence(
        s.params["ideal_gap"], _dyn(s.params), s.params["ideal_auction_vol_ratio"], 90, weak
    )
    assert bd is None                      # 默认 legacy 不产出 breakdown
    assert 0.50 <= val <= 0.95


def test_wts_legacy_value_matches_formula():
    s = _make_wts()
    s.params["confidence_mode"] = "legacy"   # 固定 legacy，独立于运行期覆盖
    weak = SimpleNamespace(weakening_type="断板", stock_code="x")
    val, _ = s._auction_recovery_confidence(
        s.params["ideal_gap"], _dyn(s.params), s.params["ideal_auction_vol_ratio"], 90, weak
    )
    # 0.60 + 高开0.15 + 竞价量0.10 + 断板0.05 (+弹性视开关) ，封顶0.95
    expected = 0.60 + 0.15 + 0.10 + 0.05
    if s.params.get("enable_flexible_scoring", False):
        expected += 0.10
    expected = min(0.95, max(0.50, expected))
    assert val == pytest.approx(expected)


def test_wts_deduction_mode_breakdown_and_ceiling():
    s = _make_wts()
    s.params["confidence_mode"] = "deduction"
    weak = SimpleNamespace(weakening_type="断板", stock_code="x")
    # best-case 入参 → 命中 ceiling 0.95
    val, bd = s._auction_recovery_confidence(0.05, _dyn(s.params), 0.2, 90, weak)
    assert bd is not None
    assert "breakdown" in bd
    assert val == pytest.approx(0.95)