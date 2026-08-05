"""Read model for the Web trading workbench."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import RLock
from time import monotonic
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

from backtest.trade_calendar import TradeCalendar
from core.application.mobile_services import MobileReadService


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _code(value: Any) -> str:
    text = str(value or "").split(".", 1)[0]
    return text.zfill(6) if text else ""


class WorkbenchService:
    """Compose concise decision data without touching external providers."""

    def __init__(
        self,
        read_service: MobileReadService | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.read_service = read_service or MobileReadService()
        self._clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Shanghai")))
        self._calendar = TradeCalendar()
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = RLock()

    def dashboard(self, trade_date: str = "") -> Dict[str, Any]:
        date = trade_date or self.read_service.repository.latest_date()
        return self._cached(
            f"dashboard:{date}",
            self._read_ttl(date, active_seconds=15.0),
            lambda: self._build_dashboard(date),
        )

    def _build_dashboard(self, trade_date: str) -> Dict[str, Any]:
        data = dict(self.read_service.dashboard(trade_date))
        dates = self.read_service.repository.list_dates()[:120]
        groups = data.get("groups") or {}
        focus = list(groups.get("重点确认") or [])
        watch = list(groups.get("盘中观察") or [])
        avoid = list(groups.get("暂不参与") or [])
        market = dict(data.get("market") or {})
        data.update(
            {
                "available_dates": dates,
                "decision_summary": {
                    "total": len(focus) + len(watch) + len(avoid),
                    "actionable": len(focus) + len(watch),
                    "focus": len(focus),
                    "watch": len(watch),
                    "avoid": len(avoid),
                },
                "market_brief": self._market_brief(market),
                "quick_links": [
                    {"label": "盘中确认", "href": "/intraday"},
                    {"label": "涨停梯队", "href": "/data/limitup"},
                    {"label": "龙头池", "href": "/dragon"},
                    {"label": "模拟交易", "href": "/backtest"},
                ],
            }
        )
        return data

    def candidate_detail(self, code: str, trade_date: str = "") -> Dict[str, Any]:
        date = trade_date or self.read_service.repository.latest_date()
        pure = _code(code)
        return self._cached(
            f"candidate:{date}:{pure}",
            self._read_ttl(date, active_seconds=30.0),
            lambda: self.read_service.candidate_detail(pure, date),
        )

    def intelligence(self, trade_date: str = "") -> Dict[str, Any]:
        date = trade_date or self.read_service.repository.latest_date()
        return self._cached(
            f"intelligence:{date}",
            self._read_ttl(date, active_seconds=30.0),
            lambda: self._build_intelligence(date),
        )

    def realtime(
        self,
        candidate_date: str = "",
        market_date: str = "",
        limit: int = 50,
    ) -> Dict[str, Any]:
        now = self._clock()
        quote_date = market_date or now.strftime("%Y%m%d")
        trade_date = candidate_date or self.read_service.repository.latest_date()
        policy = self._refresh_policy(now, quote_date)
        page_size = max(1, min(int(limit or 50), 50))

        def build() -> Dict[str, Any]:
            result = dict(self.read_service.realtime(trade_date, quote_date, page_size) or {})
            result.setdefault("trade_date", trade_date)
            result.setdefault("market_date", quote_date)
            result.setdefault("rows", [])
            result["rows"] = [self._realtime_row(row) for row in result.get("rows") or []]
            return result

        payload = dict(
            self._cached(
                f"realtime:{trade_date}:{quote_date}:{page_size}",
                2.0,
                build,
            )
        )
        payload.setdefault("trade_date", trade_date)
        payload.setdefault("market_date", quote_date)
        payload.setdefault("rows", [])
        payload["refresh_policy"] = policy
        return payload

    def stock_workspace(self, code: str, trade_date: str = "", limit: int = 120) -> Dict[str, Any]:
        date = trade_date or self.read_service.repository.latest_date()
        pure = _code(code)
        cache_key = f"stock:{date}:{pure}:{limit}"

        def build() -> Dict[str, Any]:
            profile = dict(self.read_service.stock(pure, date) or {})
            candles = dict(self.read_service.daily_candles(pure, date, limit) or {})
            profile["candles"] = list(candles.get("items") or [])
            profile.setdefault("trade_date", date)
            profile.setdefault("code", pure)
            return profile

        return self._cached(
            cache_key,
            self._read_ttl(date, active_seconds=30.0),
            build,
        )

    def _build_intelligence(self, trade_date: str) -> Dict[str, Any]:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="workbench-read") as pool:
            dashboard_future = pool.submit(self.dashboard, trade_date)
            leader_future = pool.submit(self.read_service.leaders, trade_date, 10, 20)
            limit_future = pool.submit(self.read_service.limitup, trade_date)
            lhb_future = pool.submit(self.read_service.lhb, trade_date)
            dashboard = dict(dashboard_future.result() or {})
            leader_payload = dict(leader_future.result() or {})
            limit_payload = dict(limit_future.result() or {})
            lhb_payload = dict(lhb_future.result() or {})
        groups = dashboard.get("groups") or {}
        candidates = [
            dict(row)
            for group_name, rows in groups.items()
            for row in (rows or [])
            if isinstance(row, dict)
            and group_name in {"重点确认", "盘中观察", "暂不参与"}
        ]
        leader_rows = [dict(row) for row in leader_payload.get("rows") or []]
        return {
            "trade_date": trade_date,
            "mainlines": self._mainline_rows(candidates, leader_rows),
            "limitup": {
                "limit_up_count": int(limit_payload.get("limit_up_count") or 0),
                "limit_down_count": int(limit_payload.get("limit_down_count") or 0),
                "max_board_height": int(limit_payload.get("max_board_height") or 0),
                "echelon": [
                    {
                        "board_height": int(item.get("board_height") or 0),
                        "count": int(item.get("count") or 0),
                        "stocks": [self._limitup_stock(row) for row in (item.get("stocks") or [])[:8]],
                    }
                    for item in (limit_payload.get("echelon") or [])[:6]
                ],
            },
            "leaders": {
                "counts": dict(leader_payload.get("counts") or {}),
                "role_counts": dict(leader_payload.get("role_counts") or {}),
                "rows": [self._leader_row(row) for row in leader_rows[:12]],
                "status": str(leader_payload.get("status") or "ready"),
            },
            "lhb": self._lhb_preview(lhb_payload),
            "generated_at": str(leader_payload.get("generated_at") or dashboard.get("generated_at") or ""),
            "data_status": "ready",
        }

    @staticmethod
    def _mainline_rows(candidates: List[Dict[str, Any]], leaders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        buckets: Dict[str, Dict[str, Any]] = {}
        for row in candidates:
            name = str(row.get("mainline") or "").strip()
            if not name or name in {"待确认", "无", "--"}:
                continue
            bucket = buckets.setdefault(
                name,
                {"name": name, "candidate_count": 0, "focus_count": 0, "leader_count": 0, "strengths": [], "stocks": []},
            )
            bucket["candidate_count"] += 1
            bucket["focus_count"] += int(row.get("action_group") == "重点确认")
            bucket["strengths"].append(_number(row.get("sector_strength")))
            if len(bucket["stocks"]) < 5:
                bucket["stocks"].append({"code": _code(row.get("code")), "name": str(row.get("name") or "")})
        for row in leaders:
            name = str(row.get("primary_sector") or "").strip()
            if not name:
                continue
            bucket = buckets.setdefault(
                name,
                {"name": name, "candidate_count": 0, "focus_count": 0, "leader_count": 0, "strengths": [], "stocks": []},
            )
            bucket["leader_count"] += 1
            bucket["strengths"].append(_number(row.get("sector_status_score")))
            code = _code(row.get("code"))
            if code and all(item.get("code") != code for item in bucket["stocks"]):
                bucket["stocks"].append({"code": code, "name": str(row.get("name") or "")})
        rows = []
        for bucket in buckets.values():
            strengths = bucket.pop("strengths")
            bucket["strength"] = round(sum(strengths) / len(strengths), 1) if strengths else 0.0
            bucket["stocks"] = bucket["stocks"][:5]
            rows.append(bucket)
        return sorted(
            rows,
            key=lambda row: (
                int(row["focus_count"]),
                int(row["leader_count"]),
                _number(row["strength"]),
                int(row["candidate_count"]),
            ),
            reverse=True,
        )[:8]

    @staticmethod
    def _leader_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "code": _code(row.get("code")),
            "name": str(row.get("name") or ""),
            "pool_type": str(row.get("pool_type") or "近期龙头"),
            "primary_role": str(row.get("primary_role") or ""),
            "leader_roles": list(row.get("leader_roles") or []),
            "leader_score": round(_number(row.get("leader_score")), 1),
            "lifecycle_state": str(row.get("lifecycle_state") or ""),
            "leader_time_label": str(row.get("leader_time_label") or ""),
            "primary_sector": str(row.get("primary_sector") or ""),
            "pct_chg": round(_number(row.get("pct_chg")), 2),
            "action": str(row.get("action") or "等待盘中确认"),
        }

    @staticmethod
    def _limitup_stock(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "code": _code(row.get("code")),
            "name": str(row.get("name") or ""),
            "pct_chg": round(_number(row.get("pct_chg")), 2),
            "first_time": str(row.get("first_time") or ""),
            "open_times": int(_number(row.get("open_times"))),
        }

    @staticmethod
    def _lhb_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
        stocks = list(payload.get("stocks") or [])
        actors = list(payload.get("actors") or payload.get("hot_money") or [])
        return {
            "summary": dict(payload.get("summary") or {}),
            "stocks": [
                {
                    "code": _code(row.get("code")),
                    "name": str(row.get("name") or ""),
                    "pct_chg": round(_number(row.get("pct_chg")), 2),
                    "net_buy_yuan": _number(row.get("net_buy_yuan")),
                    "institution_net_yuan": _number(row.get("institution_net_yuan")),
                }
                for row in stocks[:6]
            ],
            "actors": [
                {
                    "name": str(row.get("name") or row.get("actor_name") or ""),
                    "net_buy_yuan": _number(row.get("net_buy_yuan")),
                    "stock_count": len(row.get("stocks") or []),
                }
                for row in actors[:5]
            ],
            "status": str(payload.get("status") or ("ready" if stocks or actors else "empty")),
        }

    @staticmethod
    def _realtime_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "code": _code(row.get("code")),
            "name": str(row.get("name") or ""),
            "last_price": _number(row.get("last_price")),
            "open_price": _number(row.get("open_price")),
            "pre_close": _number(row.get("pre_close")),
            "pct_chg": round(_number(row.get("pct_chg")), 2),
            "open_gap_pct": round(_number(row.get("open_gap_pct")), 2),
            "confirm_status": str(row.get("confirm_status") or "observe"),
            "reason": str(row.get("reason") or "等待当日分钟入场条件"),
            "entry_mode": str(row.get("entry_mode") or ""),
            "entry_mode_text": str(row.get("entry_mode_text") or "等待分类"),
            "received_at": str(row.get("received_at") or ""),
            "is_stale": bool(row.get("is_stale")),
        }

    def _refresh_policy(self, now: datetime, market_date: str) -> Dict[str, Any]:
        current_date = now.strftime("%Y%m%d")
        minute = now.hour * 60 + now.minute
        is_today = market_date == current_date
        is_trade_day = bool(is_today and self._calendar.is_trade_date(current_date))
        is_session = 9 * 60 + 30 <= minute <= 15 * 60
        allowed = bool(is_trade_day and is_session)
        if not is_today:
            label = "历史行情不自动刷新"
        elif not is_trade_day:
            label = "非交易日"
        elif minute < 9 * 60 + 30:
            label = "等待开盘"
        elif minute > 15 * 60:
            label = "已收盘"
        else:
            label = "交易中"
        return {
            "auto_refresh": allowed,
            "interval_seconds": 5 if allowed else 0,
            "session_label": label,
            "market_date": market_date,
        }

    def _cached(self, key: str, ttl_seconds: float, loader: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        data = loader()
        with self._cache_lock:
            self._cache[key] = (now + ttl_seconds, data)
            if len(self._cache) > 128:
                self._cache = {name: item for name, item in self._cache.items() if item[0] > now}
        return data

    def _read_ttl(self, trade_date: str, *, active_seconds: float) -> float:
        today = self._clock().strftime("%Y%m%d")
        return active_seconds if trade_date >= today else 300.0

    @staticmethod
    def _market_brief(market: Dict[str, Any]) -> str:
        regime = str(market.get("regime_label") or "市场状态待确认")
        phase = str(market.get("emotion_phase") or "情绪阶段待确认")
        score = market.get("market_score")
        score_text = f"，市场分 {score:.0f}" if isinstance(score, (int, float)) else ""
        risks = list(market.get("risk_flags") or [])
        risk_text = f"；注意{'、'.join(risks[:2])}" if risks else ""
        return f"{regime}，{phase}{score_text}{risk_text}。"


__all__ = ["WorkbenchService"]
