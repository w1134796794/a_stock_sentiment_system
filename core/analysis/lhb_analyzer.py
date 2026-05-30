"""Sprint F-3：龙虎榜 / 游资分析器

把 ``hm_detail`` 当日明细 + ``hm_reputation`` 信誉名录聚合成两张可消费的画像：

1. **个股席位画像** ``StockLHBProfile`` —— 每只上榜股票：
     * 买方 / 卖方席位列表（含信誉标签）
     * 信誉加权净买入（好游资买 +，坏游资买 −）
     * ``has_bad_buyer`` / ``has_good_buyer`` 两个布尔，供 Sprint A 分歧日 & 龙头池用
2. **板块共识度** ``SectorLHBProfile`` —— 同板块多个游资同时买 → 真主线信号

设计取向
========
* **核心匹配逻辑是纯函数**（``build_stock_matrix`` 接收 DataFrame），便于离线单测，
  不依赖网络。
* ``analyze_lhb(dm, trade_date, ...)`` 是编排入口：拉数据 → 匹配 → 返回 ``LHBResult``。
* **降级**：``hm_detail`` 为空（积分不足）时，``LHBResult.available=False``，
  上层据此跳过相关展示，不报错。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd
import loguru

from core.analysis.hm_reputation import (
    HotMoneyReputationRegistry,
    ReputationLookup,
)

logger = loguru.logger


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class SeatRecord:
    """单条游资席位记录（hm_detail 的一行 + 信誉）。"""
    hm_name: str
    hm_orgs: str
    buy_amount: float
    sell_amount: float
    net_amount: float
    score: float
    label: str          # 白 / 灰 / 黑 / 未知
    tag: str = ""

    @property
    def is_buyer(self) -> bool:
        return self.net_amount > 0

    def desc(self) -> str:
        side = "买" if self.is_buyer else "卖"
        wan = self.net_amount / 1e4
        return f"{self.hm_name}[{self.label}{self.score:.0f}]{side}{wan:+.0f}万"


@dataclass
class StockLHBProfile:
    """单只上榜股票的游资画像。"""
    ts_code: str
    ts_name: str = ""
    seats: List[SeatRecord] = field(default_factory=list)

    @property
    def buyers(self) -> List[SeatRecord]:
        return [s for s in self.seats if s.is_buyer]

    @property
    def sellers(self) -> List[SeatRecord]:
        return [s for s in self.seats if not s.is_buyer]

    @property
    def reputation_weighted_net(self) -> float:
        """信誉加权净买入：sum(net * (score-50)/50)。

        好游资(>50)买入为正、坏游资(<50)买入为负；卖出符号自然反转。
        单位同 net_amount（元）。
        """
        return sum(s.net_amount * (s.score - 50.0) / 50.0 for s in self.seats)

    @property
    def has_bad_buyer(self) -> bool:
        """是否有"黑名单"游资在买方 —— Sprint A 分歧日的额外触发证据。"""
        return any(s.is_buyer and s.label == "黑" for s in self.buyers)

    @property
    def has_good_buyer(self) -> bool:
        return any(s.is_buyer and s.label == "白" for s in self.buyers)

    @property
    def total_net(self) -> float:
        return sum(s.net_amount for s in self.seats)

    def seats_summary(self, top: int = 4) -> str:
        """报告用：'章盟主[白85]买+1200万 / 拉萨天团[黑30]买+300万'。"""
        ordered = sorted(self.seats, key=lambda s: -abs(s.net_amount))
        return " / ".join(s.desc() for s in ordered[:top]) if ordered else "--"


@dataclass
class SectorLHBProfile:
    """板块游资共识度。"""
    sector: str
    stock_count: int = 0          # 该板块当日有几只票上游资榜
    distinct_hm_count: int = 0    # 涉及多少个不同游资
    good_hm_count: int = 0        # 其中白名单游资数
    net_buy_total: float = 0.0    # 板块合计净买入

    @property
    def consensus_level(self) -> str:
        """共识度档位：高 / 中 / 低。"""
        if self.distinct_hm_count >= 3 and self.net_buy_total > 0:
            return "高"
        if self.distinct_hm_count >= 2:
            return "中"
        return "低"


@dataclass
class LHBResult:
    """龙虎榜分析总结果。"""
    trade_date: str
    available: bool = False                       # 游资明细是否拿到（积分/降级标志）
    stock_profiles: Dict[str, StockLHBProfile] = field(default_factory=dict)  # key=ts_code
    sector_profiles: Dict[str, SectorLHBProfile] = field(default_factory=dict)
    reputation_version: str = ""

    def get_stock(self, code: str) -> Optional[StockLHBProfile]:
        """按代码取画像，自动兼容带/不带 .SH/.SZ 后缀。"""
        if not code:
            return None
        c = str(code).strip()
        if c in self.stock_profiles:
            return self.stock_profiles[c]
        # 兼容裸 6 位
        if "." not in c and len(c) == 6:
            suf = "SH" if c[:1] in ("6", "9") else "SZ"
            return self.stock_profiles.get(f"{c}.{suf}")
        # 兼容反向（带后缀查裸码）
        return self.stock_profiles.get(c.split(".")[0])


# ---------------------------------------------------------------------------
# 核心匹配（纯函数，可离线单测）
# ---------------------------------------------------------------------------

def _f(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def build_stock_matrix(
    hm_detail_df: pd.DataFrame,
    registry: HotMoneyReputationRegistry,
) -> Dict[str, StockLHBProfile]:
    """从 ``hm_detail`` 明细 DataFrame 构建 {ts_code: StockLHBProfile}。

    期望列：ts_code, ts_name, hm_name, hm_orgs, buy_amount, sell_amount, net_amount
    缺列时对应字段取默认值（0 / ""），不抛异常。
    """
    profiles: Dict[str, StockLHBProfile] = {}
    if hm_detail_df is None or hm_detail_df.empty:
        return profiles

    for _, row in hm_detail_df.iterrows():
        code = str(row.get("ts_code", "") if hasattr(row, "get") else row["ts_code"]).strip()
        if not code:
            continue
        hm_name = str(row.get("hm_name", "") or "")
        hm_orgs = str(row.get("hm_orgs", "") or "")
        look: ReputationLookup = registry.lookup(hm_name=hm_name, org_text=hm_orgs)

        seat = SeatRecord(
            hm_name=hm_name or "(未知席位)",
            hm_orgs=hm_orgs,
            buy_amount=_f(row.get("buy_amount")),
            sell_amount=_f(row.get("sell_amount")),
            net_amount=_f(row.get("net_amount")),
            score=look.score,
            label=look.label,
            tag=look.tag,
        )

        prof = profiles.get(code)
        if prof is None:
            prof = StockLHBProfile(ts_code=code, ts_name=str(row.get("ts_name", "") or ""))
            profiles[code] = prof
        prof.seats.append(seat)

    return profiles


def build_sector_concentration(
    stock_profiles: Dict[str, StockLHBProfile],
    code_to_sector: Dict[str, str],
) -> Dict[str, SectorLHBProfile]:
    """聚合板块共识度。

    Args:
        stock_profiles: build_stock_matrix 的输出
        code_to_sector: {ts_code(可裸码) : 板块名}，由 Layer2/zt_pool 提供
    """
    by_sector: Dict[str, SectorLHBProfile] = {}
    sector_hm_names: Dict[str, set] = defaultdict(set)
    sector_good_names: Dict[str, set] = defaultdict(set)

    def _sector_of(code: str) -> Optional[str]:
        if code in code_to_sector:
            return code_to_sector[code]
        bare = code.split(".")[0]
        return code_to_sector.get(bare)

    for code, prof in stock_profiles.items():
        sector = _sector_of(code)
        if not sector:
            continue
        sp = by_sector.get(sector)
        if sp is None:
            sp = SectorLHBProfile(sector=sector)
            by_sector[sector] = sp
        sp.stock_count += 1
        sp.net_buy_total += prof.total_net
        for s in prof.seats:
            sector_hm_names[sector].add(s.hm_name)
            if s.label == "白":
                sector_good_names[sector].add(s.hm_name)

    for sector, sp in by_sector.items():
        sp.distinct_hm_count = len(sector_hm_names[sector])
        sp.good_hm_count = len(sector_good_names[sector])

    return by_sector


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------

def analyze_lhb(
    dm,
    trade_date: str,
    *,
    code_to_sector: Optional[Dict[str, str]] = None,
    registry: Optional[HotMoneyReputationRegistry] = None,
) -> LHBResult:
    """拉取当日游资明细 + 匹配信誉 → 返回 ``LHBResult``。

    降级：``hm_detail`` 空（积分不足/无 token）时 available=False，stock_profiles 为空。
    任何异常都被吞掉并返回 available=False 的结果，绝不拖垮主流水线。
    """
    if registry is None:
        registry = HotMoneyReputationRegistry.load()

    result = LHBResult(trade_date=trade_date, reputation_version=registry.version)

    try:
        from core.data.lhb_data import HotMoneyDataProvider
        provider = HotMoneyDataProvider(dm)
        detail = provider.get_hm_detail(trade_date)
    except Exception as e:
        logger.warning(f"[analyze_lhb] {trade_date} 拉取游资明细失败: {e}")
        return result

    if detail is None or detail.empty:
        logger.info(f"[analyze_lhb] {trade_date} 无游资明细（降级到仅名单模式）")
        return result

    result.available = True
    result.stock_profiles = build_stock_matrix(detail, registry)
    if code_to_sector:
        result.sector_profiles = build_sector_concentration(
            result.stock_profiles, code_to_sector
        )

    logger.info(
        f"[analyze_lhb] {trade_date} 游资上榜股 {len(result.stock_profiles)} 只，"
        f"板块 {len(result.sector_profiles)} 个"
    )
    return result


__all__ = [
    "SeatRecord",
    "StockLHBProfile",
    "SectorLHBProfile",
    "LHBResult",
    "build_stock_matrix",
    "build_sector_concentration",
    "analyze_lhb",
]
