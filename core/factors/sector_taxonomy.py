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
    # Broad classification indices describe where a stock belongs. They are
    # not a tradable narrative and must not be presented as a market mainline.
    "制造业指数",
    "农业指数",
    "工业指数",
    "服务业指数",
    "消费指数",
    "综合指数",
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

# Concentration control needs a coarser vocabulary than the source sector
# labels.  For example, 创新药、CRO and 医疗研发外包 are different concepts,
# but they express the same portfolio risk when they dominate one decision
# pool.  Keep this mapping deliberately small and risk-oriented; unmatched
# themes retain their original label.
THEME_CLUSTER_KEYWORDS = (
    ("医药医疗", ("医药", "医疗", "创新药", "仿制药", "生物制药", "生物医药", "CRO", "CXO", "疫苗", "中药", "原料药", "细胞治疗", "免疫治疗")),
    ("芯片半导体", ("芯片", "半导体", "集成电路", "光刻", "存储", "封装", "先进制程", "CPO")),
    ("人工智能", ("人工智能", "AI应用", "AI智能体", "大模型", "算力", "数据要素", "机器视觉")),
    ("机器人", ("机器人", "减速器", "自动化设备", "人形机器人")),
    ("新能源", ("新能源", "光伏", "风电", "储能", "锂电", "固态电池", "充电桩")),
    ("汽车产业链", ("汽车", "智能驾驶", "无人驾驶", "汽车零部件")),
    ("军工航天", ("军工", "航天", "卫星", "商业航天", "低空经济", "航空装备")),
    ("消费", ("消费", "食品饮料", "白酒", "零售", "旅游", "酒店", "家电")),
    ("金融地产", ("证券", "券商", "银行", "保险", "房地产", "多元金融")),
)


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


def theme_cluster_name(names: Iterable[object]) -> str:
    """Return a stable risk cluster for a stock's industry/concept labels."""
    themes, _ = partition_sector_names(names)
    for cluster, keywords in THEME_CLUSTER_KEYWORDS:
        if any(keyword.lower() in theme.lower() for theme in themes for keyword in keywords):
            return cluster
    return themes[0] if themes else ""


__all__ = [
    "NON_THEME_EXACT",
    "THEME_CLUSTER_KEYWORDS",
    "is_trade_theme_sector",
    "partition_sector_names",
    "theme_cluster_name",
]
