"""Client-independent read models for the mobile API."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List

from core.application.mobile_repository import MobileReadRepository


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _list_value(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").replace("，", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _confirmation_text(row: Dict[str, Any]) -> str:
    explicit = str(row.get("次日确认条件") or row.get("确认条件") or "").strip()
    if explicit:
        return explicit
    execution = row.get("strategy_execution") or {}
    modes = _list_value(execution.get("allowed_entry_modes"))
    labels = {
        "weak_to_strong": "收复昨收、站上分时均价并突破前5分钟高点",
        "continuation": "回踩分时均价不破或突破前5分钟高点",
        "high_open_acceleration": "分钟成交确认可买且板块同步走强",
    }
    conditions = [labels[mode] for mode in modes if mode in labels]
    if not conditions:
        return "满足策略分钟条件"
    deadline = str(execution.get("confirmation_deadline") or "").strip()
    deadline_text = f"{deadline[:5]}前" if deadline else "盘中"
    return f"{deadline_text}{'；或'.join(conditions)}"


class MobileReadService:
    """Build concise mobile views from generated local artifacts."""

    def __init__(
        self,
        repository: MobileReadRepository | None = None,
        *,
        leader_loader: Callable[[str, int, int], Dict[str, Any]] | None = None,
        lhb_loader: Callable[[str], Dict[str, Any]] | None = None,
        realtime_loader: Callable[[str, str, int], Dict[str, Any] | None] | None = None,
    ) -> None:
        self.repository = repository or MobileReadRepository()
        self.leader_loader = leader_loader
        self.lhb_loader = lhb_loader
        self.realtime_loader = realtime_loader

    def bootstrap(self, user: Dict[str, Any]) -> Dict[str, Any]:
        latest = self.repository.latest_date()
        market = self.repository.market_snapshot(latest)
        return {
            "trade_date": latest,
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "display_name": user.get("display_name") or user.get("username"),
                "role": user.get("role"),
                "expire_at": user.get("expire_at"),
                "max_sessions": user.get("max_sessions"),
            },
            "latest_trade_date": latest,
            "available_dates": self.repository.list_dates()[:60],
            "market": self._market_summary(market),
            "capabilities": [
                "dashboard",
                "candidates",
                "realtime",
                "leaders",
                "limitup",
                "lhb",
                "stocks",
            ],
        }

    def dashboard(self, trade_date: str = "") -> Dict[str, Any]:
        pool = self.repository.load_decision_pool(trade_date)
        date = str(pool.get("trade_date") or trade_date or self.repository.latest_date())
        market = self.repository.market_snapshot(date)
        groups = self._group_candidates(pool.get("rows") or [])
        return {
            "trade_date": date,
            "market": self._market_summary(market, pool),
            "groups": groups,
            "counts": {key: len(rows) for key, rows in groups.items()},
            "crowding_summary": list(pool.get("crowding_summary") or []),
            "cluster_limits": dict(pool.get("cluster_limits") or {}),
            "generated_at": str(pool.get("generated_at") or market.get("computed_at") or ""),
            "data_status": "ready" if pool else "missing",
            "data_completeness": 100.0 if pool and market else (50.0 if pool or market else 0.0),
        }

    def candidates(
        self,
        trade_date: str = "",
        *,
        group: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        pool = self.repository.load_decision_pool(trade_date)
        rows = [self._candidate_summary(row) for row in pool.get("rows") or []]
        if group:
            rows = [row for row in rows if row["action_group"] == group]
        start = max(int(offset or 0), 0)
        page_size = max(1, min(int(limit or 20), 50))
        return {
            "trade_date": str(pool.get("trade_date") or trade_date or self.repository.latest_date()),
            "total": len(rows),
            "offset": start,
            "limit": page_size,
            "items": rows[start : start + page_size],
            "generated_at": str(pool.get("generated_at") or ""),
            "data_completeness": 100.0 if pool else 0.0,
        }

    def candidate_detail(self, code: str, trade_date: str = "") -> Dict[str, Any]:
        pool = self.repository.load_decision_pool(trade_date)
        pure = str(code or "").split(".", 1)[0].zfill(6)
        for row in pool.get("rows") or []:
            row_code = str(row.get("code") or row.get("代码") or "").split(".", 1)[0].zfill(6)
            if row_code != pure:
                continue
            summary = self._candidate_summary(row)
            summary["trade_date"] = str(pool.get("trade_date") or trade_date or "")
            summary["evidence"] = {
                "rule_reasons": list(row.get("rule_reasons") or []),
                "penalty_reasons": list(row.get("penalty_reasons") or []),
                "enhancements": dict(row.get("enhancements") or {}),
                "metrics": dict(row.get("metrics") or {}),
                "confidence_grade": row.get("confidence_grade") or row.get("规则等级"),
                "data_completeness": _number(row.get("data_completeness") or row.get("数据完整度%")),
            }
            return summary
        return {}

    def realtime(self, candidate_date: str = "", market_date: str = "", limit: int = 20) -> Dict[str, Any]:
        if not self.realtime_loader:
            return {"rows": [], "status": "cache_unavailable"}
        payload = self.realtime_loader(candidate_date, market_date, max(1, min(int(limit or 20), 50)))
        if not payload:
            return {"rows": [], "status": "cache_empty"}
        result = dict(payload)
        result["status"] = "cached"
        return result

    def leaders(self, trade_date: str = "", lookback: int = 10, limit: int = 30) -> Dict[str, Any]:
        date = trade_date or self.repository.latest_date()
        if not self.leader_loader:
            return {"trade_date": date, "rows": [], "status": "unavailable"}
        return self.leader_loader(
            date,
            max(1, min(int(lookback or 10), 20)),
            max(1, min(int(limit or 30), 100)),
        )

    def limitup(self, trade_date: str = "") -> Dict[str, Any]:
        date = trade_date or self.repository.latest_date()
        up = self.repository.limit_rows(date, "up")
        down = self.repository.limit_rows(date, "down")
        echelon: Dict[int, List[Dict[str, Any]]] = {}
        for row in up:
            height = max(int(row.get("limit_times") or 1), 1)
            echelon.setdefault(height, []).append(row)
        return {
            "trade_date": date,
            "limit_up_count": len(up),
            "limit_down_count": len(down),
            "max_board_height": max(echelon, default=0),
            "echelon": [
                {"board_height": height, "count": len(rows), "stocks": rows}
                for height, rows in sorted(echelon.items(), reverse=True)
            ],
            "limit_down": down,
        }

    def lhb(self, trade_date: str = "") -> Dict[str, Any]:
        date = trade_date or self.repository.latest_date()
        if not self.lhb_loader:
            return {"trade_date": date, "stocks": [], "hot_money": [], "status": "unavailable"}
        payload = dict(self.lhb_loader(date) or {})
        payload.setdefault("trade_date", date)
        return payload

    def stock(self, code: str, trade_date: str = "") -> Dict[str, Any]:
        profile = self.repository.stock_profile(code, trade_date)
        candidate = dict(profile.pop("candidate", {}) or {})
        factors = dict(profile.pop("factors", {}) or {})
        return {
            **profile,
            "industry": _list_value(
                candidate.get("所属行业") or factors.get("industry_names") or factors.get("industry_name")
            ),
            "concepts": _list_value(
                candidate.get("相关题材") or candidate.get("共振板块") or factors.get("resonance_sectors")
            )[:12],
            "mainline": candidate.get("所属主线") or factors.get("primary_sector_name") or "",
            "candidate": self._candidate_summary(candidate) if candidate else None,
        }

    def daily_candles(self, code: str, trade_date: str = "", limit: int = 120) -> Dict[str, Any]:
        return {
            "code": str(code or "").split(".", 1)[0].zfill(6),
            "trade_date": trade_date or self.repository.latest_date(),
            "items": self.repository.daily_candles(code, trade_date, limit),
        }

    @staticmethod
    def _candidate_summary(row: Dict[str, Any]) -> Dict[str, Any]:
        strategies = _list_value(row.get("命中策略") or row.get("strategy_name"))
        return {
            "code": str(row.get("code") or row.get("代码") or "").split(".", 1)[0].zfill(6),
            "name": str(row.get("name") or row.get("名称") or ""),
            "action_group": str(row.get("行动分组") or row.get("decision_label") or "暂不参与"),
            "hit_strategies": strategies,
            "strategy_consensus": int(row.get("策略共识数") or len(strategies)),
            "strategy_total": int(row.get("策略总数") or 0),
            "mainline": str(row.get("所属主线") or ""),
            "related_themes": _list_value(row.get("相关题材") or row.get("共振板块"))[:8],
            "sector_strength": _number(row.get("板块强度")),
            "entry_mode": str(row.get("明日入场模式") or row.get("策略模式") or ""),
            "conclusion": str(row.get("一句话结论") or ""),
            "confirmation": _confirmation_text(row),
            "invalidation": str(row.get("失效条件") or row.get("否决条件") or ""),
            "position": str(row.get("建议仓位") or ""),
            "position_cap_pct": _number(row.get("执行仓位上限%") or row.get("position_budget_pct")),
            "confidence_grade": str(row.get("confidence_grade") or row.get("规则等级") or ""),
            "expected_return_pct": _number(row.get("expected_gross_return_pct") or row.get("expected_return_pct")),
            "expected_excess_return_pct": _number(row.get("expected_excess_return_pct")),
            "theme_cluster": str(row.get("主题簇") or ""),
            "theme_candidate_count": int(row.get("主题候选数") or 0),
            "theme_ratio_pct": _number(row.get("主题占比%")),
            "crowding_level": str(row.get("拥挤等级") or ""),
            "crowding_note": str(row.get("拥挤说明") or ""),
        }

    def _group_candidates(self, rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        groups = {"重点确认": [], "盘中观察": [], "暂不参与": []}
        for row in rows:
            item = self._candidate_summary(row)
            groups.setdefault(item["action_group"], []).append(item)
        return groups

    @staticmethod
    def _market_summary(market: Dict[str, Any], pool: Dict[str, Any] | None = None) -> Dict[str, Any]:
        pool = pool or {}
        return {
            "regime": str(pool.get("regime") or market.get("market_regime") or ""),
            "regime_label": str(pool.get("regime_label") or ""),
            "emotion_phase": str(pool.get("emotion_phase_label") or market.get("emotion_phase_label") or ""),
            "market_score": _number(pool.get("market_score") or market.get("market_score")),
            "limit_up_count": int(market.get("limit_up_count") or 0),
            "limit_down_count": int(market.get("limit_down_count") or 0),
            "broken_rate": _number(market.get("broken_rate")),
            "amount_yuan": _number(market.get("amount_yuan")),
            "position_scale": _number(pool.get("market_position_scale") or market.get("market_position_scale"), 1.0),
            "risk_flags": _list_value(pool.get("market_risk_labels") or pool.get("market_risk_flags")),
        }


__all__ = ["MobileReadService"]
