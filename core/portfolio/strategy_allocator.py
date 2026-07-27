"""Deterministic allocator for an auditable multi-strategy candidate portfolio.

This is intentionally a portfolio construction layer, not another stock-selection
model. Strategies keep their own candidate logic; this module only reconciles
overlap, strategy consensus and concentration constraints before execution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd


STRATEGY_EVIDENCE_FAMILIES = {
    "mainline_leader": "主线与辨识度",
    "ultra_short_board": "涨停结构",
    "first_board_launch": "涨停结构",
    "weak_to_strong": "分时修复",
    "capital_resonance": "资金流",
    "momentum_repair": "量价修复",
    "trend_follow": "趋势强度",
    "defensive": "风险防守",
    "weak_market_probe": "逆势强度",
}


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


def _execution(row: Dict[str, Any]) -> Dict[str, Any]:
    value = row.get("策略执行") or row.get("strategy_execution") or {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


@dataclass(frozen=True)
class AllocationConfig:
    max_positions: int = 8
    max_total_weight: float = 0.80
    max_stock_weight: float = 0.20
    max_sector_weight: float = 0.40
    min_position_weight: float = 0.03
    max_expected_tail_loss: float = 0.04
    correlation_soft_limit: float = 0.75
    correlation_hard_limit: float = 0.90
    min_return_observations: int = 40


class StrategyPortfolioAllocator:
    """Merge strategy rows and emit a constrained, explainable allocation."""

    def __init__(self, config: AllocationConfig | None = None) -> None:
        self.config = config or AllocationConfig()
        self.last_risk_report: Dict[str, Any] = {"status": "not_run"}

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
        evidence_families = list(dict.fromkeys(
            STRATEGY_EVIDENCE_FAMILIES.get(strategy_id, strategy_id)
            for strategy_id in strategy_ids
        ))
        independent_consensus = len(evidence_families)
        quality = sum(_number(row.get("_relative_quality")) for row in members) / max(consensus, 1)
        consensus_quality = min(independent_consensus / 3.0, 1.0)
        allocation_score = 100.0 * (0.72 * quality + 0.28 * consensus_quality)
        caps = [
            _number(row.get("策略单票仓位上限%"), _number(row.get("position_cap_pct"))) / 100.0
            for row in members
            if _number(row.get("策略单票仓位上限%"), _number(row.get("position_cap_pct"))) > 0
        ]
        executions = [_execution(row) for row in members]
        allowed_entry_modes = list(dict.fromkeys(
            str(mode).strip()
            for execution in executions
            for mode in (execution.get("allowed_entry_modes") or [])
            if str(mode).strip()
        ))
        deadlines = sorted(
            str(execution.get("confirmation_deadline") or "").strip()
            for execution in executions
            if str(execution.get("confirmation_deadline") or "").strip()
        )
        max_ages = [
            int(_number(execution.get("candidate_max_age_days")))
            for execution in executions
            if _number(execution.get("candidate_max_age_days")) > 0
        ]
        combined_execution = {
            "allowed_entry_modes": allowed_entry_modes,
            "confirmation_deadline": deadlines[0] if deadlines else "10:00:00",
            "candidate_max_age_days": min(max_ages) if max_ages else 1,
            "source_strategies": strategy_ids,
        }
        result = {key: value for key, value in primary.items() if not str(key).startswith("_")}
        result.update({
            "代码": code,
            "策略ID": _text(primary.get("策略ID") or primary.get("strategy_id") or "default"),
            "策略名称": _text(primary.get("策略名称") or primary.get("strategy_name") or "default"),
            "策略来源": ",".join(strategy_ids),
            "策略共识": " / ".join(strategy_names),
            "策略共识数": consensus,
            "独立证据共识数": independent_consensus,
            "独立证据族": "、".join(evidence_families),
            "策略组合评分": round(allocation_score, 2),
            "策略单票仓位上限%": round(min(caps) * 100.0, 2) if caps else 0.0,
            "策略执行": json.dumps(combined_execution, ensure_ascii=False, sort_keys=True),
            "strategy_execution": combined_execution,
            "allowed_entry_modes": allowed_entry_modes,
            "组合板块": _sector(primary),
        })
        return result

    def _allocate_weights(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        selected = candidates[: max(int(self.config.max_positions), 1)]
        if not selected:
            return []
        total_score = sum(max(_number(row.get("策略组合评分")), 0.0) for row in selected) or float(len(selected))
        returns = self._historical_returns(selected)
        parity: Dict[str, float] = {}
        if len(returns) >= self.config.min_return_observations and len(returns.columns) >= 2:
            from risk.portfolio_allocator import risk_parity_weights

            parity = risk_parity_weights(
                returns, max_weight=self.config.max_stock_weight,
                total_position=self.config.max_total_weight,
            )
        sector_weights: Dict[str, float] = {}
        expected_tail_loss = 0.0
        allocated: List[Dict[str, Any]] = []
        equal_share = self.config.max_total_weight / len(selected)

        for row in selected:
            score_share = max(_number(row.get("策略组合评分")), 0.0) / total_score
            requested = 0.40 * equal_share + 0.60 * self.config.max_total_weight * score_share
            code = _code(row)
            if code in parity:
                requested = 0.50 * requested + 0.50 * float(parity[code])
            correlation_penalty, max_correlation = self._correlation_penalty(
                code, [_code(item) for item in allocated], returns,
            )
            requested *= 1.0 - correlation_penalty
            strategy_cap = _number(row.get("策略单票仓位上限%")) / 100.0
            stock_cap = min(
                self.config.max_stock_weight,
                strategy_cap if strategy_cap > 0 else self.config.max_stock_weight,
            )
            sector = _text(row.get("组合板块")) or "未分类"
            sector_remaining = max(self.config.max_sector_weight - sector_weights.get(sector, 0.0), 0.0)
            tail_loss = self._tail_loss(row)
            tail_remaining = max(self.config.max_expected_tail_loss - expected_tail_loss, 0.0)
            tail_cap = tail_remaining / tail_loss if tail_loss > 1e-12 else stock_cap
            weight = min(requested, stock_cap, sector_remaining, tail_cap)
            if weight < self.config.min_position_weight:
                continue
            sector_weights[sector] = sector_weights.get(sector, 0.0) + weight
            expected_tail_loss += weight * tail_loss
            row["组合建议仓位%"] = round(weight * 100.0, 2)
            row["建议仓位"] = f"组合 {weight * 100.0:.1f}%"
            row["组合分配说明"] = (
                f"{row['独立证据共识数']}类独立证据（{row['策略共识数']}个策略）；{sector}板块，"
                f"受单票{stock_cap:.0%}/板块{self.config.max_sector_weight:.0%}/组合尾损"
                f"{self.config.max_expected_tail_loss:.0%}上限约束；"
                f"历史相关性{max_correlation:.2f}，惩罚{correlation_penalty:.0%}"
            )
            row["个股预估尾损%"] = round(tail_loss * 100.0, 2)
            row["组合累计压力损失%"] = round(expected_tail_loss * 100.0, 2)
            allocated.append(row)
        weights = {_code(row): _number(row.get("组合建议仓位%")) / 100.0 for row in allocated}
        if not returns.empty and weights:
            from risk.portfolio_allocator import portfolio_risk_report

            self.last_risk_report = portfolio_risk_report(returns, weights)
            self.last_risk_report["observations"] = int(len(returns))
            self.last_risk_report["weight_source"] = "score_risk_parity_blend"
        else:
            self.last_risk_report = {
                "status": "insufficient_data", "observations": int(len(returns)),
                "weight_source": "score_and_tail_fallback",
            }
        return allocated

    def _correlation_penalty(
        self, code: str, selected_codes: List[str], returns: pd.DataFrame,
    ) -> tuple[float, float]:
        if code not in returns or not selected_codes:
            return 0.0, 0.0
        available = [item for item in selected_codes if item in returns]
        if not available:
            return 0.0, 0.0
        correlations = returns[available].corrwith(returns[code]).abs().dropna()
        maximum = float(correlations.max()) if not correlations.empty else 0.0
        hard_limit = max(float(self.config.correlation_hard_limit), 0.01)
        soft_limit = min(float(self.config.correlation_soft_limit), hard_limit)
        if maximum >= hard_limit:
            return 0.75, maximum
        if maximum >= soft_limit:
            return 0.40, maximum
        return 0.0, maximum

    def _historical_returns(self, candidates: List[Dict[str, Any]]) -> pd.DataFrame:
        codes = [_code(row) for row in candidates if _code(row)]
        if len(codes) < 2:
            return pd.DataFrame()
        dates = [
            _text(row.get("trade_date") or row.get("date") or row.get("交易日"))
            for row in candidates
        ]
        as_of_date = max((date[:8] for date in dates if len(date) >= 8), default="99999999")
        try:
            import duckdb  # type: ignore
            from config.settings import FACTOR_DB_PATH

            path = Path(FACTOR_DB_PATH)
            if not path.exists():
                return pd.DataFrame()
            placeholders = ",".join("?" for _ in codes)
            con = duckdb.connect(str(path), read_only=True)
            try:
                frame = con.execute(
                    "SELECT trade_date, code, close FROM stock_daily_silver "
                    f"WHERE code IN ({placeholders}) AND CAST(trade_date AS VARCHAR)<=? "
                    "QUALIFY DENSE_RANK() OVER (ORDER BY CAST(trade_date AS VARCHAR) DESC)<=80",
                    [*codes, as_of_date],
                ).fetchdf()
            finally:
                con.close()
        except Exception:
            return pd.DataFrame()
        if frame.empty:
            return pd.DataFrame()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        price = frame.pivot_table(index="trade_date", columns="code", values="close", aggfunc="last").sort_index()
        return price.pct_change(fill_method=None).dropna(how="all")

    @staticmethod
    def _tail_loss(row: Dict[str, Any]) -> float:
        samples = row.get("历史收益样本") or row.get("return_samples") or []
        if isinstance(samples, list) and samples:
            ordered = sorted(_number(value) for value in samples)
            if any(abs(value) > 1.0 for value in ordered):
                ordered = [value / 100.0 for value in ordered]
            count = max(int(len(ordered) * 0.10), 1)
            return max(abs(sum(ordered[:count]) / count), 0.01)
        mae = abs(_number(row.get("平均MAE%") or row.get("average_mae_pct"))) / 100.0
        stop = abs(_number(row.get("止损线%") or row.get("stop_loss_pct"))) / 100.0
        return min(max(mae, stop, 0.03), 0.20)


__all__ = ["AllocationConfig", "StrategyPortfolioAllocator", "STRATEGY_EVIDENCE_FAMILIES"]
