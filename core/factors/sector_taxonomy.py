"""Classify sector labels as tradable themes or non-theme stock attributes."""
from __future__ import annotations

from typing import Iterable, List, Tuple


# These labels describe eligibility, ownership, index membership or market access.
# They may be useful stock attributes, but they are not narratives that can lead
# sector diffusion and therefore must not be promoted to a trading mainline.
NON_THEME_EXACT = frozenset({
    "融资融券",
    "融资标的",
    "融券标的",
    "转融券标的",
    "沪股通",
    "深股通",
    "沪深股通",
    "港股通",
    "陆股通",
    "AH股",
    "AB股",
    "QFII重仓",
    "社保重仓",
    "保险重仓",
    "基金重仓",
    "机构重仓",
    "券商重仓",
    "证金持股",
    "国家队持股",
    "中央汇金持股",
    "国家大基金持股",
    "MSCI概念",
    "富时罗素概念",
    "标普道琼斯A股",
    "同花顺漂亮100",
    "高股息精选",
    "破净股",
    "低价股",
    "百元股",
    "小盘股",
    "大盘股",
    "绩优股",
})

NON_THEME_FRAGMENTS = (
    "昨日涨停表现",
    "昨日连板",
    "昨日非ST",
    "昨日打板",
    "成份股",
    "成分股",
    "指数样本",
)

TRADE_SECTOR_TYPES = frozenset({"N", "I", "概念", "行业"})


def is_trade_theme_sector(name: object, sector_type: object = "") -> bool:
    """Return whether a label can represent a market theme or industry mainline."""
    label = str(name or "").strip()
    kind = str(sector_type or "").strip().upper()
    if not label:
        return False
    if kind and kind not in TRADE_SECTOR_TYPES:
        return False
    if label in NON_THEME_EXACT:
        return False
    return not any(fragment in label for fragment in NON_THEME_FRAGMENTS)


def partition_sector_names(names: Iterable[object]) -> Tuple[List[str], List[str]]:
    """Split ordered labels into tradable themes and non-theme attributes."""
    themes: List[str] = []
    attributes: List[str] = []
    seen = set()
    for value in names:
        label = str(value or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        target = themes if is_trade_theme_sector(label) else attributes
        target.append(label)
    return themes, attributes


__all__ = [
    "NON_THEME_EXACT",
    "is_trade_theme_sector",
    "partition_sector_names",
]
