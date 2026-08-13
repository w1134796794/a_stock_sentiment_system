"""Minute-based realtime entry confirmation shared by intraday views."""
from __future__ import annotations

import json
from threading import RLock
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from backtest.minute_entry import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_WEAK,
    EntryDecision,
    MinuteEntryEvaluator,
    normalize_minute_bars,
    normalize_strategy_entry_modes,
)
from backtest.trade_calendar import TradeCalendar
from core.factors.behavior_cycle import intraday_behavior_cycle
from core.realtime.models import normalize_stock_code
from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
from core.signals.confidence_service import ConfidenceService, HistoricalSignalStatsRepository
from core.signals.minute_amount_profile import MinuteAmountProfileRepository
from core.utils.price_limit import limit_up_price

MODE_LABELS = {
    ENTRY_WEAK: "弱转强",
    ENTRY_CONTINUATION: "强势延续",
    ENTRY_ACCELERATION: "高开加速",
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_entry_mode(open_price: Any, pre_close: Any) -> str:
    """Classify the opening layer independently from minute confirmation."""
    open_value = _float(open_price)
    previous_value = _float(pre_close)
    if open_value <= 0 or previous_value <= 0:
        return ""
    gap = open_value / previous_value - 1.0
    if gap <= 0.0100001:
        return ENTRY_WEAK
    if gap <= 0.0500001:
        return ENTRY_CONTINUATION
    return ENTRY_ACCELERATION


def entry_mode_text(mode: str) -> str:
    return MODE_LABELS.get(str(mode or ""), "待开盘确认")


class RealtimeEntrySignalService:
    """Evaluate current candidates with the same minute rules as the backtest."""

    def __init__(
        self,
        data_manager: Any = None,
        *,
        evaluator: Optional[MinuteEntryEvaluator] = None,
        minute_ttl_seconds: float = 2.5,
        calendar: Optional[TradeCalendar] = None,
        amount_profile_repository: Optional[MinuteAmountProfileRepository] = None,
        sector_breadth_provider: Any = None,
        signal_stats_repository: Optional[HistoricalSignalStatsRepository] = None,
        snapshot_signal_service: Any = None,
    ) -> None:
        self.dm = data_manager
        self.evaluator = evaluator or MinuteEntryEvaluator()
        self.minute_ttl_seconds = max(float(minute_ttl_seconds), 1.0)
        self.calendar = calendar or TradeCalendar()
        self.amount_profiles = amount_profile_repository or MinuteAmountProfileRepository()
        self.sector_breadth = sector_breadth_provider or RealtimeSectorBreadthProvider()
        self.signal_stats = signal_stats_repository or HistoricalSignalStatsRepository()
        if snapshot_signal_service is None:
            from core.realtime.snapshot_signal_service import SnapshotSignalService

            snapshot_signal_service = SnapshotSignalService()
        self.snapshot_signals = snapshot_signal_service
        self._minute_cache: Dict[Tuple[str, str], Tuple[float, pd.DataFrame]] = {}
        self._previous_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = RLock()

    def evaluate(
        self,
        rows: Iterable[Dict[str, Any]],
        quotes: Dict[str, Dict[str, Any]],
        *,
        market_date: str,
        execution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        source_rows = [dict(row or {}) for row in rows or []]
        market_date = str(market_date or "").replace("-", "")[:8]
        if not market_date:
            return {}

        frames: Dict[str, pd.DataFrame] = {}
        for row in source_rows:
            code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
            if code and code not in frames:
                frames[code] = self._minute_frame(code, market_date)

        previous = self._previous_daily_map(market_date)
        out: Dict[str, Dict[str, Any]] = {}
        for row in source_rows:
            code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
            if not code:
                continue
            quote = dict(quotes.get(code) or {})
            frame = frames.get(code, pd.DataFrame())
            out[code] = self._evaluate_one(
                row,
                quote,
                frame,
                frames,
                quotes,
                previous.get(code) or {},
                market_date,
                source_rows,
                execution or {},
            )
        return out

    def _evaluate_one(
        self,
        row: Dict[str, Any],
        quote: Dict[str, Any],
        frame: pd.DataFrame,
        frames: Dict[str, pd.DataFrame],
        quotes: Dict[str, Dict[str, Any]],
        previous: Dict[str, Any],
        market_date: str,
        all_rows: List[Dict[str, Any]],
        execution: Dict[str, Any],
    ) -> Dict[str, Any]:
        code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
        name = str(quote.get("name") or row.get("name") or "")
        quote_date = str(quote.get("date") or "").replace("-", "")[:8]
        pre_close = _float(quote.get("pre_close"), _float(previous.get("close")))
        open_price = _float(quote.get("open_price"))
        if not frame.empty:
            first = frame.iloc[0]
            open_price = open_price or _float(first.get("open"), _float(first.get("close")))
            pre_close = pre_close or _float(first.get("pre_close"))
        preliminary_mode = classify_entry_mode(open_price, pre_close)
        if quote_date and quote_date != market_date:
            return self._payload(
                EntryDecision("observing", reason=f"行情日期{quote_date}与当日{market_date}不一致"),
                preliminary_mode, market_date,
            )

        if open_price <= 0 or pre_close <= 0:
            return self._payload(
                EntryDecision("observing", reason="当日开盘价或昨收价尚未取得"),
                "", market_date,
            )

        row_execution = self._row_execution(row)
        if row_execution:
            execution = {**execution, **row_execution}
        gap = open_price / pre_close - 1.0
        mode = preliminary_mode
        allowed_modes = normalize_strategy_entry_modes(
            execution.get("allowed_entry_modes") or []
        )
        if allowed_modes and mode not in allowed_modes:
            allowed_text = "、".join(MODE_LABELS.get(item, item) for item in sorted(allowed_modes))
            return self._payload(
                EntryDecision(
                    "rejected",
                    signal=MODE_LABELS.get(mode, ""),
                    reason=f"该策略仅允许{allowed_text}，当前开盘分层不适用",
                    open_gap_pct=gap,
                ),
                mode,
                market_date,
            )
        auction = self._auction(code, market_date) if mode == ENTRY_CONTINUATION else {}
        amount_ratio = self._metric(row, "amount_ratio", 0.0)
        previous_amount = self._previous_amount_yuan(previous)
        _, profile_samples = self.amount_profiles.expected_fraction(previous_amount, "10:00:00")

        def expected_fraction(time_text: str) -> Optional[float]:
            fraction, _ = self.amount_profiles.expected_fraction(previous_amount, time_text)
            return fraction

        sector_checker, sector_detail = self._sector_checker(
            row, code, all_rows, frames, quotes, market_date,
        )
        decision = self.evaluator.evaluate(
            mode=mode,
            bars=frame,
            open_gap=gap,
            prev_close=pre_close,
            previous_amount=previous_amount,
            previous_volume=_float(previous.get("vol_hand"), _float(previous.get("vol"))),
            auction_amount=_float(auction.get("竞价成交额")),
            auction_volume=_float(auction.get("竞价成交量")),
            plan_amount_ratio=amount_ratio,
            limit_price=_float(limit_up_price(pre_close, code, name)),
            is_leader=self._is_leader(row),
            sector_sync=sector_checker,
            expected_amount_fraction=expected_fraction if self.amount_profiles.available else None,
            amount_profile_samples=profile_samples,
            live=True,
        )
        ticks = self._snapshot_frame(code, market_date)
        if not ticks.empty:
            try:
                sector_state = sector_checker(str(ticks.iloc[-1].get("time") or ""))
                snapshot_decision = self.snapshot_signals.evaluate(
                    code=code,
                    trade_date=market_date,
                    mode=mode,
                    minute_bars=frame,
                    snapshots=ticks,
                    prev_close=pre_close,
                    open_gap=gap,
                    sector_confirmed=sector_state,
                    is_leader=self._is_leader(row),
                    limit_price=_float(limit_up_price(pre_close, code, name)),
                    minute_decision=decision,
                )
                if snapshot_decision is not None:
                    decision = snapshot_decision
            except Exception:
                pass
        return self._payload(decision, mode, market_date, sector_detail=sector_detail)

    @staticmethod
    def _row_execution(row: Dict[str, Any]) -> Dict[str, Any]:
        value = row.get("strategy_execution") or row.get("策略执行") or {}
        if isinstance(value, dict):
            execution = dict(value)
        elif isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                execution = dict(parsed) if isinstance(parsed, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                execution = {}
        else:
            execution = {}
        row_modes = row.get("allowed_entry_modes") or []
        if row_modes:
            execution["allowed_entry_modes"] = list(row_modes)
        return execution

    @staticmethod
    def _mode_for_gap(gap: float) -> str:
        if gap <= 0.01:
            return ENTRY_WEAK
        if gap <= 0.05:
            return ENTRY_CONTINUATION
        return ENTRY_ACCELERATION

    def _payload(
        self, decision: EntryDecision, mode: str, market_date: str,
        *, sector_detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if decision.status in {"filled", "confirmed"}:
            status = "confirmed"
            status_text = "确认"
        elif decision.status == "signal_unfilled":
            status = "unfilled"
            status_text = "无法成交"
        elif decision.status in {"cancelled", "rejected"}:
            status = "cancelled"
            status_text = "取消"
        else:
            status = "observe"
            status_text = "观察"
        signal_name = decision.signal or MODE_LABELS.get(mode, "")
        stats = self.signal_stats.get(signal_name, as_of_date=market_date) if signal_name else {}
        sector_complete = float((sector_detail or {}).get("data_completeness") or 0.0)
        data_completeness = min(float(decision.data_completeness), sector_complete or float(decision.data_completeness))
        confidence = ConfidenceService.assess(
            calibrated_probability=stats.get("success_probability", 0.5),
            expected_return=stats.get("expected_return", 0.0),
            stop_probability=stats.get("stop_probability", 0.5),
            sample_size=stats.get("sample_size", 0),
            average_mfe=stats.get("average_mfe", 0.0),
            average_mae=stats.get("average_mae", 0.0),
            data_completeness=data_completeness,
            regime_match=1.0,
            tradability=0.0 if decision.status == "signal_unfilled" else 1.0,
            model_type=f"entry_{mode}",
            as_of_date=market_date,
        )
        behavior = intraday_behavior_cycle(
            entry_mode=mode,
            signal_status=decision.status,
            amount_pace=decision.amount_pace,
            sector_confirmed=decision.sector_confirmed,
            hold_minutes=decision.hold_minutes,
            false_break_count=decision.false_break_count,
            pullback_quality=decision.pullback_quality,
            active_buy_ratio=decision.active_buy_ratio,
        )
        return {
            "market_date": market_date,
            "entry_mode": mode,
            "entry_mode_text": entry_mode_text(mode),
            "signal_status": status,
            "signal_status_text": status_text,
            "signal": signal_name,
            "reason": decision.reason,
            "confirm_time": decision.confirm_time,
            "entry_time": decision.entry_time,
            "entry_price": decision.entry_price or None,
            "amount_pace": decision.amount_pace or None,
            "sector_confirmed": bool(decision.sector_confirmed),
            "sector_detail": sector_detail or {},
            "data_status": decision.data_status,
            "trigger_source": (
                "pytdx_3s" if str(decision.data_status).startswith("snapshot_")
                else "minute_fallback"
            ),
            "data_completeness": confidence["data_completeness"],
            "profile_samples": decision.profile_samples,
            "hold_minutes": decision.hold_minutes,
            "false_break_count": decision.false_break_count,
            "pullback_quality": decision.pullback_quality,
            "active_buy_ratio": decision.active_buy_ratio,
            "confidence": confidence,
            "success_probability": confidence["candidate_probability"] if stats else None,
            "historical_samples": confidence["sample_size"] if stats else None,
            "average_mfe_pct": confidence["average_mfe_pct"] if stats else None,
            "average_mae_pct": confidence["average_mae_pct"] if stats else None,
            "confidence_grade": confidence["confidence_grade"],
            "historical_stats_basis": signal_name or "",
            "behavior_state": behavior["dominant_state"],
            "behavior_state_label": behavior["dominant_label"],
            "behavior_state_probability": behavior["dominant_probability"],
            "behavior_state_scores": behavior["scores"],
            "behavior_state_probabilities": behavior["probabilities"],
            "behavior_atomic_evidence": behavior["atomic"],
        }

    def _minute_frame(self, code: str, market_date: str) -> pd.DataFrame:
        key = (market_date, code)
        now = monotonic()
        with self._lock:
            cached = self._minute_cache.get(key)
            if cached and now - cached[0] <= self.minute_ttl_seconds:
                return cached[1].copy()
        dm = self._ensure_data_manager()
        if dm is None:
            return pd.DataFrame()
        try:
            ts_code = normalize_stock_code(code, add_suffix=True)
            raw = dm.get_minute_bars_live(ts_code, market_date)
            frame = normalize_minute_bars(raw)
        except Exception:
            frame = pd.DataFrame()
        with self._lock:
            self._minute_cache[key] = (now, frame.copy())
        return frame

    def _previous_daily_map(self, market_date: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            cached = self._previous_cache.get(market_date)
            if cached is not None:
                return cached
        dm = self._ensure_data_manager()
        if dm is None:
            return {}
        try:
            previous_date = self.calendar.prev(market_date)
            frame = dm.get_all_stocks_daily(previous_date)
        except Exception:
            return {}
        if frame is None or frame.empty:
            with self._lock:
                self._previous_cache[market_date] = {}
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for row in frame.to_dict("records"):
            code = normalize_stock_code(row.get("code") or row.get("ts_code") or "", add_suffix=False)
            if code:
                out[code] = row
        with self._lock:
            self._previous_cache[market_date] = out
        return out

    def _snapshot_frame(self, code: str, market_date: str) -> pd.DataFrame:
        dm = self._ensure_data_manager()
        if dm is None or not hasattr(dm, "get_realtime_snapshot_ticks"):
            return pd.DataFrame()
        try:
            return dm.get_realtime_snapshot_ticks(
                normalize_stock_code(code, add_suffix=True), market_date,
            )
        except Exception:
            return pd.DataFrame()

    def _auction(self, code: str, market_date: str) -> Dict[str, Any]:
        dm = self._ensure_data_manager()
        if dm is None:
            return {}
        try:
            return dict(dm.get_auction_data(normalize_stock_code(code, add_suffix=True), market_date) or {})
        except Exception:
            return {}

    def _sector_checker(
        self,
        row: Dict[str, Any],
        code: str,
        all_rows: List[Dict[str, Any]],
        frames: Dict[str, pd.DataFrame],
        quotes: Dict[str, Dict[str, Any]],
        market_date: str,
    ) -> Tuple[Any, Dict[str, Any]]:
        sectors = self._sectors(row)
        try:
            state, detail = self.sector_breadth.evaluate(sectors, market_date)
            if state is not None:
                return (lambda _at_time: state), detail
        except Exception:
            detail = {}
        peers: List[str] = []
        for other in all_rows:
            other_code = normalize_stock_code(other.get("code") or other.get("stock_code") or "", add_suffix=False)
            if not other_code or other_code == code:
                continue
            if sectors.intersection(self._sectors(other)):
                peers.append(other_code)

        def checker(at_time: str) -> bool:
            positive = observed = 0
            for peer in peers:
                frame = frames.get(peer, pd.DataFrame())
                if frame.empty:
                    continue
                available = frame[frame["time"] <= str(at_time)]
                if available.empty:
                    continue
                previous_close = _float((quotes.get(peer) or {}).get("pre_close"))
                if previous_close <= 0 and "pre_close" in frame.columns:
                    previous_close = _float(frame.iloc[0].get("pre_close"))
                latest = _float(available.iloc[-1].get("close"))
                if previous_close <= 0 or latest <= 0:
                    continue
                observed += 1
                positive += int(latest >= previous_close)
            return (positive / observed >= 0.5) if observed else None

        return checker, (detail or {
            "reason": "仅取得同候选板块样本" if peers else "板块指数与成分股实时数据不足",
            "observed_members": len(peers),
            "data_completeness": 0.5 if peers else 0.0,
        })

    @staticmethod
    def _sectors(row: Dict[str, Any]) -> set[str]:
        raw = str(row.get("resonance_sectors") or row.get("所属板块") or "")
        return {part.strip() for part in raw.replace("；", ",").split(",") if part.strip()}

    @staticmethod
    def _metric(row: Dict[str, Any], name: str, default: float = 0.0) -> float:
        metrics = row.get("metrics") or {}
        context = row.get("context") or {}
        if name in metrics:
            return _float(metrics.get(name), default)
        if name in context:
            return _float(context.get(name), default)
        return _float(row.get(name), default)

    def _is_leader(self, row: Dict[str, Any]) -> bool:
        pool_type = str(row.get("pool_type") or "")
        if pool_type in {"核心龙头", "板块龙头"}:
            return True
        leader_quality = self._metric(row, "stk_kpl_leader_quality", 0.0)
        mainline = self._metric(row, "stk_sector_mainline_score", 0.0)
        board = self._metric(row, "stk_board_position", 0.0)
        return leader_quality >= 65.0 or (mainline >= 75.0 and board >= 60.0)

    @staticmethod
    def _previous_amount_yuan(row: Dict[str, Any]) -> float:
        if "amount_yuan" in row:
            return _float(row.get("amount_yuan"))
        # Tushare daily.amount is thousand yuan.
        return _float(row.get("amount")) * 1000.0

    def _ensure_data_manager(self):
        if self.dm is not None:
            return self.dm
        try:
            from config.settings import CACHE_DIR, TUSHARE_TOKEN
            from core.data.data_manager_main import DataManager

            self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        except Exception:
            self.dm = None
        return self.dm


__all__ = [
    "MODE_LABELS",
    "RealtimeEntrySignalService",
    "classify_entry_mode",
    "entry_mode_text",
]
