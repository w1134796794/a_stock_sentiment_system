"""Snapshot-first realtime entries with a shared minute-confirmation fallback."""
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
    resolve_entry_deadline,
)
from backtest.reversal_entry import STRUCTURAL_LABELS, STRUCTURAL_MODES
from backtest.trade_calendar import TradeCalendar
from core.factors.behavior_cycle import intraday_behavior_cycle
from core.realtime.models import normalize_stock_code
from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
from core.signals.confidence_service import ConfidenceService, HistoricalSignalStatsRepository
from core.signals.minute_amount_profile import MinuteAmountProfileRepository
from core.signals.sector_confirmation import make_sector_sync_checker, sector_names_from_row
from core.utils.price_limit import limit_up_price
from core.utils.stock_eligibility import new_entry_exclusion

MODE_LABELS = {
    **STRUCTURAL_LABELS,
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


def classify_entry_mode(open_price: Any, pre_close: Any, *, weak_max_gap=None, continuation_max_gap=None) -> str:
    """Classify the opening layer independently from minute confirmation."""
    from core.signals.opening import classify_opening_gap, opening_gap
    gap = opening_gap(open_price, pre_close)
    if gap is None:
        return ""
    if weak_max_gap is None or continuation_max_gap is None:
        from risk.risk_config import RiskConfig
        risk = RiskConfig.load()
        weak_max_gap = risk.weak_entry_max_gap if weak_max_gap is None else weak_max_gap
        continuation_max_gap = risk.continuation_max_gap if continuation_max_gap is None else continuation_max_gap
    return classify_opening_gap(gap,
                                weak_max_gap=weak_max_gap, continuation_max_gap=continuation_max_gap)


def entry_mode_text(mode: str) -> str:
    return MODE_LABELS.get(str(mode or ""), "待开盘确认")


class RealtimeEntrySignalService:
    """Prefer live snapshots; retain the shared minute evaluator as fallback."""

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
        quote_service: Any = None,
        board_feedback_repository: Any = None,
        frozen_bundle=None,
    ) -> None:
        self.dm = data_manager
        self.quote_service = quote_service
        from core.signals.production_rules import minute_evaluator
        self.evaluator = evaluator or minute_evaluator()
        self._automatic_evaluator = self.evaluator if evaluator is None else None
        self._evaluator_day_key = None
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
        self.board_feedback_repository = board_feedback_repository
        self._feedback_inputs = None
        self.frozen_bundle = frozen_bundle

    def _frozen(self, market_date):
        frozen = getattr(self, "frozen_bundle", None)
        if frozen and frozen.get("market_date") == market_date:
            return frozen
        from core.operations.preopen_evidence import PreopenEvidenceStore
        return PreopenEvidenceStore().load(self.calendar.prev(market_date), market_date)

    def evaluate(
        self,
        rows: Iterable[Dict[str, Any]],
        quotes: Dict[str, Dict[str, Any]],
        *,
        market_date: str,
        execution: Optional[Dict[str, Any]] = None,
        on_evaluated=None,
        evaluate_codes=None,
    ) -> Dict[str, Dict[str, Any]]:
        # UI diagnostics and live matching can request different market dates.
        with self._lock:
            return self._evaluate_batch(rows, quotes, market_date=market_date, execution=execution,
                                        on_evaluated=on_evaluated, evaluate_codes=evaluate_codes)

    def _evaluate_batch(self, rows, quotes, *, market_date, execution=None, on_evaluated=None, evaluate_codes=None):
        from core.utils.stock_eligibility import new_entry_exclusion
        source_rows = [dict(row or {}) for row in rows or [] if not new_entry_exclusion(row or {})]
        market_date = str(market_date or "").replace("-", "")[:8]
        if not market_date:
            return {}
        bundle = None
        if self.evaluator is self._automatic_evaluator:
            from core.operations.preopen_evidence import PreopenEvidenceStore
            from core.signals.production_rules import minute_evaluator
            from risk.risk_config import RiskConfig
            bundle = self._frozen(market_date)
            key = (market_date, bundle['bundle_id'] if bundle else 'unfrozen')
            if key != self._evaluator_day_key:
                risk = RiskConfig.from_dict(bundle['inputs']['risk_config']) if bundle and bundle['inputs'].get('risk_config') else RiskConfig.load()
                self.evaluator = self._automatic_evaluator = minute_evaluator(risk)
                self._evaluator_day_key = key
        if type(self.sector_breadth) is RealtimeSectorBreadthProvider:
            if self.evaluator is not self._automatic_evaluator:
                from core.operations.preopen_evidence import PreopenEvidenceStore
                bundle = self._frozen(market_date)
            self.sector_breadth._batch_frozen_context = (market_date, bundle)

        frames: Dict[str, pd.DataFrame] = {}
        minute_read_ms = {}
        lazy_minutes = type(self.sector_breadth) is RealtimeSectorBreadthProvider
        for row in source_rows:
            code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
            if code and code not in frames and not lazy_minutes:
                started = monotonic()
                frames[code] = self._minute_frame(code, market_date)
                minute_read_ms[code] = round((monotonic() - started) * 1000, 3)

        started = monotonic()
        previous = self._previous_daily_map(market_date)
        previous_read_ms = round((monotonic() - started) * 1000, 3)
        started = monotonic()
        if getattr(self.evaluator, "board_feedback_policy", {}).get("enabled"):
            codes = [normalize_stock_code(row.get("code") or row.get("stock_code"), add_suffix=False)
                     for row in source_rows]
            self._feedback_inputs = self._load_feedback_inputs(market_date, codes)
        feedback_read_ms = round((monotonic() - started) * 1000, 3)
        out: Dict[str, Dict[str, Any]] = {}
        for row in source_rows:
            code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
            if not code:
                continue
            if evaluate_codes is not None and code not in evaluate_codes:
                continue
            if getattr(self, "persistent_evaluation_enabled", False):
                # Capture per stock, not once for a potentially long serial batch.
                # Each stock's quote/minutes/sector checks still share one cutoff.
                provider = self._ensure_data_manager().provider
                view = provider.begin(market_date, asof=self.evaluation_clock())
                self.sector_breadth.market_view = view
            if code not in frames:
                started = monotonic()
                frames[code] = self._minute_frame(code, market_date)
                minute_read_ms[code] = round((monotonic() - started) * 1000, 3)
            quote_started = monotonic()
            if self.quote_service is not None:
                try:
                    quotes[code] = dict(self.quote_service.get_quote(code) or {})
                except Exception:
                    quotes[code] = {}
            quote = dict(quotes.get(code) or {})
            quote_read_ms = round((monotonic() - quote_started) * 1000, 3)
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
            out[code].setdefault("evaluation_timing", {}).update({
                "minute_read_ms": minute_read_ms.get(code), "quote_read_ms": quote_read_ms,
                "batch_previous_read_ms": previous_read_ms, "batch_feedback_read_ms": feedback_read_ms})
            if on_evaluated is not None:
                try:
                    on_evaluated(row, quote, out[code])
                except Exception as exc:
                    from loguru import logger
                    logger.exception("Per-stock confirmation enqueue failed")
                    out[code]["notification_enqueue_error"] = type(exc).__name__
        return out

    def _evaluate_one(
        self, row, quote, frame, frames, quotes, previous, market_date, all_rows, execution,
    ):
        started = monotonic()
        code = normalize_stock_code(row.get("code") or row.get("stock_code") or "", add_suffix=False)
        journal = getattr(self, "debug_journal", None)
        provider = getattr(self._ensure_data_manager(), "provider", None)
        view = getattr(provider, "current_view", None)
        read_start = max(0, len(view.audit)-2) if view is not None else 0
        try:
            payload = self._evaluate_entry(row, quote, frame, frames, quotes, previous,
                                           market_date, all_rows, execution)
        except Exception as exc:
            if journal is not None:
                journal.record("error", market_date, code, payload={"error_type": type(exc).__name__,
                    "error": str(exc), "reason": "stock_evaluation_failed"})
            raise
        from core.signals.production_rules import acceleration_eligibility
        payload["acceleration_eligibility"] = acceleration_eligibility(row)
        payload["execution_strategy_id"] = str(row.get("strategy_id") or "")
        payload["execution_sources"] = row.get("execution_sources") or []
        payload["execution_pool"] = row.get("execution_pool") or ""
        payload["execution_selection_policy"] = row.get("execution_selection_policy") or ""
        # Optional factor evidence must not replace the executable entry decision.
        payload["board_feedback"] = self._board_feedback_context(code, market_date)
        from core.realtime.trading_session import shanghai_now
        from datetime import datetime
        finished = shanghai_now()
        try:
            receipt = shanghai_now(datetime.fromisoformat(str(quote.get("received_at") or "")))
            receipt_age = round((finished - receipt).total_seconds(), 3)
        except (ValueError, TypeError):
            receipt_age = None
        payload.setdefault("evaluation_timing", {}).update({"finished_at": finished.isoformat(timespec="milliseconds"),
                                        "quote_age_at_completion_seconds": receipt_age,
                                        "duration_ms": round((monotonic() - started) * 1000, 3)})
        payload.setdefault("condition_checks", {}).update({
            "strategy_mode_allowed": payload.get("strategy_mode_allowed"),
            "quote_fresh": not bool(quote.get("is_stale")),
            "quote_received_at": quote.get("received_at") or "",
            "last_available_minute": str(frame.iloc[-1]["time"]) if not frame.empty else ""})
        if view is not None:
            payload["market_data_reads"] = view.audit[read_start:]
            payload["market_data_view"] = {"asof": view.asof.isoformat(), "watermark": view.watermark,
                                           "storage": "durable_spool_and_archive"}
        if not getattr(self, "record_execution_timing", True):
            payload.pop("evaluation_timing", None)
            payload["condition_checks"]["quote_fresh"] = None
            payload["condition_checks"]["basis"] = "historical_prefix_not_live_receipt"
        if journal is not None:
            journal.record("evaluation", market_date, code, payload={**payload,
                "name": row.get("name"), "strategy_id": row.get("strategy_id"),
                "strategy_execution": row.get("strategy_execution") or execution,
                "quote": {k: quote.get(k) for k in (
                    "date", "time", "received_at", "last_price", "open_price", "pre_close",
                    "bid1", "ask1", "vol_hand", "amount_yuan", "is_stale", "sample_interval_seconds")}})
        return payload

    def _evaluate_entry(
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
        from core.signals.opening import opening_context
        opening = opening_context(quote, previous)
        pre_close, open_price = opening["pre_close"], opening["open_price"]
        preliminary_mode = classify_entry_mode(open_price, pre_close,
            weak_max_gap=self.evaluator.weak_max_gap, continuation_max_gap=self.evaluator.continuation_max_gap)
        if quote_date and quote_date != market_date:
            return self._payload(
                EntryDecision("observing", reason=f"行情日期{quote_date}与当日{market_date}不一致"),
                preliminary_mode, market_date,
                opening_evidence=opening,
            )
        if bool(quote.get("is_stale")):
            return self._payload(
                EntryDecision(
                    "observing", reason="实时行情已过期，拒绝确认买点",
                    data_status="quote_stale", data_completeness=0.0,
                ),
                preliminary_mode, market_date,
                opening_evidence=opening,
            )

        if opening["open_gap"] is None:
            return self._payload(
                EntryDecision("data_insufficient", reason="当日真实开盘价或昨收价尚未取得，不使用首分钟价格代替",
                              data_status="missing_session_open", data_completeness=0),
                "", market_date,
                opening_evidence=opening,
            )

        row_execution = self._row_execution(row)
        if row_execution:
            execution = {**execution, **row_execution}
        gap = opening["open_gap"]
        mode = preliminary_mode
        allowed_modes = normalize_strategy_entry_modes(
            execution.get("allowed_entry_modes") or []
        )
        structural = [item for item in STRUCTURAL_MODES if item in allowed_modes]
        if structural:
            mode = structural[0]
        structures = execution.get("structures") or row.get("reversal_structures") or {}
        if not frame.empty:
            quote_time = str(quote.get("time") or quote.get("received_at") or "").replace("T", " ").split(" ")[-1]
            if len(quote_time) >= 5 and quote_time[2] == ":":
                from core.data.minute_clock import completed_minutes
                frame = completed_minutes(frame, quote_time)
            else:
                return self._payload(EntryDecision("observing", reason="缺少行情时间，无法验证完整分钟"), mode, market_date,
                                     opening_evidence=opening)
        strategy_mode_allowed = not allowed_modes or mode in allowed_modes
        allowed_text = "、".join(
            MODE_LABELS.get(item, item) for item in sorted(allowed_modes)
        )
        from backtest.evidence_policy import requires_auction
        auction = self._auction(code, market_date) if mode == ENTRY_CONTINUATION or requires_auction(execution) else {}
        if requires_auction(execution) and not (_float(auction.get('竞价成交额')) > 0 and _float(auction.get('竞价成交量')) > 0):
            return self._payload(EntryDecision('data_insufficient', reason='策略要求的集合竞价证据缺失',
                data_status='missing_auction', data_completeness=0), mode, market_date, opening_evidence=opening)
        amount_ratio = self._metric(row, "amount_ratio", 0.0)
        previous_amount = self._previous_amount_yuan(previous)
        _, profile_samples = self.amount_profiles.expected_fraction(previous_amount, "10:00:00")

        def expected_fraction(time_text: str) -> Optional[float]:
            fraction, _ = self.amount_profiles.expected_fraction(previous_amount, time_text)
            return fraction

        sector_started = monotonic()
        sector_checker, sector_detail = self._sector_checker(
            row, code, all_rows, frames, quotes, market_date,
        )
        sector_prepare_ms = round((monotonic() - sector_started) * 1000, 3)
        evaluate = self.evaluator.evaluate_strategy if structural else self.evaluator.evaluate
        strategy_args = {"execution": {**execution, "structures": structures}} if structural else {}
        minute_args = dict(
            **strategy_args,
            mode=mode,
            bars=frame,
            open_gap=gap,
            prev_close=pre_close,
            previous_session_close=_float(previous.get("close")),
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
            structure=structures.get(mode, {}),
            confirmation_deadline=resolve_entry_deadline(execution, mode),
        )
        decision = None
        primary_enabled = getattr(self, "snapshot_primary_enabled", True)
        fallback_reason = ("structural_strategy_uses_minute_confirmation" if structural else
                           "historical_minute_diagnostic_without_snapshots" if not primary_enabled else "")
        decision_ms = 0.0
        snapshot_started = monotonic()
        snapshot_input = {}
        if not structural and primary_enabled:
            try:
                snapshots = self._snapshot_frame(code, market_date)
                fields = [k for k in ("code", "date", "time", "received_at", "last_price", "source", "quality_ok",
                                     "vol_hand", "amount_yuan", "open_price", "pre_close", "ask1", "low_price")
                          if k in snapshots.columns]
                snapshot_input = {"count": len(snapshots), "recent": snapshots[fields].tail(3).to_dict("records"),
                    "required_count": self.snapshot_signals.consecutive_ticks,
                    "max_age_seconds": self.snapshot_signals.max_snapshot_age_seconds}
                decision, fallback_reason = self.snapshot_signals.evaluate_primary(
                    code=code,
                    trade_date=market_date,
                    mode=mode,
                    minute_bars=frame,
                    snapshots=snapshots,
                    prev_close=pre_close,
                    open_gap=gap,
                    sector_sync=sector_checker,
                    is_leader=self._is_leader(row),
                    limit_price=_float(limit_up_price(pre_close, code, name)),
                    evaluator=self.evaluator,
                    previous_amount=previous_amount,
                    expected_amount_fraction=expected_fraction if self.amount_profiles.available else None,
                    profile_samples=profile_samples,
                    auction_amount=_float(auction.get("竞价成交额")),
                    auction_volume=_float(auction.get("竞价成交量")),
                    previous_volume=_float(previous.get("vol_hand"), _float(previous.get("vol"))),
                    confirmation_deadline=resolve_entry_deadline(execution, mode, self.evaluator.deadline),
                )
            except Exception as exc:
                from loguru import logger
                logger.exception("Snapshot primary evaluation failed for {}", code)
                decision = EntryDecision("data_insufficient", reason="快照主判断异常，保持观察",
                    data_status="snapshot_error", data_completeness=0,
                    condition_checks={"snapshot_error": type(exc).__name__})
        snapshot_ms = round((monotonic() - snapshot_started) * 1000, 3)
        if decision is None:
            decision_started = monotonic()
            evaluated = evaluate(**minute_args)
            if structural:
                mode, decision = evaluated
                mode = next((key for key, label in STRUCTURAL_LABELS.items() if label == decision.signal), mode)
            else:
                decision = evaluated
            decision_ms = round((monotonic() - decision_started) * 1000, 3)
        decision.condition_checks.update({"source_policy": "snapshot_first" if primary_enabled else "minute_diagnostic",
            "selected_source": "eltdx_snapshot" if str(decision.data_status).startswith("snapshot_") else "minute_fallback",
            "fallback_reason": fallback_reason, "snapshot_input": snapshot_input})
        selection_checks = dict(decision.condition_checks)
        if not strategy_mode_allowed:
            decision, strength_detected = self._disallowed_mode_observation(
                decision=decision,
                mode=mode,
                frame=frame,
                limit_price=_float(limit_up_price(pre_close, code, name)),
                allowed_text=allowed_text,
                open_gap=gap,
            )
        else:
            strength_detected = decision.status in {
                "filled", "confirmed", "signal_unfilled",
            }
        decision.condition_checks.update({**selection_checks, **decision.condition_checks})
        if not self.evaluator.confirmation_policy.require_sector:
            sector_checker(decision.confirm_time or str(quote.get("time") or "15:00:00"))
            decision.condition_checks["sector_required"] = False
        payload = self._payload(
            decision,
            mode,
            market_date,
            sector_detail=sector_detail,
            strategy_mode_allowed=strategy_mode_allowed,
            strength_detected=strength_detected,
            stats_context=row,
            opening_evidence=opening,
        )
        payload["evaluation_timing"] = {"sector_prepare_ms": sector_prepare_ms,
                                         "minute_decision_ms": decision_ms, "snapshot_ms": snapshot_ms}
        if not self.evaluator.confirmation_policy.require_sector:
            payload["sector_trend"] = sector_detail.get("trend_factor", {})
        if mode in STRUCTURAL_MODES:
            payload["structure"] = structures.get(mode, {})
            payload["structural_stop"] = (structures.get(mode) or {}).get("protection")
        payload["strategy_execution"] = execution
        payload["strategy_version"] = row.get("strategy_version", "")
        deadline = resolve_entry_deadline(execution, mode, self.evaluator.deadline)
        if decision.confirm_time and decision.confirm_time > deadline:
            return self._payload(EntryDecision("cancelled", reason="超过策略确认截止时间"), mode, market_date,
                                 opening_evidence=opening)
        if decision.confirm_time:
            from core.signals.case_features import build_signal_case
            snapshot = build_signal_case({**row, **quote, **payload}, {"market_date": market_date}, minutes=frame)
            payload["signal_feature_snapshot"] = {"features": snapshot["features"], "quality": snapshot["quality"]}
        return payload

    def _load_feedback_inputs(self, market_date, codes):
        from core.operations.preopen_evidence import PreopenEvidenceStore
        from core.realtime.board_feedback_repository import BoardFeedbackRepository
        try:
            if self.board_feedback_repository is None:
                self.board_feedback_repository = BoardFeedbackRepository(
                    cache_dir=getattr(self.dm, "cache_dir", None), calendar=self.calendar)
            frozen = self._frozen(market_date)
            return self.board_feedback_repository.capture(
                market_date, codes, frozen_inputs=frozen["inputs"] if frozen else None)
        except Exception as exc:
            from loguru import logger
            logger.warning("三板负反馈因子输入不可用，买点仍独立评估: {}", type(exc).__name__)
            return {"source_basis": "optional_history_unavailable"}

    def _board_feedback_context(self, code, market_date):
        from core.signals.board_feedback import assess_feedback, feedback_policy
        policy = getattr(self.evaluator, "board_feedback_policy", feedback_policy())
        inputs = self._feedback_inputs
        if inputs is None and policy["enabled"]:
            inputs = self._load_feedback_inputs(market_date, [code])
        return assess_feedback(inputs, code, market_date, policy)

    @staticmethod
    def _disallowed_mode_observation(
        *,
        decision: EntryDecision,
        mode: str,
        frame: pd.DataFrame,
        limit_price: float,
        allowed_text: str,
        open_gap: float,
    ) -> Tuple[EntryDecision, bool]:
        """Keep strong price evidence visible without authorising a strategy buy."""
        mode_text = MODE_LABELS.get(mode, mode)
        data = normalize_minute_bars(frame)
        if mode == ENTRY_ACCELERATION and not data.empty and limit_price > 0:
            limit_rows = data[
                (data["high"] >= limit_price * 0.998)
                | (data["close"] >= limit_price * 0.998)
            ]
            if not limit_rows.empty:
                confirm_time = str(limit_rows.iloc[0].get("time") or "")
                # The signal is only known after the confirming minute closes.
                # A lower price inside that same minute is not a future fill window.
                after_confirm = data[data["time"] > confirm_time]
                reopened = bool((after_confirm["low"] < limit_price * 0.998).any())
                status = "observing" if reopened else "signal_unfilled"
                execution_reason = (
                    f"；但策略仅允许{allowed_text}，不触发买入"
                    if allowed_text else "；当前策略不触发买入"
                )
                reason = (
                    f"{mode_text}后快速触及涨停，走势已确认"
                    + ("，盘中曾打开" if reopened else "，封板后无可成交窗口")
                    + execution_reason
                )
                return (
                    EntryDecision(
                        status,
                        signal=mode_text,
                        reason=reason,
                        confirm_time=confirm_time,
                        open_gap_pct=open_gap,
                        data_status="strategy_mode_observation",
                    ),
                    True,
                )

        if decision.status in {"filled", "confirmed", "signal_unfilled"}:
            reason = (
                f"已识别{mode_text}走势；但策略仅允许{allowed_text}，不触发买入"
            )
            return (
                EntryDecision(
                    "observing",
                    signal=mode_text,
                    reason=reason,
                    confirm_time=decision.confirm_time,
                    open_gap_pct=open_gap,
                    amount_pace=decision.amount_pace,
                    sector_confirmed=decision.sector_confirmed,
                    data_status="strategy_mode_observation",
                    data_completeness=decision.data_completeness,
                ),
                True,
            )

        return (
            EntryDecision(
                "rejected",
                signal=mode_text,
                reason=f"该策略仅允许{allowed_text}，当前{mode_text}分层不执行",
                open_gap_pct=open_gap,
                data_status="strategy_mode_not_allowed",
                data_completeness=decision.data_completeness,
            ),
            False,
        )

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
        return classify_entry_mode(1 + gap, 1)

    def _payload(
        self, decision: EntryDecision, mode: str, market_date: str,
        *, sector_detail: Optional[Dict[str, Any]] = None,
        strategy_mode_allowed: bool = True,
        strength_detected: bool = False,
        stats_context: Optional[Dict[str, Any]] = None,
        opening_evidence: Optional[Dict[str, Any]] = None,
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
        context = stats_context or {}
        stats = self.signal_stats.get(
            signal_name, as_of_date=market_date, strategy_id=str(context.get("strategy_id") or ""),
            strategy_version=str(context.get("strategy_version") or ""),
            exit_rule_version=str(context.get("exit_rule_version") or ""),
            cost_version=str(context.get("cost_version") or ""),
        ) if signal_name else {}
        sector_complete = float((sector_detail or {}).get("data_completeness") or 0.0)
        data_completeness = min(float(decision.data_completeness), sector_complete or float(decision.data_completeness))
        if (sector_detail or {}).get("sector_required") is False:
            data_completeness = float(decision.data_completeness)
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
        confidence["probability_basis"] = "entry_mode_history"
        confidence["calibration_status"] = "uncalibrated"
        confidence["baseline_probability"] = None
        confidence["probability_lift"] = None
        if not stats:
            for field in ("candidate_probability", "baseline_probability", "probability_lift",
                          "expected_return_pct", "expected_gross_return_pct", "stop_probability",
                          "average_mfe_pct", "average_mae_pct", "probability_ci_low", "probability_ci_high"):
                confidence[field] = None
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
        from core.signals.production_rules import entry_parameters
        return {
            "market_date": market_date,
            "opening_evidence": opening_evidence or {},
            "entry_parameters": entry_parameters(getattr(self, 'evaluator', None)),
            "execution_strategy_id": context.get('strategy_id') or '',
            "entry_mode": mode,
            "entry_mode_text": entry_mode_text(mode),
            "signal_status": status,
            "signal_status_text": status_text,
            "signal": signal_name,
            "reason": decision.reason,
            "condition_checks": decision.condition_checks,
            "strategy_mode_allowed": bool(strategy_mode_allowed),
            "strength_detected": bool(strength_detected),
            "confirm_time": decision.confirm_time,
            "trigger_at": decision.trigger_at,
            "formal_confirmed_at": decision.formal_confirmed_at,
            "invalidated_at": decision.invalidated_at,
            "simulated_entry_at": decision.simulated_entry_at,
            "entry_time": decision.entry_time,
            "entry_price": decision.entry_price or None,
            "breakout_level": decision.breakout_level or None,
            "initial_confirm_time": decision.initial_confirm_time,
            "confirmation_policy": decision.confirmation_policy,
            "confirmation_reason": decision.confirmation_reason,
            "amount_pace": decision.amount_pace or None,
            "sector_confirmed": bool(decision.sector_confirmed),
            "sector_detail": sector_detail or {},
            "sector_confirmation_version": (sector_detail or {}).get("sector_confirmation_version", ""),
            "data_status": decision.data_status,
            "trigger_source": decision.condition_checks.get("selected_source") or (
                "eltdx_snapshot" if str(decision.data_status).startswith("snapshot_")
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
            "average_mfe_pct": confidence["average_mfe_pct"] if stats.get("average_mfe") is not None else None,
            "average_mae_pct": confidence["average_mae_pct"] if stats.get("average_mae") is not None else None,
            "confidence_grade": confidence["confidence_grade"],
            "historical_stats_basis": signal_name or "",
            "historical_stats_label": "入场模式历史统计" if signal_name else "",
            "historical_stats_status": stats.get("sample_status", "insufficient"),
            "historical_stats_versions": stats.get("versions") or {},
            "probability_calibration_status": "uncalibrated",
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
        from core.operations.preopen_evidence import PreopenEvidenceStore
        frozen = self._frozen(market_date)
        if frozen:
            return {normalize_stock_code(row.get("code") or row.get("ts_code") or "", add_suffix=False): row
                    for row in frozen["inputs"].get("daily_rows", [])}
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
        if not self.evaluator.confirmation_policy.require_sector:
            return self._trend_checker(sectors, market_date)
        if type(self.sector_breadth) is RealtimeSectorBreadthProvider:
            try:
                policy = self.evaluator.confirmation_policy
                window = 6 if policy.mode == "legacy" else max(policy.pullback_window, policy.sustain_minutes) + 5
                return self.sector_breadth.minute_checker(sectors, market_date, exclude_code=code,
                                                          history_minutes=window)
            except Exception as exc:
                return lambda at_time: None, {"assessed": True, "data_completeness": 0.0,
                    "reason": "板块闭合分钟留档读取失败", "error_type": type(exc).__name__,
                    "sector_confirmation_source": "collector_closed_quote_minutes"}
        try:
            try:
                state, detail = self.sector_breadth.evaluate(sectors, market_date, exclude_code=code)
            except TypeError:
                state, detail = self.sector_breadth.evaluate(sectors, market_date)
            if state is not None:
                from datetime import datetime, timedelta

                observed = str(detail.get("observed_at") or datetime.now().isoformat(timespec="seconds"))
                stamp = datetime.fromisoformat(observed)
                if stamp.tzinfo is not None:
                    from zoneinfo import ZoneInfo

                    stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                history = getattr(self, "_sector_observations", {})
                key = (market_date, tuple(sorted(sectors)), code)
                samples = history.setdefault(key, [])
                if not samples or samples[-1][0] != stamp:
                    samples.append((stamp, state))
                history[key] = samples[-120:]
                self._sector_observations = {k: v for k, v in history.items() if k[0] == market_date}

                def at_minute(at_time):
                    end = datetime.strptime(market_date + str(at_time)[:8], "%Y%m%d%H:%M:%S") + timedelta(minutes=1)
                    known = [(time, value) for time, value in history[key] if time <= end and (end-time).total_seconds() <= 90]
                    return known[-1][1] if known else None

                return at_minute, {**detail, "observed_at": observed,
                                   "sector_confirmation_source": "sector_index_breadth",
                                   "sector_confirmation_version": "sector_index_breadth_v2" if detail.get("sector_sync_policy_version")
                                   else "sector_index_breadth_v1"}
        except Exception:
            detail = {}
        if isinstance(self.sector_breadth, RealtimeSectorBreadthProvider):
            return lambda at_time: None, {**detail, 'data_completeness': 0.0,
                'reason': detail.get('reason') or '真实同花顺板块证据不足，不使用候选样本代替',
                'sector_confirmation_source': 'ths_constituents_missing'}
        # Candidate peers must also belong to a verified THS trading board.
        verified_sectors = set()
        verified_members = set()
        for name in sectors:
            try:
                members = self.sector_breadth._members(name, market_date)
            except Exception:
                members = set()
            if members:
                verified_sectors.add(name)
                verified_members.update(members)
        peer_rows = []
        for other in all_rows:
            other_code = normalize_stock_code(other.get("code") or other.get("stock_code") or "", add_suffix=False)
            if other_code not in verified_members:
                continue
            quote = quotes.get(other_code) or {}
            if new_entry_exclusion(other) or new_entry_exclusion(quote):
                continue
            peer_rows.append(other)
        detail = {**detail, "reason": "仅取得同候选板块样本"}
        checker = make_sector_sync_checker(
            code, verified_sectors, peer_rows, frames,
            previous_closes={peer: quote.get("pre_close") for peer, quote in quotes.items()},
            trade_date=market_date, evidence=detail,
        )
        if not detail["peer_codes"]:
            detail["reason"] = "板块指数与成分股实时数据不足"
        return checker, detail

    @staticmethod
    def _sectors(row: Dict[str, Any]) -> set[str]:
        return sector_names_from_row(row)

    def _trend_checker(self, sectors, market_date):
        from core.signals.sector_trend import BoardTrendReader, make_trend_checker
        weight = self.evaluator.confirmation_policy.sector_score_weight
        inputs = getattr(self, "board_trend_inputs", None)
        if inputs is not None:
            key = (market_date, id(inputs), tuple(sorted(sectors)), weight)
            cached = getattr(self, "_trend_checkers", {})
            if key not in cached:
                cached[key] = make_trend_checker(sectors, inputs, date=market_date, weight=weight,
                                                 clock=getattr(self, "clock", None))
                self._trend_checkers = cached
            return cached[key]
        try:
            if not hasattr(self, "_board_trend_reader"):
                self._board_trend_reader = BoardTrendReader()
            provider = getattr(self._ensure_data_manager(), "provider", None)
            view = getattr(provider, "current_view", None)
            return self._board_trend_reader.checker(sectors, market_date, weight, view=view)
        except Exception:
            return make_trend_checker(sectors, {}, date=market_date, weight=weight)

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
        from core.signals.production_rules import acceleration_eligibility
        return acceleration_eligibility(row)["eligible"]

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
            from core.realtime.read_only_market import ReadOnlyMarketData
            self.dm = ReadOnlyMarketData()
        except Exception:
            self.dm = None
        return self.dm


__all__ = [
    "MODE_LABELS",
    "RealtimeEntrySignalService",
    "classify_entry_mode",
    "entry_mode_text",
]
