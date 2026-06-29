"""筛选与回测共用的可组合增强项定义。"""
from __future__ import annotations

from typing import Any, Iterable, List


ENHANCEMENT_DEFINITIONS = {
    "capital_flow": {"label": "资金流共识", "column": "capital_flow_adjustment"},
    "attention": {"label": "市场热度共识", "column": "attention_adjustment"},
    "leader": {"label": "龙头资金确认", "column": "leader_adjustment"},
    "margin": {"label": "融资加速度", "column": "margin_adjustment"},
    "risk": {"label": "事件风险惩罚", "column": "risk_adjustment"},
}


def normalize_enhancements(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str):
        source: Iterable[Any] = values.replace("+", ",").split(",")
    elif isinstance(values, (list, tuple, set)):
        source = values
    else:
        source = []
    selected = {str(value).strip() for value in source}
    return [key for key in ENHANCEMENT_DEFINITIONS if key in selected]


def enhancement_label(values: Any) -> str:
    selected = normalize_enhancements(values)
    if not selected:
        return "基线"
    labels = [str(ENHANCEMENT_DEFINITIONS[key]["label"]) for key in selected]
    return "基线 + " + " + ".join(labels)


def enhancement_slug(values: Any) -> str:
    selected = normalize_enhancements(values)
    return "baseline" if not selected else "baseline_" + "_".join(selected)
