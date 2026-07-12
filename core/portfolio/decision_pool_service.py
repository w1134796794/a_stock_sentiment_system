"""Turn multi-strategy screening output into a compact daily decision pool."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from core.portfolio.strategy_allocator import StrategyPortfolioAllocator


REGIME_STRATEGIES = {
    "strong": ("mainline_leader", "ultra_short_board", "first_board_launch"),
    "neutral": ("capital_resonance", "momentum_repair", "weak_to_strong"),
    "weak": ("defensive_quality", "weak_market_probe"),
}
REGIME_LABELS = {"strong": "强市", "neutral": "震荡市", "weak": "弱市"}
ENTRY_MODE_LABELS = {
    "weak_to_strong": "弱转强确认",
    "continuation": "强势延续确认",
    "acceleration": "高开加速确认",
}
BLOCKED_STATUSES = {"model_degraded", "data_insufficient", "no_edge"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _code(row: Mapping[str, Any]) -> str:
    value = str(row.get("code") or row.get("股票代码") or row.get("代码") or "").split(".", 1)[0]
    return value.zfill(6) if value else ""


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


class DecisionPoolService:
    """Select applicable strategies, merge overlap, and assign an action group."""

    def __init__(self, allocator: StrategyPortfolioAllocator | None = None) -> None:
        self.allocator = allocator or StrategyPortfolioAllocator()

    @staticmethod
    def market_regime(market_score: float) -> str:
        return "strong" if market_score >= 70 else "weak" if market_score < 45 else "neutral"

    def build(
        self,
        payloads: Mapping[str, Mapping[str, Any]],
        profiles: Mapping[str, Mapping[str, Any]],
        *,
        market_score: float = 50.0,
        market_regime: str = "",
    ) -> Dict[str, Any]:
        regime = str(market_regime) if str(market_regime) in REGIME_STRATEGIES else self.market_regime(market_score)
        applicable = [strategy_id for strategy_id in REGIME_STRATEGIES[regime] if strategy_id in payloads]
        hidden = [strategy_id for strategy_id in payloads if strategy_id not in applicable]

        raw_rows: List[Dict[str, Any]] = []
        members_by_code: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for strategy_id in applicable:
            payload = payloads.get(strategy_id) or {}
            profile = profiles.get(strategy_id) or {}
            strategy_name = str(payload.get("strategy_name") or profile.get("name") or strategy_id)
            execution = dict(profile.get("execution") or {})
            runtime = str((payload.get("weight_metadata") or {}).get("candidate_model_runtime") or "")
            model_status = "正常" if runtime == "active" else "已回退" if runtime.startswith("fallback") else "不可用"
            for source in payload.get("final") or []:
                if not isinstance(source, Mapping) or not _code(source):
                    continue
                row = dict(source)
                row.update({
                    "策略ID": strategy_id,
                    "策略名称": strategy_name,
                    "策略单票仓位上限%": _number(profile.get("position_cap_pct")),
                    "_entry_modes": list(execution.get("allowed_entry_modes") or []),
                    "_model_status": model_status,
                })
                raw_rows.append(row)
                members_by_code[_code(row)].append(row)

        merged = self.allocator.merge(raw_rows)
        total = max(len(applicable), 1)
        for row in merged:
            members = members_by_code.get(_code(row), [])
            self._decorate(row, members, total, regime)

        actionable = [row for row in merged if not row["_blocked"]]
        inactive = [row for row in merged if row["_blocked"]]
        focus: List[Dict[str, Any]] = []
        watch: List[Dict[str, Any]] = []
        for row in actionable:
            grade = str(row.get("股票等级") or "D")
            expected = _number(row.get("预期超额收益%"))
            if len(focus) < 3 and grade in {"A", "B"} and expected >= 0.30:
                self._set_group(row, "focus", regime)
                focus.append(row)
            elif len(watch) < 5 and len(focus) + len(watch) < 8 and grade in {"A", "B", "C"}:
                self._set_group(row, "watch", regime)
                watch.append(row)
            else:
                self._set_group(row, "inactive", regime)
                inactive.append(row)

        for row in inactive:
            if row.get("行动分组") != "暂不参与":
                self._set_group(row, "inactive", regime)

        active_names = [str((profiles.get(key) or {}).get("name") or key) for key in applicable]
        hidden_names = [str((profiles.get(key) or {}).get("name") or key) for key in hidden]
        return {
            "regime": regime,
            "regime_label": REGIME_LABELS[regime],
            "market_score": round(market_score, 1),
            "active_strategy_ids": applicable,
            "active_strategy_names": active_names,
            "hidden_strategy_names": hidden_names,
            "rows": focus + watch + inactive,
            "groups": [
                {"key": "focus", "name": "重点确认", "meaning": "多策略共识，盘中满足条件可交易", "rows": focus},
                {"key": "watch", "name": "盘中观察", "meaning": "具备优势，等待弱转强或板块确认", "rows": watch},
                {"key": "inactive", "name": "暂不参与", "meaning": "模型失效、样本不足或风险过高", "rows": inactive},
            ],
            "decision_count": len(focus) + len(watch),
        }

    def _decorate(
        self,
        row: Dict[str, Any],
        members: Sequence[Mapping[str, Any]],
        strategy_total: int,
        regime: str,
    ) -> None:
        names = _unique(str(item.get("策略名称") or item.get("策略ID") or "") for item in members)
        sectors = _unique(
            str(sector).strip()
            for item in members
            for sector in str(item.get("resonance_sectors") or "").replace("，", ",").split(",")
        )
        contexts = [item.get("context") or {} for item in members]
        sector_scores = [
            max(_number(ctx.get("sector_mainline_score")), _number(ctx.get("sector_resonance_score")))
            for ctx in contexts
        ]
        sector_strength = max(sector_scores or [0.0])
        modes = _unique(
            ENTRY_MODE_LABELS.get(mode, mode)
            for item in members for mode in (item.get("_entry_modes") or [])
        )
        grade_order = {"A": 0, "B": 1, "C": 2, "D": 3}
        grades = [str(item.get("confidence_grade") or "D") for item in members]
        stock_grade = min(grades or ["D"], key=lambda value: grade_order.get(value, 3))
        expected_return = max((_number(item.get("expected_return_pct")) for item in members), default=0.0)
        model_statuses = [str(item.get("_model_status") or "不可用") for item in members]
        model_status = "正常" if "正常" in model_statuses else "已回退" if "已回退" in model_statuses else "不可用"
        blocked_reasons: List[str] = []
        statuses = {str(item.get("decision_status") or "") for item in members}
        if members and statuses and statuses.issubset({"data_insufficient", "no_edge"}):
            blocked_reasons.append("模型或样本暂不可用")
        if stock_grade == "D":
            blocked_reasons.append("可信等级不足")
        stop_probability = max((_number(item.get("stop_probability")) for item in members), default=0.0)
        if stop_probability >= 65:
            blocked_reasons.append(f"先触发止损概率{stop_probability:.0f}%")

        row.update({
            "命中策略": names,
            "策略总数": strategy_total,
            "策略共识显示": f"{len(names)}/{strategy_total}",
            "所属主线": sectors[0] if sectors else "主线待确认",
            "共振板块": sectors[:4],
            "板块强度": round(sector_strength, 1),
            "板块强度说明": "强" if sector_strength >= 70 else "中" if sector_strength >= 50 else "弱",
            "明日入场模式": " / ".join(modes) if modes else "等待分钟行情分类",
            "模型状态": model_status,
            "股票等级": stock_grade,
            "预期超额收益%": round(expected_return, 2),
            "_blocked": bool(blocked_reasons),
            "_blocked_reasons": blocked_reasons,
        })
        row["一句话结论"] = (
            f"{row.get('name') or row.get('股票名称') or row.get('代码')}命中{len(names)}个当前适用策略，"
            f"主线{row['所属主线']}，板块强度{row['板块强度说明']}；"
            + ("当前证据不足，先不参与。" if blocked_reasons else "次日只在分钟条件确认后参与。")
        )
        row["失效条件"] = (
            "；".join(blocked_reasons)
            if blocked_reasons
            else "板块转弱、跌破开盘低点或10:00前未确认"
        )

    @staticmethod
    def _set_group(row: Dict[str, Any], group: str, regime: str) -> None:
        if group == "inactive":
            row["行动分组"] = "暂不参与"
            row["建议仓位"] = "0%"
            return
        cap = _number(row.get("position_budget_pct"), _number(row.get("策略单票仓位上限%"), 10.0))
        if cap <= 0:
            cap = 10.0
        if regime == "weak":
            cap = min(cap, 8.0)
        elif group == "watch":
            cap = min(cap, 10.0)
        row["行动分组"] = "重点确认" if group == "focus" else "盘中观察"
        row["建议仓位"] = f"确认后参考 {cap:.0f}%"


__all__ = ["DecisionPoolService", "REGIME_STRATEGIES"]
