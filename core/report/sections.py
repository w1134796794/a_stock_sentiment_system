"""
报告分节协议（P3-6）

把 `ReportGeneratorV2` 的 10 个 `_write_xxx` 方法抽象成可插拔的 `ReportSection`：
- 单一职责：每个 section 负责一个 Sheet
- 可枚举：`create_daily_report` 退化为遍历 sections
- 可扩展：业务方可以子类化新增 section，不需要改 `ReportGeneratorV2`

兼容策略：
- `LegacyMethodSection` 直接复用 `ReportGeneratorV2._write_xxx`，存量代码不动
- 后续新模块可以直接实现 `ReportSection`，写自己的 render

用法：
    from core.report.sections import default_sections
    gen = ReportGeneratorV2(output_dir)
    gen.create_daily_report(data, sections=default_sections())
"""
from __future__ import annotations

from typing import Any, Callable, List, Protocol, runtime_checkable

import loguru

logger = loguru.logger


@runtime_checkable
class ReportSection(Protocol):
    """报告分节协议。实现类必须提供 sheet_name 与 render(writer, data, formats)."""

    sheet_name: str

    def render(self, writer: Any, data: dict, formats: dict) -> None:
        ...


class BaseSection:
    """便于自定义 section 时继承的基类。"""

    sheet_name: str = ""

    def render(self, writer: Any, data: dict, formats: dict) -> None:  # pragma: no cover
        raise NotImplementedError


class LegacyMethodSection(BaseSection):
    """
    把 `ReportGeneratorV2._write_xxx` 方法包装成 ReportSection。

    避免大改既有渲染代码：保留每个 `_write_xxx` 的内部实现，只在外层加一层
    协议适配，使 `create_daily_report` 能用统一的 `for section: section.render(...)` 模式。
    """

    def __init__(self, sheet_name: str,
                 method: Callable[[Any, dict, dict], None]):
        self.sheet_name = sheet_name
        self._method = method

    def render(self, writer: Any, data: dict, formats: dict) -> None:
        try:
            self._method(writer, data, formats)
        except Exception as e:
            logger.warning(f"[Report] section '{self.sheet_name}' 渲染失败: {e}")


def default_sections(generator) -> List[ReportSection]:
    """
    返回 `ReportGeneratorV2` 当前默认的 sections。

    布局逻辑：
    1. 决策入口：市场概览 → 今日操作清单（一眼看全今天买什么）
    2. 信号细节：4 大模式 + 涨停梯队 + 概念梯队 + 龙头/走弱池
    3. 交易计划 + 复盘总结（Layer4/Layer5）
    4. 因子总览（A/B/C/D/E/F 一表概览）
    5. 因子原始数据（审计/喂 LLM/回测）

    Args:
        generator: ReportGeneratorV2 实例（用于绑定 _write_xxx 方法）
    """
    spec = [
        ("市场概览",       "_write_dashboard"),
        ("今日操作清单",   "_write_action_list"),
        ("热点概念",       "_write_hot_sectors"),
        ("首板突破",       "_write_first_board"),
        ("二板定龙",       "_write_second_board"),
        ("弱转强",         "_write_weak_to_strong"),
        ("龙头二波",       "_write_dragon_second_wave"),
        ("涨停梯队",       "_write_limit_up_hierarchy"),
        ("概念连板梯队",   "_write_concept_hierarchy"),
        ("龙头池",         "_write_dragon_pool"),
        ("龙虎榜",         "_write_lhb"),
        ("走弱池",         "_write_weakening_pool"),
        ("交易计划",       "_write_trade_plans"),
        ("风控闸门",       "_write_risk_gate"),
        ("复盘总结",       "_write_review"),
        ("周期模式胜率",   "_write_cycle_pattern_matrix"),
        ("因子总览",       "_write_factor_dashboard"),
        ("因子原始数据",   "_write_factor_raw"),
    ]
    sections: List[ReportSection] = []
    for sheet_name, method_name in spec:
        method = getattr(generator, method_name, None)
        if method is None:
            logger.debug(f"[Report] 跳过 section '{sheet_name}' —— 缺方法 {method_name}")
            continue
        sections.append(LegacyMethodSection(sheet_name, method))
    return sections


__all__ = ["ReportSection", "BaseSection", "LegacyMethodSection", "default_sections"]

