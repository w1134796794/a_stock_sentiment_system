"""跨策略信号仲裁单测（Phase 4）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from core.pattern import signal_arbitrator as arb


@dataclass
class _Sig:
    stock_code: str
    stock_name: str = ""
    confidence: float = 0.6
    key_metrics: Dict[str, Any] = field(default_factory=dict)


def _results(**kw):
    return {k: v for k, v in kw.items()}


def test_single_pattern_no_resonance():
    r = _results(弱转强=[_Sig("000001.SZ", "平安", 0.7)])
    res = arb.arbitrate(r)
    d = res.decisions["000001"]
    assert d.primary_pattern == "弱转强"
    assert d.is_resonance is False
    assert d.suppressed_patterns == []


def test_multi_pattern_primary_by_priority():
    # 同票被弱转强+二板定龙命中：二板定龙优先级更高 → 主信号
    s1 = _Sig("000002.SZ", "万科", 0.9)   # 弱转强 置信更高
    s2 = _Sig("000002.SZ", "万科", 0.72)  # 二板定龙 置信较低但优先级高
    res = arb.arbitrate(_results(弱转强=[s1], 二板定龙=[s2]))
    d = res.decisions["000002"]
    assert d.primary_pattern == "二板定龙"
    assert "弱转强" in d.suppressed_patterns
    assert d.is_resonance is True


def test_priority_tiebreak_by_confidence():
    # 构造同优先级不可能（优先级唯一），此处验证置信度参与排序：自定义 priority 持平
    s1 = _Sig("600000.SH", "浦发", 0.65)
    s2 = _Sig("600000.SH", "浦发", 0.80)
    res = arb.arbitrate(_results(弱转强=[s1], 龙二波=[s2]),
                        priority={"弱转强": 1, "龙二波": 1})
    d = res.decisions["600000"]
    assert d.primary_pattern == "龙二波"  # 置信度更高


def test_annotate_mode_zero_change():
    s1 = _Sig("000002.SZ", "万科", 0.9)
    s2 = _Sig("000002.SZ", "万科", 0.72)
    r = _results(弱转强=[s1], 二板定龙=[s2])
    res = arb.arbitrate(r)
    out = arb.apply(r, res, mode="annotate")
    # 信号集合不变
    assert len(out["弱转强"]) == 1 and len(out["二板定龙"]) == 1
    # 置信度不变
    assert s1.confidence == 0.9 and s2.confidence == 0.72
    # 标注写入
    assert s2.key_metrics["仲裁"]["本条为主信号"] is True
    assert s1.key_metrics["仲裁"]["本条为主信号"] is False


def test_reweight_mode_bonus_on_primary_only():
    s1 = _Sig("000002.SZ", "万科", 0.90)   # 弱转强（被抑制）
    s2 = _Sig("000002.SZ", "万科", 0.72)   # 二板定龙（主）
    r = _results(弱转强=[s1], 二板定龙=[s2])
    res = arb.arbitrate(r, cfg={"resonance_bonus": 0.05})
    out = arb.apply(r, res, mode="reweight")
    assert abs(s2.confidence - 0.77) < 1e-9   # 主信号加权
    assert s1.confidence == 0.90              # 被抑制信号不变
    assert len(out["弱转强"]) == 1            # reweight 不剔除


def test_reweight_bonus_capped_by_max():
    s = _Sig("000002.SZ", "万科", 0.965)
    s2 = _Sig("000002.SZ", "万科", 0.50)
    r = _results(二板定龙=[s], 弱转强=[s2])
    res = arb.arbitrate(r, cfg={"resonance_bonus": 0.05, "resonance_max_confidence": 0.98})
    arb.apply(r, res, mode="reweight")
    assert abs(s.confidence - 0.98) < 1e-9    # 0.965+min(0.05,0.015)=0.98 封顶


def test_dedup_removes_suppressed_keeps_snapshot():
    s1 = _Sig("000002.SZ", "万科", 0.90)   # 弱转强（被抑制）
    s2 = _Sig("000002.SZ", "万科", 0.72)   # 二板定龙（主）
    r = _results(弱转强=[s1], 二板定龙=[s2])
    res = arb.arbitrate(r)
    out = arb.apply(r, res, mode="dedup")
    assert len(out["弱转强"]) == 0           # 被抑制剔除
    assert len(out["二板定龙"]) == 1         # 主信号保留
    snap = s2.key_metrics["仲裁"]["被抑制明细"]
    assert any(x["策略"] == "弱转强" for x in snap)


def test_code_normalization_cross_suffix():
    # 带后缀 vs 6位纯数字应视为同一只
    s1 = _Sig("000002.SZ", "万科", 0.7)
    s2 = _Sig("000002", "万科", 0.8)
    res = arb.arbitrate(_results(弱转强=[s1], 龙二波=[s2]))
    assert "000002" in res.decisions
    assert res.decisions["000002"].is_resonance is True


def test_emotion_routing_changes_primary():
    # 默认：二板定龙(4) > 弱转强(2) → 主二板定龙
    s_wts = _Sig("000002.SZ", "万科", 0.7)
    s_sbd = _Sig("000002.SZ", "万科", 0.7)
    r = _results(弱转强=[s_wts], 二板定龙=[s_sbd])
    base = arb.arbitrate(r)
    assert base.decisions["000002"].primary_pattern == "二板定龙"
    # 退潮期路由：弱转强 +5 → 反超二板定龙
    cfg = {"emotion_routing": {"退潮期": {"弱转强": 5}}}
    routed = arb.arbitrate(r, cfg=cfg, emotion="退潮期")
    assert routed.decisions["000002"].primary_pattern == "弱转强"


def test_emotion_routing_empty_is_noop():
    s_wts = _Sig("000002.SZ", "万科", 0.7)
    s_sbd = _Sig("000002.SZ", "万科", 0.7)
    r = _results(弱转强=[s_wts], 二板定龙=[s_sbd])
    routed = arb.arbitrate(r, cfg={"emotion_routing": {}}, emotion="退潮期")
    assert routed.decisions["000002"].primary_pattern == "二板定龙"


def test_emotion_gate_suppresses_pattern():
    r = _results(首板突破=[_Sig("000001.SZ", "平安", 0.7)],
                 弱转强=[_Sig("000002.SZ", "万科", 0.7)])
    cfg = {"emotion_gate": {"退潮期": ["首板突破"]}}
    out = arb.gate_by_emotion(r, "退潮期", cfg)
    assert out["首板突破"] == []
    assert len(out["弱转强"]) == 1


def test_emotion_gate_empty_is_noop():
    r = _results(首板突破=[_Sig("000001.SZ", "平安", 0.7)])
    out = arb.gate_by_emotion(r, "退潮期", {"emotion_gate": {}})
    assert len(out["首板突破"]) == 1