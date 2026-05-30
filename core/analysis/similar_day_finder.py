"""Sprint E-2：历史相似日 KNN 匹配器

业务问题
========
操盘手最实用的"经验"其实不是模式胜率，而是**"今天像哪一天"**：

> 「今天上证 +0.5%，涨停 80 家，高度 5 板，炸板率 30%——历史上长得最像的是
>   2025-11-12 那一天。那天次日大盘怎么走的？」

如果能从 60+ 个历史日里找出**Top 3 最相似日**，操盘手就能立刻拿到这些日
的「次日实际表现」作为参考——相当于一个**最简单可解释的"经验回放"**。

实现策略
========

* 特征向量（5 维 + 后续可扩展）：
    1. 涨停家数              limit_up_count
    2. 跌停家数              limit_down_count
    3. 最大连板高度          max_board_height
    4. 炸板率                broken_rate
    5. 大盘综合评分          composite_score
* 归一化：每个维度按全样本 z-score
* 距离：L2 欧氏距离
* Top-K：默认 K=3
* "次日表现"：用相似日的 T+1 涨跌幅（如果有 factor_results 关联） + ``next_day_outcome`` 概览描述

依赖 ``output/factor_results/factor_results_*.json`` 作为历史样本库。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import loguru

logger = loguru.logger


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class DaySnapshot:
    """单个交易日的特征快照。"""
    trade_date: str
    cycle_name: str = ""
    # 5 维特征
    limit_up_count: float = 0.0
    limit_down_count: float = 0.0
    max_board_height: float = 0.0
    broken_rate: float = 0.0
    composite_score: float = 50.0
    # 关联描述（next_day_outcome / 主线 etc.）
    main_themes: List[str] = field(default_factory=list)
    description: str = ""

    def vector(self) -> List[float]:
        return [
            self.limit_up_count,
            self.limit_down_count,
            self.max_board_height,
            self.broken_rate,
            self.composite_score,
        ]


@dataclass
class SimilarDay:
    """一个相似日匹配结果。"""
    trade_date: str
    cycle_name: str
    distance: float           # 归一化后的 L2 距离（越小越相似）
    description: str
    raw_metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class SimilarDayResult:
    """完整匹配结果。"""
    today_snapshot: DaySnapshot
    similar_days: List[SimilarDay]
    sample_pool_size: int     # 历史样本池规模


# ---------------------------------------------------------------------------
# JSON 解析：从 factor_results JSON 提取 DaySnapshot
# ---------------------------------------------------------------------------

def _parse_snapshot_from_json(raw: Dict[str, Any]) -> Optional[DaySnapshot]:
    """从 ``factor_results_YYYYMMDD.json`` 解析 DaySnapshot。"""
    trade_date = (raw.get("meta") or {}).get("trade_date", "")
    if not trade_date:
        return None

    l1 = raw.get("layer1_market_env") or {}
    emo = raw.get("emotion_cycle") or {}

    snapshot = DaySnapshot(
        trade_date=trade_date,
        cycle_name=emo.get("情绪周期", "") or "",
        limit_up_count=float((emo.get("原始统计") or {}).get("涨停家数", 0) or 0),
        limit_down_count=float((emo.get("原始统计") or {}).get("跌停家数", 0) or 0),
        max_board_height=float((emo.get("原始统计") or {}).get("最高连板", 0) or 0),
        broken_rate=float((emo.get("原始统计") or {}).get("炸板率", 0) or 0),
        composite_score=float(l1.get("综合评分", 50) or 50),
    )

    # description：用大盘评分 + 风险等级 + 主要主线一句话总结
    risk = l1.get("风险等级", "")
    position = l1.get("建议仓位", "")
    snapshot.description = (
        f"{snapshot.cycle_name} / 涨停{int(snapshot.limit_up_count)}只 "
        f"/ 最高{int(snapshot.max_board_height)}板 "
        f"/ 炸板率{snapshot.broken_rate:.0f}% / 综合{snapshot.composite_score:.0f}分"
    )
    if risk:
        snapshot.description += f" / 风险{risk}"
    if position:
        snapshot.description += f" / 建议{position}"

    # 主线
    l2 = raw.get("layer2_sector") or {}
    main_themes = []
    for theme in (l2.get("主线板块") or [])[:3]:
        if isinstance(theme, dict):
            main_themes.append(str(theme.get("板块名称") or theme.get("名称") or ""))
        else:
            main_themes.append(str(theme))
    snapshot.main_themes = [t for t in main_themes if t]

    return snapshot


# ---------------------------------------------------------------------------
# 主匹配函数
# ---------------------------------------------------------------------------

def _zscore_normalize(vectors: List[List[float]]) -> List[List[float]]:
    """对 N 行 D 维 vector 矩阵做按列 z-score 归一化。"""
    if not vectors:
        return []
    d = len(vectors[0])
    means = [0.0] * d
    stds = [0.0] * d
    n = len(vectors)
    for v in vectors:
        for i in range(d):
            means[i] += v[i]
    means = [m / n for m in means]
    for v in vectors:
        for i in range(d):
            stds[i] += (v[i] - means[i]) ** 2
    stds = [math.sqrt(s / n) if (s / n) > 0 else 1.0 for s in stds]

    normalized = []
    for v in vectors:
        normalized.append([(v[i] - means[i]) / stds[i] for i in range(d)])
    return normalized


def _l2_distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def find_similar_days(
    today_snapshot: DaySnapshot,
    *,
    factor_results_dir: Optional[Path] = None,
    top_k: int = 3,
    exclude_recent_days: int = 5,
    lookback_max: int = 200,
) -> SimilarDayResult:
    """从历史 factor_results 中找 ``today_snapshot`` 的 Top-K 相似日。

    Args:
        today_snapshot: 今日的快照（外部已构造好）
        factor_results_dir: JSON 目录，默认 ``output/factor_results``
        top_k: 取 Top K 相似（默认 3）
        exclude_recent_days: 排除最近 N 个交易日（避免"自己最像自己附近"）
        lookback_max: 最多扫描多少个历史 JSON（防止性能问题）

    Returns:
        ``SimilarDayResult``
    """
    if factor_results_dir is None:
        factor_results_dir = Path(__file__).parent.parent.parent / "output" / "factor_results"
    factor_results_dir = Path(factor_results_dir)

    if not factor_results_dir.exists():
        return SimilarDayResult(today_snapshot=today_snapshot, similar_days=[], sample_pool_size=0)

    # 列出 JSON
    files = sorted(factor_results_dir.glob("factor_results_*.json"))
    if not files:
        return SimilarDayResult(today_snapshot=today_snapshot, similar_days=[], sample_pool_size=0)
    files = files[-lookback_max:]

    # 解析全部样本（含 today）
    all_snapshots: List[DaySnapshot] = []
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            snap = _parse_snapshot_from_json(raw)
            if snap is not None:
                all_snapshots.append(snap)
        except Exception as e:
            logger.debug(f"[SimilarDay] 读取 {fp.name} 失败: {e}")
            continue

    if len(all_snapshots) < 2:
        return SimilarDayResult(today_snapshot=today_snapshot, similar_days=[], sample_pool_size=len(all_snapshots))

    # 排除最近 N 个交易日 + today
    today_date = today_snapshot.trade_date
    candidates = [
        s for s in all_snapshots
        if s.trade_date < today_date  # 严格早于今天
    ]
    # 按日期排序后去掉最近的 N 个
    candidates.sort(key=lambda s: s.trade_date)
    if exclude_recent_days > 0 and len(candidates) > exclude_recent_days:
        candidates = candidates[:-exclude_recent_days]

    if not candidates:
        return SimilarDayResult(today_snapshot=today_snapshot, similar_days=[], sample_pool_size=len(all_snapshots))

    # 把 today + candidates 一起归一化（保证同分布）
    vectors = [today_snapshot.vector()] + [c.vector() for c in candidates]
    normalized = _zscore_normalize(vectors)
    today_vec = normalized[0]
    cand_vecs = normalized[1:]

    # 计算距离并排序
    distances = [
        (cand, _l2_distance(today_vec, vec))
        for cand, vec in zip(candidates, cand_vecs)
    ]
    distances.sort(key=lambda x: x[1])

    top = distances[:top_k]
    similar_days = [
        SimilarDay(
            trade_date=cand.trade_date,
            cycle_name=cand.cycle_name,
            distance=round(dist, 3),
            description=cand.description,
            raw_metrics={
                "limit_up_count": cand.limit_up_count,
                "limit_down_count": cand.limit_down_count,
                "max_board_height": cand.max_board_height,
                "broken_rate": cand.broken_rate,
                "composite_score": cand.composite_score,
            },
        )
        for cand, dist in top
    ]

    logger.info(
        f"[SimilarDay] 今日={today_date}，候选池={len(candidates)}，"
        f"匹配到 Top {len(similar_days)}：" +
        ", ".join(f"{d.trade_date}(d={d.distance})" for d in similar_days)
    )

    return SimilarDayResult(
        today_snapshot=today_snapshot,
        similar_days=similar_days,
        sample_pool_size=len(candidates),
    )


# ---------------------------------------------------------------------------
# 便捷工厂：从 ctx 构造 today_snapshot
# ---------------------------------------------------------------------------

def build_today_snapshot_from_ctx(ctx: Any) -> Optional[DaySnapshot]:
    """从 SharedContext 提取今日 DaySnapshot。

    需要 ctx.market_env / ctx.emotion_result 至少有一个非空。
    """
    env = getattr(ctx, "market_env", None)
    emo = getattr(ctx, "emotion_result", {}) or {}

    trade_date = getattr(ctx, "trade_date", "")
    if not trade_date:
        return None

    metrics = emo.get("metrics", {}) or {}

    snapshot = DaySnapshot(
        trade_date=trade_date,
        cycle_name=emo.get("cycle_name", "") or "",
        limit_up_count=float(metrics.get("limit_up_count", 0) or 0),
        limit_down_count=float(getattr(env, "limit_down_count", 0) or 0),
        max_board_height=float(metrics.get("max_board_height", 0) or 0),
        broken_rate=float(metrics.get("broken_rate", 0) or 0),
        composite_score=float(getattr(env, "composite_score", 50) or 50),
    )

    risk = getattr(env, "risk_level", "")
    position = getattr(env, "suggested_position", "")
    snapshot.description = (
        f"{snapshot.cycle_name} / 涨停{int(snapshot.limit_up_count)}只 "
        f"/ 最高{int(snapshot.max_board_height)}板 "
        f"/ 炸板率{snapshot.broken_rate:.0f}% / 综合{snapshot.composite_score:.0f}分"
    )
    if risk:
        snapshot.description += f" / 风险{risk}"
    if position:
        snapshot.description += f" / 建议{position}"
    return snapshot


__all__ = [
    "DaySnapshot",
    "SimilarDay",
    "SimilarDayResult",
    "find_similar_days",
    "build_today_snapshot_from_ctx",
]
