"""Presentation-only summaries of the exact rows already supplied to the page."""
from __future__ import annotations

from collections import Counter


def data_highlights(category, sections):
    by_name = {section.get("name"): section.get("rows") or [] for section in sections}
    if category == "limitup":
        rows = by_name.get("涨停梯队") or []
        if not rows:
            return None
        levels = Counter()
        normalized = []
        for row in rows:
            try:
                raw = float(row.get("连板数"))
                if not raw.is_integer():
                    continue
                level = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if level > 0:
                levels[level] += 1
                normalized.append((row, level))
        height = max(levels, default=None)
        leaders = [str(row.get("股票名称") or row.get("股票代码") or "") for row, level in normalized if level == height]
        return {"label": "涨停结构", "headline": f"最高 {height} 板" if height else "连板高度待核验",
                "detail": "高度梯队：" + "、".join(leaders[:4]) if leaders else "部分连板字段缺失",
                "metrics": [("涨停家数", len(rows), "本页涨停池"),
                            ("连板家数", sum(count for level, count in levels.items() if level >= 2) if levels else "--", "已知二板及以上"),
                            ("首板家数", levels.get(1, 0) if levels else "--", "已知一板"),
                            ("概念梯队", len(by_name["概念连板梯队"]) if "概念连板梯队" in by_name else "--", "本页已展示概念")]}
    if category == "sector":
        mainline = by_name.get("主线主题") or []
        concepts = by_name.get("热点概念") or []
        industries = by_name.get("热点行业") or []
        if not (mainline or concepts or industries):
            return None
        if any(row.get("_review_version") for row in mainline + concepts):
            qualified = [row for row in mainline if row.get("_review_qualified")]
            watching = [row for row in mainline if row.get("_review_status") == "轮动观察"]
            first = (qualified or mainline or concepts or industries)[0]
            name = first.get("sector_name") or first.get("板块名称") or "暂无持续主线"
            return {"label": "持续主线" if qualified else "主线待确认", "headline": str(name) if qualified else "轮动观察",
                    "detail": "近10个交易日 · 复盘评分，非盘中买点",
                    "metrics": [("持续主线", len(qualified), "证据达标且评分达标"),
                                ("轮动观察", len(watching), "证据充分，尚未达标"),
                                ("当日热点", len(concepts), "独立概念折叠展示"),
                                ("证据不足", sum(r.get("_review_status") == "数据不足" for r in mainline), "不按零分处理")]}
        first = (mainline or concepts or industries)[0]
        name = first.get("sector_name") or first.get("板块名称") or first.get("名称") or "板块名称待核验"
        return {"label": "板块主线" if mainline else "板块热点", "headline": str(name), "detail": "当前归档排序首位 · 非盘中买点确认",
                "metrics": [("主线主题", len(mainline) if "主线主题" in by_name else "--", "当前视图"), ("热点概念", len(concepts) if "热点概念" in by_name else "--", "当前视图"),
                            ("热点行业", len(industries) if "热点行业" in by_name else "--", "当前视图"),
                            ("持续性视图", sum(name in by_name for name in ("概念持续性", "行业持续性")), "已生成视图数")]}
    return None
