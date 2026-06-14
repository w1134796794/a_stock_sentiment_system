"""情绪循环相位子态分析（消费 ``emotion_phase_model`` 的输出）。

情绪周期的**权威判定**来自 :func:`core.analysis.emotion_phase_model.compute_phase_model`
（小票 / 中军 / 大票分群 + 方向性相位：冰点 → 修复 → 发酵 → 高潮 → 退潮）。
本模块在其之上，提炼操盘手最关心的两个**前瞻性**问题：

1. **相位进度（phase_progress / phase_label）** —— 当前相位处于早期 / 中期 / 晚期？
   - 由动量方向（升温 / 见顶 / 降温）估计：升温=早期、走平=中期、见顶/降温=晚期。
2. **转换预警（transition_warning / next_likely_cycle）** —— 最可能转入哪个相位？
   - 由相位得分的次高项与领先差（score_gap）判断；高潮 / 见顶额外给出「退潮」预警。

输出 ``EmotionPhaseResult`` 的字段保持稳定（下游报告 / 状态 / 概览模板直接消费）。
简洁、可解释、可单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import loguru

logger = loguru.logger


# 新相位 → 旧周期名（与 emotion_phase_model.LEGACY_MAP 对齐，去掉 None 兜底项）
_LEGACY_MAP = {
    "冰点": "冰点期",
    "修复": "上升期",
    "发酵": "上升期",
    "高潮": "高潮期",
    "退潮": "退潮期",
}

# 动量方向 → 相位进度（0-1）。升温偏早期、走平居中、见顶/降温偏晚期。
_MOMENTUM_PROGRESS = {"升温": 0.2, "—": 0.5, "见顶": 0.9, "降温": 0.75}


@dataclass
class EmotionPhaseResult:
    """情绪周期相位分析结果。"""
    cycle_name: str
    phase_progress: float           # 0-1，当前相位内的进度位置
    phase_label: str                # "早期" / "中期" / "晚期"
    transition_warning: str         # 转换预警文字
    next_likely_cycle: str          # 最可能转入的相位（旧周期名）
    main_score: float               # 主相位得分
    next_score: float               # 次高相位得分
    score_gap: float                # 主分 - 次分


def _classify_phase_progress(progress: float) -> str:
    """把 0-1 进度归到"早期 / 中期 / 晚期"标签。"""
    if progress < 0.33:
        return "早期"
    if progress < 0.67:
        return "中期"
    return "晚期"


def _compute_transition_warning(
    phase: Optional[str],
    momentum: str,
    next_phase: str,
    score_gap: float,
) -> str:
    """生成转换预警文本（基于相位 / 动量 / 相位得分领先度）。"""
    if phase == "高潮" or momentum == "见顶":
        return "⚠ 高位见顶风险，警惕转入「退潮」"
    if momentum == "降温":
        nxt = next_phase or "退潮"
        return f"⚠ 动量降温，警惕走弱（下一相位「{nxt}」）"
    if next_phase and score_gap < 1.0:
        return f"⚠ 相位胶着，警惕转入「{next_phase}」（领先 {score_gap:.1f} 分）"
    if next_phase and score_gap < 2.0:
        return f"关注「{next_phase}」信号（领先 {score_gap:.1f} 分）"
    return "相位稳定"


def analyze_emotion_phase(emotion_result: Dict[str, Any]) -> Optional[EmotionPhaseResult]:
    """对 emotion_result 做相位子态分析。

    Args:
        emotion_result: ``EmotionCycleEngine.analyze_market_data`` 的返回值，
                        需含 ``phase_model``（来自 compute_phase_model）。

    Returns:
        ``EmotionPhaseResult``，输入异常或缺相位模型时返回 None。
    """
    if not emotion_result or not isinstance(emotion_result, dict):
        return None

    pm = emotion_result.get("phase_model") or {}
    scores = pm.get("scores") or {}
    if not scores:
        return None

    phase = pm.get("phase")                                   # 新相位名 或 "无主线"
    momentum = pm.get("momentum") or "—"
    legacy_cycle = (
        emotion_result.get("cycle_name")
        or pm.get("legacy_cycle_name")
        or "震荡期"
    )

    # 主相位 / 次相位（按相位得分排序）
    try:
        ordered = sorted(scores.items(), key=lambda kv: -float(kv[1]))
    except (TypeError, ValueError):
        return None
    main_phase, main_score = ordered[0]
    next_phase, next_score = (ordered[1] if len(ordered) > 1 else ("", 0.0))
    main_score = float(main_score)
    next_score = float(next_score)
    score_gap = round(main_score - next_score, 1)

    progress = _MOMENTUM_PROGRESS.get(momentum, 0.5)
    phase_label = _classify_phase_progress(progress)

    # 下一相位：高潮 / 见顶强制提示「退潮」，否则取次高相位
    nxt_phase = "退潮" if (phase == "高潮" or momentum == "见顶") else next_phase
    next_legacy = _LEGACY_MAP.get(nxt_phase, nxt_phase or "")

    warning = _compute_transition_warning(phase, momentum, nxt_phase, score_gap)

    return EmotionPhaseResult(
        cycle_name=legacy_cycle,
        phase_progress=progress,
        phase_label=phase_label,
        transition_warning=warning,
        next_likely_cycle=next_legacy,
        main_score=main_score,
        next_score=next_score,
        score_gap=score_gap,
    )


__all__ = [
    "EmotionPhaseResult",
    "analyze_emotion_phase",
]
