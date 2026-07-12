"""Deterministic allocator for an auditable multi-strategy candidate portfolio.

This is intentionally a portfolio construction layer, not another stock-selection
model. Strategies keep their own candidate logic; this module only reconciles
overlap, strategy consensus and concentration constraints before execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    return str(value or "").strip()


def _code(row: Dict[str, Any]) -> str:
    value = _text(row.get("代码") or row.get("股票代码") or row.get("code") or row.get("ts_code")).split(".", 1)[0]
    return value.zfill(6) if value else ""


def _sector(row: Dict[str, Any]) -> str:
    raw = _text(row.get("共振板块") or row.get("所属板块") or row.get("resonance_sectors"))
    return raw.replace("，", ",").split(",", 1)[0].strip() or "未分类"


@dataclass(frozen=True)
class AllocationConfig:
    max_positions: int = 8
    max_total_weight: float = 0.80
    max_stock_weight: float = 0.20
    max_sector_weight: float = 0.40
    min_position_weight: float = 0.03


class StrategyPortfolioAllocator:
    """Merge strategy rows and emit a constrained, explainable allocation."""

    def __init__(self, config: AllocationConfig | None = None) -> None:
        self.config = config or AllocationConfig()

    def allocate(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = self.merge(rows)
        return self._allocate_weights(candidates)

    def merge(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate candidates without applying portfolio position limits."""
        normalized = [dict(row or {}) for row in rows if _code(dict(row or {}))]
        if not normalized:
            return []

        self._attach_relative_quality(normalized)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in normalized:
            grouped.setdefault(_code(row), []).append(row)

        candidates = [self._merge_code(code, members) for code, members in grouped.items()]
        candidates.sort(
            key=lambda row: (
                -_number(row.get("策略组合评分")),
                -int(row.get("策略共识数") or 0),
                -_number(row.get("综合评分")),
                _code(row),
            )
        )
        return candidates

    @staticmethod
    def _attach_relative_quality(rows: List[Dict[str, Any]]) -> None:
        by_strategy: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            strategy_id = _text(row.get("策略ID") or row.get("strategy_id") or "default")
            row["_strategy_id"] = strategy_id
            row["_score"] = _number(row.get("综合评分"), _number(row.get("score")))
            rank = int(_number(row.get("优先级"), _number(row.get("rank"), 999)))
            row["_rank"] = rank if rank > 0 else 999
            by_strategy.setdefault(strategy_id, []).append(row)

        for members in by_strategy.values():
            scores = [row["_score"] for row in members]
            low, high = min(scores), max(scores)
            max_rank = max(row["_rank"] for row in members)
            for row in members:
                score_quality = (row["_score"] - low) / (high - low) if high > low else 0.65
                rank_quality = 1.0 - min(max(row["_rank"] - 1, 0), max(max_rank - 1, 1)) / max(max_rank - 1, 1)
                row["_relative_quality"] = 0.70 * score_quality + 0.30 * rank_quality

    def _merge_code(self, code: str, members: List[Dict[str, Any]]) -> Dict[str, Any]:
        primary = max(members, key=lambda row: (row["_relative_quality"], row["_score"], -row["_rank"]))
        strategy_ids = list(dict.fromkeys(row["_strategy_id"] for row in members))
        strategy_names = list(dict.fromkeys(
            _text(row.get("策略名称") or row.get("strategy_name") or row["_strategy_id"])
            for row in members
        ))
        consensus = len(strategy_ids)
        quality = sum(_number(row.get("_relative_quality")) for row in members) / max(consensus, 1)
        consensus_quality = min(consensus / 2.0, 1.0)
        allocation_score = 100.0 * (0.72 * quality + 0.28 * consensus_quality)
        caps = [
            _number(row.get("策略单票仓位上限%"), _number(row.get("position_cap_pct"))) / 100.0
            for row in members
            if _number(row.get("策略单票仓位上限%"), _number(row.get("position_cap_pct"))) > 0
        ]
        result = {key: value for key, value in primary.items() if not str(key).startswith("_")}
        result.update({
            "代码": code,
            "策略ID": _text(primary.get("策略ID") or primary.get("strategy_id") or "default"),
            "策略名称": _text(primary.get("策略名称") or primary.get("strategy_name") or "default"),
            "策略来源": ",".join(strategy_ids),
            "策略共识": " / ".join(strategy_names),
            "策略共识数": consensus,
            "策略组合评分": round(allocation_score, 2),
            "策略单票仓位上限%": round(min(caps) * 100.0, 2) if caps else 0.0,
            "组合板块": _sector(primary),
        })
        return result

    def _allocate_weights(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        selected = candidates[: max(int(self.config.max_positions), 1)]
        if not selected:
            return []
        total_score = sum(max(_number(row.get("策略组合评分")), 0.0) for row in selected) or float(len(selected))
        sector_weights: Dict[str, float] = {}
        allocated: List[Dict[str, Any]] = []
        equal_share = self.config.max_total_weight / len(selected)

        for row in selected:
            score_share = max(_number(row.get("策略组合评分")), 0.0) / total_score
            requested = 0.40 * equal_share + 0.60 * self.config.max_total_weight * score_share
            strategy_cap = _number(row.get("策略单票仓位上限%")) / 100.0
            stock_cap = min(
                self.config.max_stock_weight,
                strategy_cap if strategy_cap > 0 else self.config.max_stock_weight,
            )
            sector = _text(row.get("组合板块")) or "未分类"
            sector_remaining = max(self.config.max_sector_weight - sector_weights.get(sector, 0.0), 0.0)
            weight = min(requested, stock_cap, sector_remaining)
            if weight < self.config.min_position_weight:
                continue
            sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
            row["组合建议仓位%"] = round(weight * 100.0, 2)
            row["建议仓位"] = f"组合 {weight * 100.0:.1f}%"
            row["组合分配说明"] = (
                f"{row['策略共识数']}个策略共识；{sector}板块，"
                f"受单票{stock_cap:.0%}/板块{self.config.max_sector_weight:.0%}上限约束"
            )
            allocated.append(row)
        return allocated


__all__ = ["AllocationConfig", "StrategyPortfolioAllocator"]
