"""Confirm minute entry structures with a short normalized eltdx snapshot sequence."""
from __future__ import annotations

from datetime import datetime
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
    """Use minute structure as a gate and 3-second snapshots as the trigger."""

    def __init__(
        self,
        *,
        deadline: str = "10:00:00",
        consecutive_ticks: int = 2,
        max_snapshot_age_seconds: float = 8.0,
        clock: Any = None,
    ) -> None:
        self.deadline = deadline
        self.consecutive_ticks = max(int(consecutive_ticks), 2)
        self.max_snapshot_age_seconds = max(float(max_snapshot_age_seconds), 3.0)
        self.clock = clock or shanghai_now
        self._pending: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._lock = RLock()

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
    ) -> Optional[EntryDecision]:
        ticks = self._normalize_ticks(snapshots, trade_date)
        if len(ticks) < self.consecutive_ticks:
            return None
        minutes = normalize_minute_bars(minute_bars)
        first_five = minutes[minutes["time"] <= "09:35:00"].head(5)
        if len(first_five) < 5:
            return EntryDecision(
                "observing", MODE_LABELS.get(mode, ""), "等待开盘前5分钟结构完成",
                open_gap_pct=open_gap, data_status="minute_structure_pending",
                data_completeness=0.7,
            )
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
        vwap = self._current_vwap(minutes, ticks)
        prices = recent["last_price"]
        above_vwap = bool((prices >= vwap).all()) if vwap > 0 else False
        has_turnover = float(ticks.tail(5)["delta_volume"].sum()) > 0
        sector_ok = bool(sector_confirmed)
        current = float(latest["last_price"])
        from backtest.confirmation_quality import ConfirmationPolicy, price_quality_reason
        policy = confirmation_policy or ConfirmationPolicy(mode=minute_decision.confirmation_policy)
        key_level = minute_decision.breakout_level if policy.mode == "volume_price" else opening_high
        denial = price_quality_reason(_number(latest.get("ask1")) or current, vwap, key_level, policy)
        if denial:
            with self._lock:
                self._pending.pop((str(trade_date), str(code), str(mode)), None)
            return replace(minute_decision, status="observing", reason=denial, entry_price=0,
                           entry_time="", data_status="snapshot_price_quality")

        if mode == ENTRY_WEAK:
            after_opening = ticks[ticks["time"] > str(first_five.iloc[-1]["time"])]
            if not after_opening.empty and float(after_opening["last_price"].min()) < opening_low * 0.999:
                return EntryDecision(
                    "cancelled", "弱转强", "3秒快照显示已跌破开盘前5分钟低点",
                    latest_time, open_gap_pct=open_gap,
                )
            triggered = (
                bool((prices >= prev_close).all())
                and above_vwap
                and current > key_level
                and sector_ok
                and has_turnover
            )
            reason = "连续快照收复昨收、站稳VWAP并突破前5分钟高点"
        elif mode == ENTRY_CONTINUATION:
            triggered = (
                above_vwap
                and current > key_level
                and sector_ok
                and has_turnover
            )
            reason = "连续快照站稳VWAP并突破前5分钟高点"
        elif mode == ENTRY_ACCELERATION:
            triggered = (
                is_leader
                and above_vwap
                and current >= key_level
                and sector_ok
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
                    missing.append("站稳确认关键价" if policy.mode == "volume_price" else "突破前5分钟高点")
                if not sector_ok:
                    missing.append("板块同步走强")
                if not has_turnover:
                    missing.append("近15秒真实成交")
                return EntryDecision(
                    "observing", MODE_LABELS.get(mode, ""),
                    "等待" + "、".join(missing or ["3秒快照连续确认"]),
                    open_gap_pct=open_gap, data_status="snapshot_observing",
                    data_completeness=0.95,
                )
            if not pending:
                self._pending[key] = {"time": latest_time, "price": current, "confirmation_signature": signature}
                return replace(
                    minute_decision, status="observing", signal=MODE_LABELS.get(mode, ""),
                    reason=f"{reason}，等待下一快照模拟成交", confirm_time=latest_time,
                    entry_time="", entry_price=0,
                    open_gap_pct=open_gap, sector_confirmed=sector_ok,
                    data_status="snapshot_triggered", data_completeness=1.0,
                )
            if latest_time <= str(pending.get("time") or ""):
                return replace(
                    minute_decision, status="observing", signal=MODE_LABELS.get(mode, ""),
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
