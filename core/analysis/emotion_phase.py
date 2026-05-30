"""Sprint E-1：情绪周期相位 / 转换预警分析器

现有 ``EmotionCycleEngine`` 给出"今日处于哪个周期"（高潮/上升/震荡/退潮/冰点），
但实战中操盘手最关心的是两个**前瞻性**问题：

1. **相位进度（phase_progress）** —— 当前周期是早期 / 中期 / 晚期？
   - 上升期早期：可以重仓加仓
   - 上升期晚期：开始减仓换强势龙头
   - 同样是"上升期"，早期和晚期的操作策略截然不同。

2. **转换预警（transition_warning）** —— 下一周期最可能是什么？
   - 上升期 + 高潮期分数次高且接近 → "警惕高潮期"，准备撤退
   - 上升期 + 退潮期分数次高 → "警惕退潮风险"，谨慎打板
   - 没有显著差异 → "暂稳"

实现策略
========

* **phase_progress**：取 emotion_result 中**当前周期 score** 在 (0~100) 区间内的归一化值，
  并结合关键指标（涨停数 / 连板高度 / 炸板率）的极端度做一个 0-1 的相位估计。
* **transition_warning**：检查 scores 字典里**次高分**和**主分**的差距：
  - 差距 < 8 分 → 「警惕向 {次高周期} 转换」
  - 差距 < 15 分 → 「关注 {次高周期} 信号」
  - 否则 → 「周期稳定」

简洁、可解释、可单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import loguru

logger = loguru.logger


# 周期顺序：从最强到最弱
_CYCLE_ORDER_DESC = ["高潮期", "上升期", "震荡期", "回暖期", "退潮期", "冰点期"]

# 周期英文 key 到中文名的映射（emotion_result 里 scores 用英文 key）
_SCORE_KEY_TO_NAME = {
    "boom": "高潮期",
    "rise": "上升期",
    "shake": "震荡期",
    "decline": "退潮期",
    "freeze": "冰点期",
}


@dataclass
class EmotionPhaseResult:
    """情绪周期相位分析结果。"""
    cycle_name: str
    phase_progress: float           # 0-1，当前周期内的进度位置
    phase_label: str                # "早期" / "中期" / "晚期"
    transition_warning: str         # 转换预警文字
    next_likely_cycle: str          # 次高分对应的周期名（=最可能转入的周期）
    main_score: float               # 主周期得分
    next_score: float               # 次高周期得分
    score_gap: float                # 主分 - 次分


# ---------------------------------------------------------------------------
# 主分析函数
# ---------------------------------------------------------------------------

def _classify_phase_progress(progress: float) -> str:
    """把 0-1 进度归到"早期 / 中期 / 晚期"标签。"""
    if progress < 0.33:
        return "早期"
    if progress < 0.67:
        return "中期"
    return "晚期"


def _compute_phase_progress(
    cycle_name: str,
    main_score: float,
    metrics: Dict[str, Any],
) -> float:
    """估算当前周期的相位进度。

    实现思路：
    - 主周期得分越接近 100 → 该周期表现越"典型/强烈"
    - 同时考虑关键 metrics 的极端度（涨停数 / 连板高度 / 炸板率）
    - 输出 0-1 的归一化进度

    特殊处理（每个周期的"进度"含义不同）：
    - 高潮期：得分越高 → 越晚期（高潮顶点 = 最危险点）
    - 上升期：得分越高 → 越偏中期（健康状态）；下降趋势 → 晚期
    - 退潮期：得分越高 → 越晚期（最差的退潮 = 最深的冰点边缘）
    - 冰点期：得分越高 → 越晚期（最冷点 = 反弹前夜）
    """
    # 主周期得分占 70% 权重
    score_progress = min(1.0, max(0.0, main_score / 100.0))

    # 关键指标加成 30% 权重
    limit_up = float(metrics.get("limit_up_count", 0) or 0)
    max_height = float(metrics.get("max_board_height", 0) or 0)
    broken_rate = float(metrics.get("broken_rate", 0) or 0)
    nuclear = float(metrics.get("nuclear_button_count", 0) or 0)

    metric_progress = 0.5
    if cycle_name == "高潮期":
        # 涨停越多、连板越高、炸板率越低 → 越晚期
        metric_progress = min(1.0, (limit_up / 120.0) * 0.5 + (max_height / 10.0) * 0.5)
    elif cycle_name == "上升期":
        # 上升期：涨停 50-100 家区间，越接近 100 越晚期
        if limit_up <= 50:
            metric_progress = 0.0
        elif limit_up >= 100:
            metric_progress = 1.0
        else:
            metric_progress = (limit_up - 50) / 50.0
    elif cycle_name == "震荡期":
        # 震荡期看炸板率：炸板率越高 → 越向退潮转
        metric_progress = min(1.0, broken_rate / 60.0)
    elif cycle_name == "退潮期":
        # 退潮越深 → 越晚期；用核按钮数量
        metric_progress = min(1.0, nuclear / 30.0)
    elif cycle_name == "冰点期":
        # 涨停越少、连板越低 → 越晚期（接近反弹）
        metric_progress = 1.0 - min(1.0, limit_up / 20.0)

    return round(0.7 * score_progress + 0.3 * metric_progress, 3)


def _compute_transition_warning(
    cycle_name: str,
    next_cycle_name: str,
    score_gap: float,
) -> str:
    """生成转换预警文本。

    阈值经验值：
    - gap < 8：很可能即将转换 → "警惕"
    - gap < 15：有转换风险 → "关注"
    - 否则：稳定
    """
    if score_gap < 8:
        return f"⚠ 警惕向「{next_cycle_name}」转换（次分 vs 主分 gap={score_gap:.1f}）"
    if score_gap < 15:
        return f"关注「{next_cycle_name}」信号（gap={score_gap:.1f}）"
    return "周期稳定"


def analyze_emotion_phase(emotion_result: Dict[str, Any]) -> Optional[EmotionPhaseResult]:
    """对 emotion_result 做相位分析。

    Args:
        emotion_result: ``EmotionCycleEngine.analyze`` 的返回值，
                        需含 ``cycle_name`` / ``scores`` / ``metrics`` 三键

    Returns:
        ``EmotionPhaseResult``，输入异常时返回 None
    """
    if not emotion_result or not isinstance(emotion_result, dict):
        return None

    cycle_name = emotion_result.get("cycle_name", "")
    scores = emotion_result.get("scores", {}) or {}
    metrics = emotion_result.get("metrics", {}) or {}

    if not cycle_name or not scores:
        return None

    # scores 的 key 可能是英文（"boom"/"rise"/...）也可能直接是中文，做兼容
    name_score_map: Dict[str, float] = {}
    for k, v in scores.items():
        if v is None:
            continue
        try:
            v = float(v)
        except (ValueError, TypeError):
            continue
        if k in _SCORE_KEY_TO_NAME:
            name_score_map[_SCORE_KEY_TO_NAME[k]] = v
        else:
            name_score_map[k] = v

    if not name_score_map:
        return None

    main_score = name_score_map.get(cycle_name, 0.0)

    # 次高分对应的周期（排除主周期本身）
    sorted_others = sorted(
        [(name, sc) for name, sc in name_score_map.items() if name != cycle_name],
        key=lambda x: -x[1],
    )
    if sorted_others:
        next_cycle_name, next_score = sorted_others[0]
    else:
        next_cycle_name, next_score = "", 0.0

    score_gap = main_score - next_score
    phase_progress = _compute_phase_progress(cycle_name, main_score, metrics)
    phase_label = _classify_phase_progress(phase_progress)
    warning = _compute_transition_warning(cycle_name, next_cycle_name, score_gap)

    return EmotionPhaseResult(
        cycle_name=cycle_name,
        phase_progress=phase_progress,
        phase_label=phase_label,
        transition_warning=warning,
        next_likely_cycle=next_cycle_name,
        main_score=main_score,
        next_score=next_score,
        score_gap=score_gap,
    )


__all__ = [
    "EmotionPhaseResult",
    "analyze_emotion_phase",
]
