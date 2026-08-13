"""Confirm minute entry structures with a short pytdx snapshot sequence."""
from __future__ import annotations

from threading import RLock
from typing import Any, Dict, Optional, Tuple

import pandas as pd

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

    def __init__(self, *, deadline: str = "10:00:00", consecutive_ticks: int = 2) -> None:
        self.deadline = deadline
        self.consecutive_ticks = max(int(consecutive_ticks), 2)
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
    ) -> Optional[EntryDecision]:
        ticks = self._normalize_ticks(snapshots)
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
        if minute_decision.status in {"rejected", "cancelled"}:
            return minute_decision

        latest = ticks.iloc[-1]
        latest_time = str(latest["time"])
        if latest_time > self.deadline:
            return EntryDecision(
                "cancelled", MODE_LABELS.get(mode, ""), "10:00前未完成3秒快照确认",
                open_gap_pct=open_gap,
            )

        recent = ticks.tail(self.consecutive_ticks)
        opening_high = float(first_five["high"].max())
        opening_low = float(first_five["low"].min())
        vwap = self._current_vwap(minutes, ticks)
        prices = recent["last_price"]
        above_vwap = bool((prices >= vwap).all()) if vwap > 0 else False
        has_turnover = float(ticks.tail(5)["delta_volume"].sum()) > 0
        sector_ok = bool(sector_confirmed)
        current = float(latest["last_price"])

        if mode == ENTRY_WEAK:
            if float(ticks["last_price"].min()) < opening_low * 0.999:
                return EntryDecision(
                    "cancelled", "弱转强", "3秒快照显示已跌破开盘前5分钟低点",
                    latest_time, open_gap_pct=open_gap,
                )
            triggered = (
                bool((prices >= prev_close).all())
                and above_vwap
                and current > opening_high
                and sector_ok
                and has_turnover
            )
            reason = "连续快照收复昨收、站稳VWAP并突破前5分钟高点"
        elif mode == ENTRY_CONTINUATION:
            triggered = (
                above_vwap
                and current > opening_high
                and sector_ok
                and has_turnover
            )
            reason = "连续快照站稳VWAP并突破前5分钟高点"
        elif mode == ENTRY_ACCELERATION:
            triggered = (
                is_leader
                and above_vwap
                and current >= opening_high
                and sector_ok
                and has_turnover
            )
            reason = "龙头高开后获得连续快照成交确认"
        else:
            return None

        key = (str(trade_date), str(code), str(mode))
        with self._lock:
            pending = self._pending.get(key)
            if not triggered:
                self._pending.pop(key, None)
                missing = []
                if not above_vwap:
                    missing.append("连续站稳VWAP")
                if current <= opening_high:
                    missing.append("突破前5分钟高点")
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
                self._pending[key] = {"time": latest_time, "price": current}
                return EntryDecision(
                    "observing", MODE_LABELS.get(mode, ""),
                    f"{reason}，等待下一快照模拟成交", latest_time,
                    open_gap_pct=open_gap, sector_confirmed=sector_ok,
                    data_status="snapshot_triggered", data_completeness=1.0,
                )
            if latest_time <= str(pending.get("time") or ""):
                return EntryDecision(
                    "observing", MODE_LABELS.get(mode, ""),
                    f"{reason}，等待下一快照模拟成交", str(pending.get("time") or ""),
                    open_gap_pct=open_gap, sector_confirmed=sector_ok,
                    data_status="snapshot_triggered", data_completeness=1.0,
                )
            self._pending.pop(key, None)

        ask1 = _number(latest.get("ask1"))
        delta_volume = _number(latest.get("delta_volume"))
        locked = limit_price > 0 and current >= limit_price * 0.998 and ask1 <= 0
        if locked or delta_volume <= 0:
            return EntryDecision(
                "signal_unfilled", MODE_LABELS.get(mode, ""),
                f"{reason}，但下一快照无可成交证明", str(pending.get("time") or ""),
                latest_time, 0.0, open_gap, sector_confirmed=sector_ok,
                data_status="snapshot_unfilled", data_completeness=1.0,
            )
        return EntryDecision(
            "filled", MODE_LABELS.get(mode, ""), reason,
            str(pending.get("time") or ""), latest_time, ask1 or current,
            open_gap, sector_confirmed=sector_ok,
            data_status="snapshot_filled", data_completeness=1.0,
        )

    @staticmethod
    def _normalize_ticks(frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        data = frame.copy()
        required = ("last_price", "delta_volume", "ask1")
        for column in required:
            data[column] = pd.to_numeric(data.get(column, 0.0), errors="coerce").fillna(0.0)
        if "time" not in data.columns:
            return pd.DataFrame()
        data["time"] = data["time"].astype(str).str[-8:]
        return data[(data["last_price"] > 0) & data["time"].between("09:30:00", "10:00:00")].sort_values("time").drop_duplicates("time", keep="last")

    @staticmethod
    def _current_vwap(minutes: pd.DataFrame, ticks: pd.DataFrame) -> float:
        if not minutes.empty and _number(minutes.iloc[-1].get("vwap")) > 0:
            return _number(minutes.iloc[-1].get("vwap"))
        volume = float(ticks["delta_volume"].sum())
        if volume <= 0:
            return 0.0
        return float((ticks["last_price"] * ticks["delta_volume"]).sum() / volume)


__all__ = ["SnapshotSignalService"]
