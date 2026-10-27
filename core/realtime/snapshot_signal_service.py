"""Confirm entries from live eltdx snapshots, with completed opening structure."""
from __future__ import annotations

from datetime import datetime
import math
from dataclasses import replace
from threading import RLock
from typing import Any, Dict, Optional, Tuple

import pandas as pd
from core.realtime.trading_session import shanghai_now

from backtest.minute_entry import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_WEAK,
    EntryDecision,
    normalize_minute_bars,
)

MODE_LABELS = {
    ENTRY_WEAK: "弱转强",
    ENTRY_CONTINUATION: "强势延续",
    ENTRY_ACCELERATION: "高开加速",
}


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class SnapshotSignalService:
    """Use snapshots as the primary trigger, not as proof of a minute signal."""

    def __init__(
        self,
        *,
        deadline: str = "10:00:00",
        consecutive_ticks: int = 2,
        max_snapshot_age_seconds: float = 8.0,
        clock: Any = None,
        state_backend: Any = None,
    ) -> None:
        self.deadline = deadline
        self.consecutive_ticks = max(int(consecutive_ticks), 2)
        self.max_snapshot_age_seconds = max(float(max_snapshot_age_seconds), 3.0)
        self.clock = clock or shanghai_now
        self.state_backend = state_backend
        self._pending: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._protection_lows = {}
        self._lock = RLock()

    def evaluate_primary(
        self, *, code, trade_date, mode, minute_bars, snapshots, prev_close,
        open_gap, sector_sync, is_leader, limit_price, evaluator,
        previous_amount=0.0, expected_amount_fraction=None, profile_samples=0,
        auction_amount=0.0, auction_volume=0.0, previous_volume=0.0,
        confirmation_deadline="",
    ):
        """Return (decision, fallback reason); a valid negative is never fallback."""
        key = (str(trade_date), str(code), str(mode))

        def unavailable(reason):
            with self._lock:
                self._pending.pop(key, None)
            return None, reason

        ticks = self._normalize_ticks(snapshots, trade_date)
        if len(ticks) < self.consecutive_ticks:
            return unavailable("snapshot_sequence_missing")
        recent = ticks.tail(self.consecutive_ticks)
        now = shanghai_now(self.clock())
        times = pd.to_datetime(trade_date + " " + recent["time"], errors="coerce")
        receipts = [pd.to_datetime(v, errors="coerce") for v in recent["received_at"]]
        if times.isna().any() or any(pd.isna(v) for v in receipts):
            return unavailable("snapshot_timestamp_missing")
        if (times.diff().dt.total_seconds().dropna() > self.max_snapshot_age_seconds).any():
            return unavailable("snapshot_gap")
        for at, received in zip(times, receipts):
            if (not -1 <= (now - shanghai_now(at.to_pydatetime())).total_seconds() <= self.max_snapshot_age_seconds
                    or not -1 <= (now - shanghai_now(received.to_pydatetime())).total_seconds() <= self.max_snapshot_age_seconds):
                return unavailable("snapshot_stale")
        if (not bool(recent["quality_ok"].all()) or "source" not in recent
                or not recent["source"].eq("eltdx_batch").all()):
            return unavailable("snapshot_invalid")
        if "code" not in recent or not recent["code"].astype(str).eq(str(code)).all():
            return unavailable("snapshot_identity_mismatch")
        required = ("vol_hand", "amount_yuan", "open_price", "pre_close")
        if any(column not in recent for column in required):
            return unavailable("snapshot_cumulative_fields_missing")
        totals = recent[list(required)].apply(pd.to_numeric, errors="coerce")
        if not totals.apply(lambda column: column.map(lambda v: math.isfinite(v) and v > 0)).all().all():
            return unavailable("snapshot_cumulative_fields_invalid")
        if (totals[["vol_hand", "amount_yuan"]].diff().dropna() < 0).any().any():
            return unavailable("snapshot_cumulative_regressed")
        true_open = prev_close * (1 + open_gap)
        if (not (totals["pre_close"] - prev_close).abs().le(0.011).all()
                or not (totals["open_price"] - true_open).abs().le(0.011).all()):
            return unavailable("snapshot_session_reference_mismatch")

        latest_time = str(recent.iloc[-1]["time"])
        minutes = normalize_minute_bars(minute_bars)
        if not minutes.empty:
            minutes = minutes[minutes["time"] < latest_time[:5] + ":00"].copy()
        checks = {"basis": "eltdx_snapshot_primary_v2_temporal_protection", "snapshot_count": len(recent),
                  "snapshot_received_at": str(recent.iloc[-1]["received_at"]),
                  "minute": latest_time, "quote_fresh": True}

        def stop(reason, status="observing", data_status="snapshot_observing"):
            with self._lock:
                self._pending.pop(key, None)
            return EntryDecision(status, MODE_LABELS.get(mode, ""), reason,
                open_gap_pct=open_gap, data_status=data_status,
                data_completeness=0.0 if data_status in {"snapshot_structure_pending", "snapshot_missing_sector", "snapshot_missing_amount_profile"} else 1.0,
                condition_checks={"latest": {"minute": latest_time, "checks": dict(checks)},
                                  "source_policy": "snapshot_first"}), ""

        opening_count = evaluator.opening_window_minutes(mode)
        opening_end = f"09:{30 + opening_count}:00"
        opening = minutes[minutes["time"].isin([f"09:{m}:00" for m in range(31, 31 + opening_count)])] if not minutes.empty else minutes
        checks.update(opening_minutes_required=opening_count, opening_minutes_available=len(opening),
                      confirmation_deadline=confirmation_deadline or evaluator.deadline,
                      max_snapshot_age_seconds=self.max_snapshot_age_seconds)
        if len(opening) != opening_count:
            return stop(f"等待完整开盘前{opening_count}分钟结构，不以稀疏快照替代高低点", data_status="snapshot_structure_pending")
        checks["opening_bar_kinds"] = sorted(set(opening["bar_kind"]))
        if not opening["ohlc_evidence"].all():
            return stop("开盘结构缺少真实分钟OHLC，分时价格点不能替代高低点", data_status="snapshot_structure_pending")
        checks["opening_structure_complete"] = True
        deadline = confirmation_deadline or evaluator.deadline
        if latest_time > deadline:
            return stop(f"{deadline[:5]}前未完成3秒快照确认", "cancelled")
        allowed = {
            ENTRY_WEAK: evaluator.weak_min_gap <= open_gap <= evaluator.weak_max_gap,
            ENTRY_CONTINUATION: evaluator.weak_max_gap < open_gap <= evaluator.continuation_max_gap,
            ENTRY_ACCELERATION: open_gap > evaluator.continuation_max_gap and is_leader,
        }.get(mode, False)
        checks["opening_gap_allowed"] = bool(allowed)
        checks.update(open_gap=open_gap, is_leader=bool(is_leader), weak_min_gap=evaluator.weak_min_gap,
                      weak_max_gap=evaluator.weak_max_gap, continuation_max_gap=evaluator.continuation_max_gap)
        if not allowed:
            return stop("开盘分层或龙头身份不满足当前入场模式", "rejected")
        opening_high, opening_low = float(opening["high"].max()), float(opening["low"].min())
        protection = opening_low * 0.999
        protection_ok, protection_detail = self._protection(
            key, minutes, ticks, opening_end, protection)
        checks.update(opening_high=opening_high, opening_low=opening_low, **protection_detail)
        checks["protection"] = protection_ok
        if not protection_ok and (mode == ENTRY_WEAK or evaluator.confirmation_policy.mode == "volume_price"):
            decision, fallback = stop(f"已跌破开盘前{opening_count}分钟保护低点", "cancelled")
            return replace(decision, invalidated_at=protection_detail["first_break_at"] or latest_time), fallback
        auction_available = auction_amount > 0 and auction_volume > 0 and previous_volume > 0
        checks["auction_available"] = auction_available
        if mode == ENTRY_CONTINUATION and auction_available:
            auction_ok = auction_amount >= evaluator.min_auction_amount and auction_volume / previous_volume >= evaluator.min_auction_volume_ratio
            checks["auction_allowed"] = auction_ok
            if not auction_ok:
                return stop("竞价成交额或竞价量比不足")

        # Snapshot volume is in hands, amount in yuan; never use a rolling-window
        # weighted price as the full-session VWAP.
        vwaps = totals["amount_yuan"] / (totals["vol_hand"] * 100)
        if not vwaps.map(lambda v: math.isfinite(v) and v > 0).all():
            return unavailable("snapshot_vwap_invalid")
        vwap = float(vwaps.iloc[-1])
        prices = recent["last_price"]
        level = max(opening_high, prev_close) if mode == ENTRY_WEAK else opening_high
        checks.update({"previous_close": bool((prices >= prev_close).all()),
                       "vwap": bool((prices >= vwaps).all()), "vwap_value": vwap,
                       "breakout": bool((prices > level).all()), "breakout_level": level})
        fraction = expected_amount_fraction(latest_time) if expected_amount_fraction else None
        pace = (float(totals.iloc[-1]["amount_yuan"]) / (previous_amount * fraction)
                if previous_amount > 0 and fraction is not None and fraction > 0 else None)
        minimum = max(evaluator.min_amount_pace, 1.0) if mode == ENTRY_CONTINUATION and not auction_available else evaluator.min_amount_pace
        checks["amount_pace"] = pace
        checks["amount_pace_allowed"] = (minimum <= pace <= evaluator.max_amount_pace) if pace is not None and math.isfinite(pace) else None
        checks.update(amount_pace_min=minimum, amount_pace_max=evaluator.max_amount_pace,
                      min_local_volume_ratio=evaluator.confirmation_policy.min_volume_ratio,
                      max_vwap_distance=evaluator.confirmation_policy.max_vwap_distance,
                      max_breakout_extension=evaluator.confirmation_policy.max_breakout_extension)
        if mode != ENTRY_ACCELERATION and checks["amount_pace_allowed"] is None:
            return stop("缺少真实累计量能或历史同分钟进度模型", data_status="snapshot_missing_amount_profile")

        elapsed = (times.iloc[-1] - times.iloc[0]).total_seconds()
        traded = float(totals["vol_hand"].iloc[-1] - totals["vol_hand"].iloc[0]) * 100
        amount_delta = float(totals["amount_yuan"].iloc[-1] - totals["amount_yuan"].iloc[0])
        checks["turnover"] = traded > 0 and amount_delta > 0
        baseline = float(minutes.tail(evaluator.confirmation_policy.volume_lookback)["volume_shares"].mean())
        ratio = traded * 60 / elapsed / baseline if elapsed > 0 and baseline > 0 else None
        checks["local_volume_ratio"] = ratio
        checks["local_volume_allowed"] = ratio is not None and ratio >= evaluator.confirmation_policy.min_volume_ratio
        sector = sector_sync(latest_time) if sector_sync else None
        checks["sector"] = sector
        from backtest.confirmation_quality import price_quality_reason
        ask = _number(recent.iloc[-1].get("ask1"))
        price_denial = price_quality_reason(ask or float(prices.iloc[-1]), vwap, level, evaluator.confirmation_policy)
        checks["price_quality"] = not bool(price_denial)
        checks["tradable"] = ask > 0 and (limit_price <= 0 or ask <= limit_price + 0.011)
        gates = ["vwap", "breakout", "turnover", "price_quality", "tradable"]
        if mode == ENTRY_WEAK:
            gates.append("previous_close")
        if mode != ENTRY_ACCELERATION:
            gates.append("amount_pace_allowed")
        if evaluator.confirmation_policy.mode == "volume_price":
            gates.append("local_volume_allowed")
        checks["sector_required"] = evaluator.confirmation_policy.require_sector
        if evaluator.confirmation_policy.require_sector and not bool(sector):
            return stop("缺少真实板块指数或成分股宽度，保持观察" if sector is None else "板块同步走强条件尚未满足",
                        data_status="snapshot_missing_sector" if sector is None else "snapshot_observing")
        failed = [field for field in gates if not checks[field]]
        if failed:
            labels = {"vwap": "连续站稳VWAP", "breakout": "连续突破关键价", "turnover": "真实增量成交",
                      "price_quality": price_denial, "tradable": "可成交卖一报价", "previous_close": "收复昨收",
                      "amount_pace_allowed": "同分钟量能进度", "local_volume_allowed": "快照局部放量"}
            return stop("等待" + "、".join(labels[field] for field in failed))
        base = EntryDecision("confirmed", MODE_LABELS[mode], breakout_level=level,
            open_gap_pct=open_gap, amount_pace=pace or 0, sector_confirmed=bool(sector),
            profile_samples=profile_samples, confirmation_policy=evaluator.confirmation_policy.mode,
            confirmation_reason="连续真实快照独立确认，不等待分钟收盘确认",
            condition_checks={"latest": {"minute": latest_time, "checks": dict(checks)},
                "confirmation": {"minute": latest_time, "checks": dict(checks)}, "source_policy": "snapshot_first"})
        result = self.evaluate(code=code, trade_date=trade_date, mode=mode, minute_bars=minutes,
            snapshots=ticks, prev_close=prev_close, open_gap=open_gap, sector_confirmed=sector,
            is_leader=is_leader, limit_price=limit_price, minute_decision=base,
            confirmation_deadline=deadline, confirmation_policy=evaluator.confirmation_policy,
            snapshot_vwap=vwap, primary=True, opening_minutes=opening_count)
        return result, ""

    def evaluate(
        self,
        *,
        code: str,
        trade_date: str,
        mode: str,
        minute_bars: pd.DataFrame,
        snapshots: pd.DataFrame,
        prev_close: float,
        open_gap: float,
        sector_confirmed: Optional[bool],
        is_leader: bool,
        limit_price: float,
        minute_decision: EntryDecision,
        confirmation_deadline: str = "",
        confirmation_policy=None,
        snapshot_vwap=None,
        primary=False,
        opening_minutes=5,
    ) -> Optional[EntryDecision]:
        ticks = self._normalize_ticks(snapshots, trade_date)
        if len(ticks) < self.consecutive_ticks:
            return None
        minutes = normalize_minute_bars(minute_bars)
        opening_end = f"09:{30 + opening_minutes}:00"
        first_five = minutes[minutes["time"].between("09:31:00", opening_end)].head(opening_minutes) if primary else minutes[minutes["time"] <= opening_end].head(opening_minutes)
        if len(first_five) < opening_minutes:
            return EntryDecision(
                "observing", MODE_LABELS.get(mode, ""), f"等待开盘前{opening_minutes}分钟结构完成",
                open_gap_pct=open_gap, data_status="minute_structure_pending",
                data_completeness=0.7,
            )
        if not first_five["ohlc_evidence"].all():
            return EntryDecision("data_insufficient", reason="开盘结构缺少真实分钟OHLC",
                                 data_status="missing_opening_ohlc", data_completeness=0.0)
        # Snapshots refine execution, never override the common minute gates.
        if minute_decision.status not in {"confirmed", "filled"}:
            with self._lock:
                self._pending.pop((str(trade_date), str(code), str(mode)), None)
            return minute_decision

        recent = ticks.tail(self.consecutive_ticks)
        recent_times = pd.to_datetime(trade_date + " " + recent["time"], format="%Y%m%d %H:%M:%S", errors="coerce")
        if recent_times.isna().any() or (recent_times.diff().dt.total_seconds().dropna() > self.max_snapshot_age_seconds).any():
            with self._lock:
                self._pending.pop((str(trade_date), str(code), str(mode)), None)
            return EntryDecision(
                "observing", MODE_LABELS.get(mode, ""), "实时快照存在采集空窗，等待连续新快照",
                open_gap_pct=open_gap, data_status="snapshot_gap", data_completeness=0.0,
            )
        if not bool(recent["quality_ok"].all()):
            return EntryDecision(
                "observing", MODE_LABELS.get(mode, ""), "3秒快照质量校验未通过",
                open_gap_pct=open_gap, data_status="snapshot_invalid",
                data_completeness=0.0,
            )
        latest = ticks.iloc[-1]
        observed_at = pd.to_datetime(latest.get("received_at"), errors="coerce")
        if pd.isna(observed_at):
            observed_at = pd.to_datetime(
                f"{trade_date} {latest.get('time')}", format="%Y%m%d %H:%M:%S",
                errors="coerce",
            )
        age_seconds = (
            (shanghai_now(self.clock()) - shanghai_now(observed_at.to_pydatetime())).total_seconds()
            if not pd.isna(observed_at) else float("inf")
        )
        if age_seconds < -1.0 or age_seconds > self.max_snapshot_age_seconds:
            return EntryDecision(
                "observing", MODE_LABELS.get(mode, ""), "3秒快照已过期，拒绝确认买点",
                open_gap_pct=open_gap, data_status="snapshot_stale",
                data_completeness=0.0,
            )
        deadline = confirmation_deadline or self.deadline
        deadline = datetime.strptime(deadline, "%H:%M:%S" if len(deadline) == 8 else "%H:%M").strftime("%H:%M:%S")
        latest_time = str(latest["time"])
        if latest_time > deadline:
            return EntryDecision(
                "cancelled", MODE_LABELS.get(mode, ""), f"{deadline[:5]}前未完成3秒快照确认",
                open_gap_pct=open_gap,
            )

        opening_high = float(first_five["high"].max())
        opening_low = float(first_five["low"].min())
        vwap = snapshot_vwap if snapshot_vwap is not None else self._current_vwap(minutes, ticks)
        prices = recent["last_price"]
        above_vwap = bool((prices >= vwap).all()) if vwap > 0 else False
        has_turnover = float(ticks.tail(5)["delta_volume"].sum()) > 0
        sector_ok = bool(sector_confirmed)
        current = float(latest["last_price"])
        from backtest.confirmation_quality import ConfirmationPolicy, price_quality_reason
        policy = confirmation_policy or ConfirmationPolicy(mode=minute_decision.confirmation_policy)
        key_level = minute_decision.breakout_level if primary or policy.mode == "volume_price" else opening_high
        denial = price_quality_reason(_number(latest.get("ask1")) or current, vwap, key_level, policy)
        if denial:
            with self._lock:
                self._pending.pop((str(trade_date), str(code), str(mode)), None)
            return replace(minute_decision, status="observing", reason=denial, entry_price=0,
                           entry_time="", data_status="snapshot_price_quality")

        if mode == ENTRY_WEAK:
            protection_ok, detail = self._protection(
                (str(trade_date), str(code), str(mode)), minutes, ticks,
                str(first_five.iloc[-1]["time"]), opening_low * 0.999)
            if not protection_ok:
                return EntryDecision(
                    "cancelled", "弱转强", f"3秒快照显示已跌破开盘前{opening_minutes}分钟低点",
                    invalidated_at=detail["first_break_at"] or latest_time, open_gap_pct=open_gap,
                )
            triggered = (
                bool((prices >= prev_close).all())
                and above_vwap
                and current > key_level
                and (sector_ok or not policy.require_sector)
                and has_turnover
            )
            reason = f"连续快照收复昨收、站稳VWAP并突破前{opening_minutes}分钟高点"
        elif mode == ENTRY_CONTINUATION:
            triggered = (
                above_vwap
                and current > key_level
                and (sector_ok or not policy.require_sector)
                and has_turnover
            )
            reason = f"连续快照站稳VWAP并突破前{opening_minutes}分钟高点"
        elif mode == ENTRY_ACCELERATION:
            triggered = (
                is_leader
                and above_vwap
                and current >= key_level
                and (sector_ok or not policy.require_sector)
                and has_turnover
            )
            reason = "龙头高开后获得连续快照成交确认"
        else:
            return None

        above_key = bool((prices >= key_level).all())
        if policy.mode == "volume_price":
            triggered = triggered and above_key

        key = (str(trade_date), str(code), str(mode))
        with self._lock:
            pending = self._pending.get(key)
            signature = (minute_decision.confirm_time, minute_decision.breakout_level, policy.mode)
            if pending and pending.get("confirmation_signature") != signature:
                self._pending.pop(key, None)
                pending = None
            if pending:
                between = ticks[ticks["time"] >= str(pending.get("time") or "")]
                timeline = pd.to_datetime(trade_date + " " + between["time"], format="%Y%m%d %H:%M:%S", errors="coerce")
                if timeline.empty or str(between.iloc[0]["time"]) != str(pending.get("time")) or (
                    timeline.diff().dt.total_seconds().dropna() > self.max_snapshot_age_seconds
                ).any():
                    self._pending.pop(key, None)
                    pending = None
            if not triggered:
                self._pending.pop(key, None)
                missing = []
                if not above_vwap:
                    missing.append("连续站稳VWAP")
                if current <= key_level or (policy.mode == "volume_price" and not above_key):
                    missing.append("站稳确认关键价" if policy.mode == "volume_price" else f"突破前{opening_minutes}分钟高点")
                if policy.require_sector and not sector_ok:
                    missing.append("板块同步走强")
                if not has_turnover:
                    missing.append("近15秒真实成交")
                return EntryDecision(
                    "observing", MODE_LABELS.get(mode, ""),
                    "等待" + "、".join(missing or ["3秒快照连续确认"]),
                    open_gap_pct=open_gap, data_status="snapshot_observing",
                    data_completeness=0.95, condition_checks=minute_decision.condition_checks,
                )
            if not pending:
                self._pending[key] = {"time": latest_time, "price": current, "confirmation_signature": signature}
                return replace(
                    minute_decision, status="confirmed", signal=MODE_LABELS.get(mode, ""),
                    reason=f"{reason}，等待下一快照模拟成交", confirm_time=latest_time,
                    entry_time="", entry_price=0,
                    open_gap_pct=open_gap, sector_confirmed=sector_ok,
                    data_status="snapshot_triggered", data_completeness=1.0,
                )
            if latest_time <= str(pending.get("time") or ""):
                return replace(
                    minute_decision, status="confirmed", signal=MODE_LABELS.get(mode, ""),
                    reason=f"{reason}，等待下一快照模拟成交", confirm_time=str(pending.get("time") or ""),
                    entry_time="", entry_price=0,
                    open_gap_pct=open_gap, sector_confirmed=sector_ok,
                    data_status="snapshot_triggered", data_completeness=1.0,
                )
            self._pending.pop(key, None)

        ask1 = _number(latest.get("ask1"))
        delta_volume = _number(latest.get("delta_volume"))
        locked = limit_price > 0 and current >= limit_price * 0.998 and ask1 <= 0
        if locked or delta_volume <= 0:
            return replace(
                minute_decision, status="signal_unfilled", signal=MODE_LABELS.get(mode, ""),
                reason=f"{reason}，但下一快照无可成交证明", confirm_time=str(pending.get("time") or ""),
                entry_time=latest_time, entry_price=0.0, open_gap_pct=open_gap, sector_confirmed=sector_ok,
                data_status="snapshot_unfilled", data_completeness=1.0,
            )
        return replace(
            minute_decision, status="filled", signal=MODE_LABELS.get(mode, ""), reason=reason,
            confirm_time=str(pending.get("time") or ""), entry_time=latest_time, entry_price=ask1 or current,
            open_gap_pct=open_gap, sector_confirmed=sector_ok,
            data_status="snapshot_filled", data_completeness=1.0,
        )

    def _protection(self, key, minutes, ticks, boundary, protection):
        """Remember observed post-structure lows beyond the rolling hot window."""
        bars = minutes[(minutes["time"] > boundary) & minutes["ohlc_evidence"]]
        points = ticks[ticks["time"] > boundary]
        observations = list(zip(bars["low"].astype(float), bars["time"].astype(str)))
        observations.extend(zip(points["last_price"].astype(float), points["time"].astype(str)))
        state_key = (*key, boundary)
        storage_key = "realtime:protection:v2:" + ":".join(state_key)
        with self._lock:
            self._protection_lows = {k: v for k, v in self._protection_lows.items() if k[0] == key[0]}
            prior = self._protection_lows.get(state_key)
            if prior is None and self.state_backend is not None:
                prior = self.state_backend.get_json(storage_key)
            if prior is not None:
                observations.append((prior["low"], prior.get("low_at") or boundary))
            low, low_at = min(observations) if observations else (None, "")
            breaks = [at for price, at in observations if price < protection]
            if prior and prior.get("protection_price") == protection and prior.get("first_break_at"):
                breaks.append(prior["first_break_at"])
            first_break = min(breaks) if breaks else ""
            if low is not None:
                state = {"low": low, "low_at": low_at, "first_break_at": first_break, "protection_price": protection}
                self._protection_lows[state_key] = state
                if self.state_backend is not None and state != prior:
                    self.state_backend.set_json(storage_key, state, 172800)
        return low is None or low >= protection, {
            "protection_price": protection, "protection_window_start": boundary,
            "post_structure_low": low, "protection_evidence": "observed_post_structure_prices",
            "session_low_used": False, "first_break_at": first_break,
        }

    @staticmethod
    def _normalize_ticks(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        data = frame.copy()
        required = ("last_price", "delta_volume", "ask1")
        for column in required:
            values = data[column] if column in data else pd.Series(0.0, index=data.index)
            data[column] = pd.to_numeric(values, errors="coerce").fillna(0.0)
        if "time" not in data.columns:
            return pd.DataFrame()
        if "date" not in data.columns:
            return pd.DataFrame()
        data["date"] = data["date"].astype(str).str.replace("-", "", regex=False).str[:8]
        data = data[data["date"] == str(trade_date)]
        if data.empty:
            return data
        if "quality_ok" not in data.columns:
            data["quality_ok"] = True
        data["quality_ok"] = data["quality_ok"].fillna(False).astype(bool)
        if "received_at" not in data.columns:
            data["received_at"] = ""
        data["time"] = data["time"].astype(str).str[-8:]
        session = data["time"].between("09:30:00", "11:30:00") | data["time"].between("13:00:00", "15:00:00")
        return data[(data["last_price"] > 0) & session].sort_values("time").drop_duplicates("time", keep="last")

    @staticmethod
    def _current_vwap(minutes: pd.DataFrame, ticks: pd.DataFrame) -> float:
        if not minutes.empty and _number(minutes.iloc[-1].get("vwap")) > 0:
            return _number(minutes.iloc[-1].get("vwap"))
        volume = float(ticks["delta_volume"].sum())
        if volume <= 0:
            return 0.0
        return float((ticks["last_price"] * ticks["delta_volume"]).sum() / volume)


__all__ = ["SnapshotSignalService"]
