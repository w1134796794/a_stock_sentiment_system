"""Sprint F-7：龙虎榜游资信誉 → 信号置信度 / 综合评分调整器

把 ``LHBResult`` 的"个股游资画像"折算成对 Layer3 信号(``RankedSignal``)与
多因子综合评分(``CompositeScore``)的乘子 / 增减项，使得：

* **黑名单游资接盘** → 置信度打折 + 综合分扣分 → 仓位档位下沉甚至"放弃"
  （这正是最初需求："遇到名声不好的游资上龙虎榜，做决策时避开"）
* **优质白名单游资进场** → 置信度小幅加权 + 综合分加分 → 排序上浮（顺优质资金）

设计取向
========
* **纯函数**：输入 ``ranked_signals`` / ``composite_scores`` / ``lhb_result``，
  原地调整对象字段并返回调整明细 ``list[LHBAdjustment]``，便于离线单测。
* **降级安全**：``lhb_result`` 为 None 或 ``available=False``（积分不足）→ 一切不变，
  返回空明细，绝不抛异常拖垮主流水线。
* **稀疏叠加**：仅对"当日出现在游资榜上的股票"生效，是稀疏但高信号的叠加层。
* **职责单一**：本模块只改分/改置信度并写说明；**重新排序 / 重新 rank** 由调用方负责
  （pipeline 在调用后重排 ranked_signals 与 composite_scores）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import loguru

from core.stock_ranking.multi_factor_scorer import action_for_score

logger = loguru.logger


# ---------------------------------------------------------------------------
# 配置 & 明细
# ---------------------------------------------------------------------------

@dataclass
class LHBAdjustConfig:
    """游资信誉调整参数（保守默认，可按账户风格调）。"""

    bad_conf_mult: float = 0.65       # 黑名单接盘：置信度乘子
    good_conf_mult: float = 1.10      # 白名单进场：置信度乘子
    bad_score_delta: float = -15.0    # 黑名单接盘：综合分扣分
    good_score_delta: float = 4.0     # 白名单进场：综合分加分

    # 灰度补充：信誉加权净买入大幅为负（坏游资重金接盘）→ 额外扣分
    weighted_net_threshold: float = 5e7   # 5000 万（元）
    weighted_net_extra_delta: float = -5.0

    conf_floor: float = 0.05
    conf_cap: float = 0.99
    score_floor: float = 0.0
    score_cap: float = 100.0


@dataclass
class LHBAdjustment:
    """一条调整记录（供日志 / 报告展示）。"""

    stock_code: str
    stock_name: str
    kind: str                 # "bad" / "good"
    conf_before: float = 0.0
    conf_after: float = 0.0
    score_before: float = 0.0
    score_after: float = 0.0
    action_before: str = ""
    action_after: str = ""
    note: str = ""            # "⚠ 黑名单游资[拉萨天团] 接盘 → 降权降仓"
    seats: str = ""           # 席位摘要

    def to_dict(self) -> dict:
        return {
            "代码": self.stock_code,
            "名称": self.stock_name,
            "类型": "坑货规避" if self.kind == "bad" else "优质加权",
            "置信度": f"{self.conf_before:.2f}→{self.conf_after:.2f}",
            "综合分": f"{self.score_before:.0f}→{self.score_after:.0f}",
            "建议": f"{self.action_before}→{self.action_after}"
            if self.action_before and self.action_before != self.action_after
            else self.action_after,
            "说明": self.note,
            "席位": self.seats,
        }


# ---------------------------------------------------------------------------
# 分类逻辑（纯函数）
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def classify_profile(profile: Any, config: LHBAdjustConfig) -> Tuple[Optional[str], str]:
    """把一只票的游资画像分类为 'bad' / 'good' / None，并给出说明文案。

    规则（保守，坏优先）：
      1. 有黑名单游资在买方 → 'bad'
      2. 信誉加权净买入大幅为负（坏游资重金接盘） → 'bad'（即便没人被打"黑"标）
      3. 否则有白名单游资在买方 → 'good'
      4. 否则不调整
    """
    if profile is None:
        return None, ""

    bad_names = [s.hm_name for s in profile.buyers if s.label == "黑"]
    good_names = [s.hm_name for s in profile.buyers if s.label == "白"]
    weighted = float(getattr(profile, "reputation_weighted_net", 0.0) or 0.0)

    if bad_names:
        names = "、".join(dict.fromkeys(bad_names))  # 去重保序
        return "bad", f"⚠ 黑名单游资[{names}]接盘 → 降权降仓"

    if weighted <= -config.weighted_net_threshold:
        return "bad", f"⚠ 信誉加权净买入大幅为负({weighted/1e4:.0f}万) → 坏资金主导，降权"

    if good_names:
        names = "、".join(dict.fromkeys(good_names))
        return "good", f"✓ 优质游资[{names}]进场 → 小幅加权"

    return None, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def adjust_signals(
    ranked_signals: List[Any],
    composite_scores: List[Any],
    lhb_result: Any,
    config: Optional[LHBAdjustConfig] = None,
) -> List[LHBAdjustment]:
    """根据游资信誉原地调整 ``ranked_signals`` 与 ``composite_scores``。

    Args:
        ranked_signals: ``list[RankedSignal]``，调整其 ``confidence`` + ``lhb_adjust_note``
        composite_scores: ``list[CompositeScore]``，调整其 ``total_score`` + 重算建议
        lhb_result: ``LHBResult`` 或 None
        config: 调整参数

    Returns:
        ``list[LHBAdjustment]`` 调整明细（无调整时为空）。调整后**不**重排，由调用方负责。
    """
    config = config or LHBAdjustConfig()

    if lhb_result is None or not getattr(lhb_result, "available", False):
        return []

    adjustments: List[LHBAdjustment] = []
    # 同一只票可能同时出现在 ranked_signals 与 composite_scores，合并成一条记录
    by_code: dict[str, LHBAdjustment] = {}

    def _record(code: str, name: str, kind: str, note: str, seats: str) -> LHBAdjustment:
        rec = by_code.get(code)
        if rec is None:
            rec = LHBAdjustment(
                stock_code=code, stock_name=name, kind=kind, note=note, seats=seats
            )
            by_code[code] = rec
            adjustments.append(rec)
        return rec

    # --- 1) 调 RankedSignal.confidence（影响排序 / 展示） ---
    for sig in ranked_signals or []:
        code = getattr(sig, "stock_code", "")
        profile = lhb_result.get_stock(code) if code else None
        kind, note = classify_profile(profile, config)
        if not kind:
            continue
        seats = profile.seats_summary() if profile else ""
        mult = config.bad_conf_mult if kind == "bad" else config.good_conf_mult
        before = float(getattr(sig, "confidence", 0.0) or 0.0)
        after = _clamp(before * mult, config.conf_floor, config.conf_cap)
        sig.confidence = after
        sig.lhb_adjust_note = note

        rec = _record(code, getattr(sig, "stock_name", ""), kind, note, seats)
        rec.conf_before, rec.conf_after = before, after

    # --- 2) 调 CompositeScore.total_score（影响仓位档位 / 建议操作 → Layer4） ---
    for cs in composite_scores or []:
        code = getattr(cs, "stock_code", "")
        profile = lhb_result.get_stock(code) if code else None
        kind, note = classify_profile(profile, config)
        if not kind:
            continue
        seats = profile.seats_summary() if profile else ""
        weighted = float(getattr(profile, "reputation_weighted_net", 0.0) or 0.0)

        delta = config.bad_score_delta if kind == "bad" else config.good_score_delta
        # 坏资金重金接盘再加一档惩罚
        if kind == "bad" and weighted <= -config.weighted_net_threshold:
            delta += config.weighted_net_extra_delta

        before = float(getattr(cs, "total_score", 0.0) or 0.0)
        after = _clamp(before + delta, config.score_floor, config.score_cap)
        action_before, _ = action_for_score(before)
        action_after, pos_after = action_for_score(after)

        cs.total_score = after
        cs.suggested_action = action_after
        cs.suggested_position_pct = pos_after
        cs.lhb_adjust_delta = after - before
        cs.lhb_adjust_note = note

        rec = _record(code, getattr(cs, "stock_name", ""), kind, note, seats)
        rec.score_before, rec.score_after = before, after
        rec.action_before, rec.action_after = action_before, action_after

    if adjustments:
        n_bad = sum(1 for a in adjustments if a.kind == "bad")
        n_good = sum(1 for a in adjustments if a.kind == "good")
        logger.info(
            f"[LHB-Adjust] 游资信誉调整 {len(adjustments)} 只："
            f"坑货规避 {n_bad} / 优质加权 {n_good}"
        )
        for a in adjustments:
            logger.info(
                f"[LHB-Adjust] {a.stock_name}({a.stock_code}) {a.note} | "
                f"置信度 {a.conf_before:.2f}→{a.conf_after:.2f} "
                f"综合分 {a.score_before:.0f}→{a.score_after:.0f} "
                f"建议 {a.action_before}→{a.action_after}"
            )

    return adjustments


__all__ = [
    "LHBAdjustConfig",
    "LHBAdjustment",
    "classify_profile",
    "adjust_signals",
]
