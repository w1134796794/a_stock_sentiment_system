"""Sprint F-8：龙虎榜板块游资共识度 → Layer2 主线评分加权

**多个游资同板块共同买入**是"真主线 / 真资金"的强证据；反之多个游资同板块
**净卖出（派发）**是主线降温的预警。本模块把 ``SectorLHBProfile`` 的板块共识度
折算成对 ``main_themes_df['综合评分']`` 的加减分，并重排主线榜。

设计取向
========
* **纯函数**：输入 ``main_themes_df`` + ``sector_profiles``，返回 (新 df, 调整明细)，
  不触网、便于离线单测。
* **降级安全**：``sector_profiles`` 为空 / df 为空 → 原样返回，空明细。
* **板块名对齐**：``main_themes_df['板块名称']``（同花顺概念/行业名）与
  ``SectorLHBProfile.sector``（由 zt_pool 行业/概念构造）都源自同花顺口径，
  采用"精确 → 归一化 → 双向包含"三级匹配，并记录命中率。
* **方向敏感**：净买入才加分；多游资净卖出（派发）则减分，避免给被砸的板块加权。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import loguru

logger = loguru.logger


# ---------------------------------------------------------------------------
# 配置 & 明细
# ---------------------------------------------------------------------------

@dataclass
class LHBSectorBoostConfig:
    """板块共识度加权参数（保守默认）。"""

    high_bonus: float = 8.0       # 高共识（≥3 游资净买）综合分加分
    mid_bonus: float = 4.0        # 中共识（≥2 游资净买）加分
    distribute_penalty: float = -4.0  # 派发（≥2 游资净卖）减分

    good_hm_extra: float = 1.5    # 每个白名单游资额外加分
    good_hm_extra_cap: float = 4.0

    high_min_hm: int = 3          # 高共识所需不同游资数
    mid_min_hm: int = 2           # 中共识所需不同游资数

    score_floor: float = 0.0
    score_cap: float = 100.0


@dataclass
class LHBSectorBoost:
    """一条板块加权记录。"""

    sector: str
    consensus: str                # 高 / 中 / 派发 / 低
    distinct_hm_count: int = 0
    good_hm_count: int = 0
    net_buy_total: float = 0.0
    score_before: float = 0.0
    score_after: float = 0.0
    delta: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "板块": self.sector,
            "游资共识": self.consensus,
            "上榜游资数": self.distinct_hm_count,
            "白名单数": self.good_hm_count,
            "净买入(万)": round(self.net_buy_total / 1e4, 0),
            "综合分": f"{self.score_before:.1f}→{self.score_after:.1f}",
            "加分": f"{self.delta:+.1f}",
            "说明": self.note,
        }


# ---------------------------------------------------------------------------
# 板块名匹配
# ---------------------------------------------------------------------------

def _norm(s: Any) -> str:
    return str(s or "").strip().replace(" ", "")


def _match_profile(name: str, profiles: Dict[str, Any],
                   norm_index: Dict[str, Any]) -> Optional[Any]:
    """三级匹配：精确 → 归一化 → 双向包含。"""
    if not name:
        return None
    if name in profiles:
        return profiles[name]
    n = _norm(name)
    if n in norm_index:
        return norm_index[n]
    # 双向包含（取最长板块名优先，减少误配）
    best = None
    best_len = 0
    for key_norm, prof in norm_index.items():
        if not key_norm:
            continue
        if (key_norm in n or n in key_norm) and len(key_norm) > best_len:
            best = prof
            best_len = len(key_norm)
    return best


# ---------------------------------------------------------------------------
# 分类（纯函数）
# ---------------------------------------------------------------------------

def classify_sector(profile: Any, config: LHBSectorBoostConfig) -> Tuple[str, float, str]:
    """板块共识度 → (档位, 加减分, 说明)。

    规则（方向敏感）：
      * ≥high_min_hm 个不同游资 且 净买入>0 → '高'，+high_bonus
      * ≥mid_min_hm  个不同游资 且 净买入>0 → '中'，+mid_bonus
      * ≥mid_min_hm  个不同游资 且 净买入<0 → '派发'，distribute_penalty
      * 其它 → '低'，0
    净买入为正时再按白名单游资数追加 good_hm_extra（封顶）。
    """
    if profile is None:
        return "低", 0.0, ""

    n_hm = int(getattr(profile, "distinct_hm_count", 0) or 0)
    n_good = int(getattr(profile, "good_hm_count", 0) or 0)
    net = float(getattr(profile, "net_buy_total", 0.0) or 0.0)

    if n_hm >= config.high_min_hm and net > 0:
        bonus = config.high_bonus
        bonus += min(n_good * config.good_hm_extra, config.good_hm_extra_cap)
        return "高", bonus, f"✓ {n_hm}家游资共识净买(白名单{n_good}) → 真主线加权"
    if n_hm >= config.mid_min_hm and net > 0:
        bonus = config.mid_bonus
        bonus += min(n_good * config.good_hm_extra, config.good_hm_extra_cap)
        return "中", bonus, f"✓ {n_hm}家游资净买 → 主线加权"
    if n_hm >= config.mid_min_hm and net < 0:
        return "派发", config.distribute_penalty, f"⚠ {n_hm}家游资净卖 → 主线派发降温"
    return "低", 0.0, ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def boost_main_themes(
    main_themes_df: pd.DataFrame,
    sector_profiles: Dict[str, Any],
    config: Optional[LHBSectorBoostConfig] = None,
) -> Tuple[pd.DataFrame, List[LHBSectorBoost]]:
    """按游资共识度调整主线 ``综合评分`` 并重排。

    Returns:
        (新 DataFrame, 调整明细列表)。无可调整时返回原 df 的拷贝 + 空列表。
        新 df 追加列：``游资共识`` / ``上榜游资数`` / ``游资加分``，并按 ``综合评分``
        重排、重写 ``排名``。
    """
    config = config or LHBSectorBoostConfig()

    if main_themes_df is None or main_themes_df.empty:
        return main_themes_df, []
    if not sector_profiles:
        return main_themes_df, []
    if "板块名称" not in main_themes_df.columns or "综合评分" not in main_themes_df.columns:
        logger.debug("[LHB-Sector] main_themes_df 缺少 板块名称/综合评分 列，跳过")
        return main_themes_df, []

    df = main_themes_df.copy()
    norm_index = {_norm(k): v for k, v in sector_profiles.items()}

    boosts: List[LHBSectorBoost] = []
    consensus_col: List[str] = []
    count_col: List[int] = []
    delta_col: List[float] = []
    matched = 0

    for _, row in df.iterrows():
        name = row.get("板块名称", "")
        prof = _match_profile(name, sector_profiles, norm_index)
        if prof is None:
            consensus_col.append("--")
            count_col.append(0)
            delta_col.append(0.0)
            continue

        matched += 1
        consensus, delta, note = classify_sector(prof, config)
        before = float(row.get("综合评分", 0.0) or 0.0)
        after = max(config.score_floor, min(config.score_cap, before + delta))

        consensus_col.append(consensus)
        count_col.append(int(getattr(prof, "distinct_hm_count", 0) or 0))
        delta_col.append(round(after - before, 1))

        if abs(after - before) > 1e-9:
            boosts.append(LHBSectorBoost(
                sector=str(name),
                consensus=consensus,
                distinct_hm_count=int(getattr(prof, "distinct_hm_count", 0) or 0),
                good_hm_count=int(getattr(prof, "good_hm_count", 0) or 0),
                net_buy_total=float(getattr(prof, "net_buy_total", 0.0) or 0.0),
                score_before=before,
                score_after=after,
                delta=after - before,
                note=note,
            ))

    df["游资共识"] = consensus_col
    df["上榜游资数"] = count_col
    df["游资加分"] = delta_col
    # 应用加分到综合评分
    df["综合评分"] = (df["综合评分"].astype(float) + df["游资加分"].astype(float)).clip(
        lower=config.score_floor, upper=config.score_cap
    ).round(1)

    # 重排 + 重写排名
    df = df.sort_values("综合评分", ascending=False).reset_index(drop=True)
    if "排名" in df.columns:
        df["排名"] = range(1, len(df) + 1)

    if boosts:
        n_up = sum(1 for b in boosts if b.delta > 0)
        n_down = sum(1 for b in boosts if b.delta < 0)
        logger.info(
            f"[LHB-Sector] 主线游资共识调整 {len(boosts)} 个板块"
            f"（加权 {n_up} / 派发降温 {n_down}），"
            f"匹配率 {matched}/{len(df)}"
        )
        for b in boosts:
            logger.info(
                f"[LHB-Sector] {b.sector} {b.note} | "
                f"综合分 {b.score_before:.1f}→{b.score_after:.1f}({b.delta:+.1f})"
            )
    else:
        logger.debug(f"[LHB-Sector] 无板块达到共识阈值（匹配 {matched}/{len(df)}）")

    return df, boosts


__all__ = [
    "LHBSectorBoostConfig",
    "LHBSectorBoost",
    "classify_sector",
    "boost_main_themes",
]
