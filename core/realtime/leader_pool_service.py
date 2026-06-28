"""Leader pool and intraday strength views built on current factor screening output."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.realtime.models import normalize_stock_code
from core.utils.price_limit import (
    get_price_limit_pct_points,
    limit_progress,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _score_between(value: Any, low: float, high: float) -> float:
    val = _to_float(value)
    if high <= low:
        return 0.0
    return max(0.0, min(100.0, (val - low) / (high - low) * 100.0))


def _pct_text(value: Any) -> str:
    val = _to_float(value)
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


class LeaderPoolService:
    """Build a factor-native leader pool from ``webdata/screening`` results.

    龙头池在当前体系里不再是旧策略池，而是近段筛选中曾进入前 3 名的历史龙头集合；
    当日排名、量价、强势位置和流动性仅用于更新龙头评分与盘中观察顺序。
    """

    def __init__(self, *, screening_dir: Optional[Path] = None) -> None:
        from config.settings import WEB_DATA_DIR

        self.screening_dir = Path(screening_dir or WEB_DATA_DIR / "screening")

    def build_pool(
        self,
        trade_date: Optional[str] = None,
        *,
        lookback: int = 10,
        limit: int = 30,
    ) -> Dict[str, Any]:
        dates = self._recent_dates(trade_date, lookback)
        target = str(trade_date or (dates[-1] if dates else ""))
        history = [(date, self._load_screening(date)) for date in dates]
        by_code: Dict[str, Dict[str, Any]] = {}

        for date, data in history:
            for item in data.get("final") or []:
                code = normalize_stock_code(item.get("code") or item.get("stock_code") or "", add_suffix=False)
                if not code:
                    continue
                bucket = by_code.setdefault(code, {
                    "code": code,
                    "name": item.get("name") or "",
                    "appearances": 0,
                    "dates": [],
                    "scores": [],
                    "ranks": [],
                    "latest": None,
                    "last_item": None,
                    "last_date": "",
                    "leader_dates": [],
                })
                bucket["name"] = item.get("name") or bucket.get("name") or ""
                bucket["appearances"] += 1
                bucket["dates"].append(date)
                bucket["scores"].append(_to_float(item.get("score")))
                item_rank = int(_to_float(item.get("rank"), 999))
                bucket["ranks"].append(item_rank)
                bucket["last_item"] = item
                bucket["last_date"] = date
                if item_rank <= 3:
                    bucket["leader_dates"].append(date)
                if date == target:
                    bucket["latest"] = item

        rows = [self._build_pool_row(raw, target, dates) for raw in by_code.values()]
        rows = [row for row in rows if row is not None]
        rows.sort(
            key=lambda row: (
                row["pool_type_order"],
                int(row.get("leader_age_days") or 0),
                -_to_float(row.get("leader_score")),
                int(row.get("latest_rank") or 999),
            )
        )
        selected = rows[: max(int(limit or 30), 1)]
        for idx, row in enumerate(selected, start=1):
            row["pool_rank"] = idx
            row.pop("pool_type_order", None)

        counts: Dict[str, int] = {}
        for row in selected:
            counts[row["pool_type"]] = counts.get(row["pool_type"], 0) + 1

        return {
            "ok": bool(selected),
            "trade_date": target,
            "lookback": len(dates),
            "dates": dates,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": counts,
            "rows": selected,
        }

    def _build_pool_row(self, raw: Dict[str, Any], target: str, dates: List[str]) -> Optional[Dict[str, Any]]:
        latest = raw.get("latest")
        leader_dates = sorted(set(raw.get("leader_dates") or []))
        if not leader_dates:
            return None

        source = latest or raw.get("last_item") or {}
        source_date = target if latest is not None else str(raw.get("last_date") or "")
        metrics = source.get("metrics") or {}
        context = source.get("context") or {}
        latest_rank = int(_to_float(source.get("rank"), 999)) if latest else None
        source_rank_value = int(_to_float(source.get("rank"), 999)) if source else 999
        source_rank = source_rank_value if source_rank_value < 999 else None
        latest_score = _to_float(source.get("score"), max(raw.get("scores") or [0.0]))
        best_rank = min(raw.get("ranks") or [999])
        avg_score = sum(raw.get("scores") or [0.0]) / max(len(raw.get("scores") or []), 1)
        appearance_score = min(100.0, raw.get("appearances", 0) / max(len(dates), 1) * 100.0)
        rank_score = max(0.0, 100.0 - (float(source_rank or best_rank or 999) - 1.0) * 9.0)
        tech_score = _to_float(metrics.get("tech_score"), _to_float(source.get("tech_score"), 50.0))
        liquidity = _to_float(metrics.get("stk_liquidity_percentile"), _to_float(context.get("liquidity_score"), 50.0))
        new_high = _to_float(metrics.get("stk_new_high_20d"), _to_float(context.get("new_high_ratio"), 0.0) * 100.0)
        amount_ratio = _to_float(context.get("amount_ratio"), 1.0)
        vol_ratio = _to_float(context.get("vol_ratio"), 1.0)
        volume_score = (_score_between(amount_ratio, 0.7, 2.2) + _score_between(vol_ratio, 0.7, 2.2)) / 2.0
        pct_chg = _to_float(context.get("pct_chg"), 0.0)
        code = raw.get("code") or ""
        name = raw.get("name") or source.get("name") or ""
        limit_pct = get_price_limit_pct_points(code, name) or 10.0
        limit_ratio = limit_progress(pct_chg, code, name)
        limit_bonus = 5.0 if limit_ratio >= 0.95 else 0.0
        leader_score = (
            latest_score * 0.35
            + appearance_score * 0.16
            + rank_score * 0.14
            + tech_score * 0.12
            + liquidity * 0.10
            + new_high * 0.08
            + volume_score * 0.05
            + limit_bonus
        )
        leader_score = max(0.0, min(100.0, leader_score))

        last_leader_date = leader_dates[-1] if leader_dates else ""
        target_index = dates.index(target) if target in dates else max(len(dates) - 1, 0)
        leader_index = dates.index(last_leader_date) if last_leader_date in dates else target_index
        leader_age_days = max(target_index - leader_index, 0)
        if latest and (latest_rank or 999) <= 3:
            pool_type = "核心龙头"
            order = 0
            leader_time_label = "当日龙头"
        else:
            pool_type = "近期龙头"
            order = 1
            leader_time_label = "上一交易日龙头" if leader_age_days == 1 else f"{leader_age_days}个交易日前龙头"

        reasons = self._reasons(
            pool_type, source_rank, source_date, target, last_leader_date,
            leader_score, raw, context, metrics, code, name,
        )
        return {
            "code": code,
            "name": name,
            "pool_type": pool_type,
            "pool_type_order": order,
            "leader_score": round(leader_score, 2),
            "latest_rank": latest_rank,
            "source_rank": source_rank,
            "source_date": source_date,
            "is_today_candidate": latest is not None,
            "best_rank": best_rank if best_rank != 999 else None,
            "avg_score": round(avg_score, 2),
            "appearances": int(raw.get("appearances") or 0),
            "active_dates": raw.get("dates") or [],
            "leader_dates": leader_dates,
            "last_leader_date": last_leader_date,
            "leader_age_days": leader_age_days,
            "leader_time_label": leader_time_label,
            "latest_score": round(latest_score, 2),
            "pct_chg": round(pct_chg, 2),
            "limit_pct": round(limit_pct, 2),
            "limit_progress": round(limit_ratio, 4),
            "amount_ratio": round(amount_ratio, 2),
            "vol_ratio": round(vol_ratio, 2),
            "tech_score": round(tech_score, 2),
            "liquidity_score": round(liquidity, 2),
            "new_high_score": round(new_high, 2),
            "action": "只在高开且实时转强时确认；低开直接放弃",
            "reasons": reasons,
            "candidate_reasons": source.get("reasons") or [],
        }

    @staticmethod
    def _reasons(
        pool_type: str,
        source_rank: Optional[int],
        source_date: str,
        target: str,
        last_leader_date: str,
        leader_score: float,
        raw: Dict[str, Any],
        context: Dict[str, Any],
        metrics: Dict[str, Any],
        code: Any,
        name: Any,
    ) -> List[str]:
        reasons: List[str] = []
        if source_rank:
            prefix = "当日" if source_date == target else source_date
            reasons.append(f"{prefix}因子筛选排名第 {source_rank}")
        reasons.append(f"龙头池评分 {leader_score:.1f}，归类为{pool_type}")
        if last_leader_date and last_leader_date != target:
            reasons.append(f"最近一次龙头身份来自 {last_leader_date}")
        if raw.get("appearances", 0) >= 2:
            reasons.append(f"近 {len(raw.get('dates') or [])} 次进入候选，具备持续关注价值")
        pct_chg = _to_float(context.get("pct_chg"))
        limit_pct = get_price_limit_pct_points(code, name) or 10.0
        progress = limit_progress(pct_chg, code, name)
        if progress >= 0.95:
            reasons.append(f"当日涨停进度接近满档（{limit_pct:.0f}cm进度 {progress * 100:.0f}%），涨幅 {_pct_text(pct_chg)}")
        elif progress >= 0.60:
            reasons.append(f"当日强涨幅 {_pct_text(pct_chg)}，{limit_pct:.0f}cm涨停进度 {progress * 100:.0f}%")
        amount_ratio = _to_float(context.get("amount_ratio"), 1.0)
        if amount_ratio >= 1.2:
            reasons.append(f"成交额相对5日放大 {amount_ratio:.2f} 倍")
        elif amount_ratio < 0.9:
            reasons.append(f"成交额相对5日偏弱 {amount_ratio:.2f} 倍，实时确认要更严格")
        tech = _to_float(metrics.get("tech_score"), 0.0)
        if tech >= 80:
            reasons.append(f"技术强度 {tech:.1f} 分，处于强势区")
        return reasons[:5]

    def _recent_dates(self, trade_date: Optional[str], lookback: int) -> List[str]:
        dates = self._available_dates()
        if not dates:
            return []
        target = str(trade_date or dates[-1])
        if target in dates:
            end_idx = dates.index(target) + 1
        else:
            end_idx = len([d for d in dates if d <= target]) or len(dates)
        return dates[max(0, end_idx - max(int(lookback or 5), 1)):end_idx]

    def _available_dates(self) -> List[str]:
        dates = []
        for path in sorted(self.screening_dir.glob("screening_*.json")):
            stem = path.stem.replace("screening_", "")
            if len(stem) == 8 and stem.isdigit():
                dates.append(stem)
        return sorted(set(dates))

    def _load_screening(self, date: str) -> Dict[str, Any]:
        path = self.screening_dir / f"screening_{date}.json"
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"trade_date": date, "final": []}


class IntradayStrengthService:
    """Realtime strength confirmation over the factor-native leader pool."""

    def __init__(self, *, quote_service: Any = None, pool_service: Optional[LeaderPoolService] = None) -> None:
        self.quote_service = quote_service
        self.pool_service = pool_service or LeaderPoolService()

    def build(
        self,
        trade_date: Optional[str] = None,
        *,
        lookback: int = 10,
        limit: int = 30,
    ) -> Dict[str, Any]:
        pool = self.pool_service.build_pool(trade_date, lookback=lookback, limit=limit)
        rows = pool.get("rows") or []
        quotes = self._quote_map(row.get("code") for row in rows)
        enriched = [self._build_row(row, quotes.get(row.get("code") or "", {})) for row in rows]
        enriched.sort(key=lambda row: (row["status_order"], -_to_float(row.get("turn_score")), int(row.get("pool_rank") or 999)))
        for row in enriched:
            row.pop("status_order", None)
        counts: Dict[str, int] = {}
        for row in enriched:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "ok": bool(enriched),
            "trade_date": pool.get("trade_date"),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "thresholds": {
                "cancel_low_open": "低开直接放弃",
                "turning_pct": 1.5,
                "confirmed_pct": 3.0,
            },
            "counts": counts,
            "rows": enriched,
        }

    def _quote_map(self, codes: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
        service = self._ensure_quote_service()
        normalized = [normalize_stock_code(code, add_suffix=False) for code in codes or []]
        normalized = [code for code in dict.fromkeys(normalized) if code]
        if service is None or not normalized:
            return {}
        try:
            payload = service.get_quotes(normalized)
        except Exception:
            return {}
        return {
            normalize_stock_code(row.get("code") or "", add_suffix=False): row
            for row in payload.get("quotes") or []
            if row.get("code")
        }

    def _ensure_quote_service(self):
        if self.quote_service is not None:
            return self.quote_service
        try:
            from core.realtime.quote_service import RealtimeQuoteService

            self.quote_service = RealtimeQuoteService()
            return self.quote_service
        except Exception:
            return None

    def _build_row(self, pool_row: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
        pre_close = _to_float(quote.get("pre_close"))
        open_price = _to_float(quote.get("open_price"))
        last_price = _to_float(quote.get("last_price"))
        high_price = _to_float(quote.get("high_price"))
        change_pct = _to_float(quote.get("change_pct"))
        open_gap = (open_price / pre_close - 1.0) * 100.0 if open_price > 0 and pre_close > 0 else None
        intraday_lift = (last_price / open_price - 1.0) * 100.0 if last_price > 0 and open_price > 0 else None
        high_from_open = (high_price / open_price - 1.0) * 100.0 if high_price > 0 and open_price > 0 else None
        status, order, reason = self._status(quote, open_gap, change_pct, intraday_lift)
        realtime_score = self._realtime_score(open_gap, change_pct, intraday_lift)
        turn_score = _to_float(pool_row.get("leader_score")) * 0.45 + realtime_score * 0.55
        if status == "cancelled":
            turn_score = min(turn_score, 45.0)
        return {
            **pool_row,
            "status": status,
            "status_order": order,
            "status_text": {"confirmed": "转强确认", "turning": "转强观察", "observe": "继续观察", "cancelled": "取消"}[status],
            "turn_score": round(max(0.0, min(100.0, turn_score)), 2),
            "reason": reason,
            "quote_time": quote.get("received_at") or quote.get("time") or "",
            "quote_source": quote.get("source") or "",
            "is_stale": bool(quote.get("is_stale")),
            "last_price": round(last_price, 3) if last_price else None,
            "open_price": round(open_price, 3) if open_price else None,
            "pre_close": round(pre_close, 3) if pre_close else None,
            "change_pct": round(change_pct, 2),
            "open_gap_pct": round(open_gap, 2) if open_gap is not None else None,
            "intraday_lift_pct": round(intraday_lift, 2) if intraday_lift is not None else None,
            "high_from_open_pct": round(high_from_open, 2) if high_from_open is not None else None,
        }

    @staticmethod
    def _status(
        quote: Dict[str, Any],
        open_gap: Optional[float],
        change_pct: float,
        intraday_lift: Optional[float],
    ) -> tuple[str, int, str]:
        if not quote:
            return "observe", 2, "未获取到实时行情，保持观察"
        if quote.get("is_stale"):
            return "observe", 2, "行情可能过期，保持观察"
        if open_gap is not None and open_gap < 0:
            return "cancelled", 3, f"低开 {open_gap:.2f}%，按规则直接放弃"
        if change_pct <= -2.0:
            return "cancelled", 3, f"实时跌幅 {change_pct:.2f}% 触发取消线"
        if open_gap is not None and open_gap >= 0 and change_pct >= 3.0:
            lift_txt = f"，开盘后拉升 {intraday_lift:.2f}%" if intraday_lift is not None else ""
            return "confirmed", 0, f"高开 {open_gap:.2f}% 且实时涨幅 {change_pct:.2f}%{lift_txt}"
        if open_gap is not None and open_gap >= 0 and change_pct >= 1.5:
            return "turning", 1, f"高开 {open_gap:.2f}% 且实时涨幅 {change_pct:.2f}%，进入转强观察"
        if open_gap is not None and open_gap >= 0:
            return "observe", 2, f"高开 {open_gap:.2f}%，但实时涨幅 {change_pct:.2f}% 未达到转强线"
        return "observe", 2, "行情不完整，保持观察"

    @staticmethod
    def _realtime_score(open_gap: Optional[float], change_pct: float, intraday_lift: Optional[float]) -> float:
        gap_score = 50.0 if open_gap is None else _score_between(open_gap, -1.0, 4.0)
        pct_score = _score_between(change_pct, -2.0, 7.0)
        lift_score = 50.0 if intraday_lift is None else _score_between(intraday_lift, -1.0, 4.0)
        return gap_score * 0.35 + pct_score * 0.50 + lift_score * 0.15
