"""Point-in-time minute-bar entry rules for short-term backtests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import pandas as pd


ENTRY_FIXED = "fixed_gap"
ENTRY_WEAK = "weak_only"
ENTRY_CONTINUATION = "continuation_only"
ENTRY_ACCELERATION = "acceleration_only"
ENTRY_HYBRID = "hybrid"
ENTRY_COMPARE = "compare"
ENTRY_MODES = {
    ENTRY_FIXED,
    ENTRY_WEAK,
    ENTRY_CONTINUATION,
    ENTRY_ACCELERATION,
    ENTRY_HYBRID,
    ENTRY_COMPARE,
}


@dataclass(frozen=True)
class EntryDecision:
    status: str
    signal: str = ""
    reason: str = ""
    confirm_time: str = ""
    entry_time: str = ""
    entry_price: float = 0.0
    open_gap_pct: float = 0.0
    amount_pace: float = 0.0
    sector_confirmed: bool = False

    @property
    def filled(self) -> bool:
        return self.status == "filled" and self.entry_price > 0


def normalize_minute_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize point or OHLC minute data and calculate point-in-time VWAP."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "time" not in data.columns and "datetime" in data.columns:
        data["time"] = pd.to_datetime(data["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
    if "time" not in data.columns:
        return pd.DataFrame()
    def normalize_time(value: Any) -> str:
        text = str(value or "").strip().split(" ")[-1]
        if len(text) == 5 and text[2] == ":":
            return text + ":00"
        if len(text) >= 8 and text[-6] == ":" and text[-3] == ":":
            return text[-8:]
        parsed = pd.to_datetime(text, errors="coerce")
        return parsed.strftime("%H:%M:%S") if pd.notna(parsed) else ""

    data["time"] = data["time"].map(normalize_time)
    for column in ("open", "high", "low", "close"):
        if column not in data.columns:
            data[column] = data.get("price", 0.0)
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["volume"] = pd.to_numeric(
        data["volume"] if "volume" in data.columns else data.get("vol", 0.0),
        errors="coerce",
    ).fillna(0.0)
    if "amount" in data.columns:
        data["amount"] = pd.to_numeric(data["amount"], errors="coerce").fillna(0.0)
    else:
        data["amount"] = 0.0
    estimated_amount = data["close"].fillna(0.0) * data["volume"]
    data.loc[data["amount"] <= 0, "amount"] = estimated_amount[data["amount"] <= 0]
    data = data[
        data["time"].between("09:30:00", "10:01:59")
        & (data["close"] > 0)
    ].sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
    if data.empty:
        return data
    weighted = data["close"] * data["volume"]
    cum_volume = data["volume"].cumsum()
    calculated_vwap = weighted.cumsum() / cum_volume.replace(0, pd.NA)
    source_avg = pd.to_numeric(data.get("avg_price"), errors="coerce") if "avg_price" in data.columns else None
    data["vwap"] = source_avg.fillna(calculated_vwap) if source_avg is not None else calculated_vwap
    data["vwap"] = data["vwap"].fillna(data["close"])
    data["cum_amount"] = data["amount"].cumsum()
    return data


class MinuteEntryEvaluator:
    """Evaluate weak-to-strong, continuation and high-gap acceleration entries."""

    def __init__(
        self,
        *,
        deadline: str = "10:00:00",
        weak_min_gap: float = -0.03,
        weak_max_gap: float = 0.01,
        continuation_max_gap: float = 0.05,
        min_amount_pace: float = 0.80,
        max_amount_pace: float = 3.0,
        min_auction_volume_ratio: float = 0.008,
        min_auction_amount: float = 5_000_000,
    ) -> None:
        self.deadline = deadline
        self.weak_min_gap = weak_min_gap
        self.weak_max_gap = weak_max_gap
        self.continuation_max_gap = continuation_max_gap
        self.min_amount_pace = min_amount_pace
        self.max_amount_pace = max_amount_pace
        self.min_auction_volume_ratio = min_auction_volume_ratio
        self.min_auction_amount = min_auction_amount

    def evaluate(
        self,
        *,
        mode: str,
        bars: pd.DataFrame,
        open_gap: float,
        prev_close: float,
        previous_amount: float = 0.0,
        previous_volume: float = 0.0,
        auction_amount: float = 0.0,
        auction_volume: float = 0.0,
        plan_amount_ratio: float = 0.0,
        limit_price: float = 0.0,
        is_leader: bool = False,
        sector_sync: Optional[Callable[[str], bool]] = None,
        live: bool = False,
    ) -> EntryDecision:
        data = normalize_minute_bars(bars)
        if data.empty or len(data) < 2:
            status = "observing" if live else "missing_minutes"
            return EntryDecision(status, reason="等待当日一分钟行情", open_gap_pct=open_gap)
        opening_rows = data[data["time"] <= "09:35:00"]
        first_five = opening_rows.head(5)
        last_opening_index = int(first_five.index.max()) if not first_five.empty else -1
        scan = data[(data.index > last_opening_index) & (data["time"] <= self.deadline)]
        if first_five.empty or scan.empty:
            status = "observing" if live and str(data.iloc[-1]["time"]) <= self.deadline else "missing_minutes"
            return EntryDecision(status, reason="等待开盘前5分钟完成", open_gap_pct=open_gap)

        if mode == ENTRY_WEAK:
            if not self.weak_min_gap <= open_gap <= self.weak_max_gap:
                return EntryDecision("rejected", signal="弱转强", reason="开盘不在弱转强区间", open_gap_pct=open_gap)
            return self._weak_to_strong(
                data, first_five, scan, open_gap, prev_close, previous_amount,
                plan_amount_ratio, limit_price, sector_sync, live,
            )
        if mode == ENTRY_CONTINUATION:
            if not self.weak_max_gap < open_gap <= self.continuation_max_gap:
                return EntryDecision("rejected", signal="强势延续", reason="开盘不在强势延续区间", open_gap_pct=open_gap)
            return self._continuation(
                data, first_five, scan, open_gap, previous_amount, previous_volume,
                auction_amount, auction_volume, plan_amount_ratio, limit_price, sector_sync, live,
            )
        if mode == ENTRY_ACCELERATION:
            if open_gap <= self.continuation_max_gap:
                return EntryDecision(
                    "rejected", signal="高开加速", reason="开盘未达到高开加速区间",
                    open_gap_pct=open_gap,
                )
            return self._acceleration(
                data, first_five, scan, open_gap, limit_price, is_leader, sector_sync, live,
            )
        if mode == ENTRY_HYBRID:
            if self.weak_min_gap <= open_gap <= self.weak_max_gap:
                return self._weak_to_strong(
                    data, first_five, scan, open_gap, prev_close, previous_amount,
                    plan_amount_ratio, limit_price, sector_sync, live,
                )
            if self.weak_max_gap < open_gap <= self.continuation_max_gap:
                return self._continuation(
                    data, first_five, scan, open_gap, previous_amount, previous_volume,
                    auction_amount, auction_volume, plan_amount_ratio, limit_price, sector_sync, live,
                )
            if open_gap > self.continuation_max_gap:
                return EntryDecision(
                    "rejected", reason="高开超过5%，仅高开加速模式参与",
                    open_gap_pct=open_gap,
                )
            return EntryDecision("rejected", reason="低开超过3%，取消", open_gap_pct=open_gap)
        return EntryDecision("rejected", reason=f"未知分钟入场模式: {mode}", open_gap_pct=open_gap)

    def _weak_to_strong(
        self, data, first_five, scan, gap, prev_close, previous_amount,
        plan_amount_ratio, limit_price, sector_sync, live,
    ) -> EntryDecision:
        opening_low = float(first_five["low"].min())
        opening_high = float(first_five["high"].max())
        for index, row in scan.iterrows():
            if float(row["low"]) < opening_low * 0.999:
                return EntryDecision("cancelled", "弱转强", "跌破开盘前5分钟低点", str(row["time"]), open_gap_pct=gap)
            pace = self._amount_pace(row, previous_amount, plan_amount_ratio)
            sector_ok = bool(sector_sync(str(row["time"]))) if sector_sync else True
            confirmed = (
                float(row["close"]) >= prev_close
                and float(row["close"]) >= float(row["vwap"])
                and float(row["high"]) > opening_high
                and sector_ok
                and self.min_amount_pace <= pace <= self.max_amount_pace
            )
            if confirmed:
                return self._next_minute_fill(
                    data, index, "弱转强", "收复昨收、站上VWAP并突破前5分钟高点",
                    gap, pace, sector_ok, limit_price, live=live,
                )
        if live and str(data.iloc[-1]["time"]) <= self.deadline:
            return EntryDecision("observing", "弱转强", "弱转强条件尚未全部满足", open_gap_pct=gap)
        return EntryDecision("cancelled", "弱转强", "10:00前未完成弱转强确认", open_gap_pct=gap)

    def _continuation(
        self, data, first_five, scan, gap, previous_amount, previous_volume,
        auction_amount, auction_volume, plan_amount_ratio, limit_price, sector_sync, live,
    ) -> EntryDecision:
        auction_ratio = auction_volume / previous_volume if previous_volume > 0 else 0.0
        auction_ok = (
            auction_amount >= self.min_auction_amount
            and auction_ratio >= self.min_auction_volume_ratio
        )
        if not auction_ok:
            status = "observing" if live else "cancelled"
            return EntryDecision(status, "强势延续", "竞价成交额或竞价量比不足", open_gap_pct=gap)
        opening_high = float(first_five["high"].max())
        touched_vwap = False
        for index, row in scan.iterrows():
            vwap = float(row["vwap"])
            touched_vwap = touched_vwap or float(row["low"]) <= vwap * 1.002
            sector_ok = bool(sector_sync(str(row["time"]))) if sector_sync else True
            pace = self._amount_pace(row, previous_amount, plan_amount_ratio)
            trigger = (touched_vwap and float(row["close"]) >= vwap) or float(row["high"]) > opening_high
            if trigger and sector_ok and self.min_amount_pace <= pace <= self.max_amount_pace:
                return self._next_minute_fill(
                    data, index, "强势延续", "竞价放量后回踩VWAP承接或突破前5分钟高点",
                    gap, pace, sector_ok, limit_price, live=live,
                )
        if live and str(data.iloc[-1]["time"]) <= self.deadline:
            return EntryDecision("observing", "强势延续", "强势延续条件尚未全部满足", open_gap_pct=gap)
        return EntryDecision("cancelled", "强势延续", "10:00前未出现有效承接或突破", open_gap_pct=gap)

    def _acceleration(
        self, data, first_five, scan, gap, limit_price, is_leader, sector_sync, live,
    ) -> EntryDecision:
        if not is_leader:
            return EntryDecision("rejected", "高开加速", "非龙头或主线核心，不参与高开加速", open_gap_pct=gap)
        opened_locked = limit_price > 0 and float(first_five.iloc[0]["open"]) >= limit_price * 0.998
        if opened_locked:
            tradable = data[(data["low"] < limit_price * 0.998) & (data["volume"] > 0)]
            if tradable.empty:
                return EntryDecision("signal_unfilled", "高开加速", "接近涨停开盘，暂无可成交证据", "09:30:00", open_gap_pct=gap)
        opening_high = float(first_five["high"].max())
        for index, row in scan.iterrows():
            sector_ok = bool(sector_sync(str(row["time"]))) if sector_sync else True
            if sector_ok and (
                float(row["high"]) > opening_high
                or (limit_price > 0 and float(row["high"]) >= limit_price * 0.998)
            ):
                return self._next_minute_fill(
                    data, index, "高开加速", "龙头高开后继续突破", gap, 0.0,
                    sector_ok, limit_price, unfilled_when_locked=True, live=live,
                )
        if live and str(data.iloc[-1]["time"]) <= self.deadline:
            return EntryDecision("observing", "高开加速", "高开加速条件尚未全部满足", open_gap_pct=gap)
        return EntryDecision("cancelled", "高开加速", "10:00前未出现龙头加速确认", open_gap_pct=gap)

    def _next_minute_fill(
        self, data, index, signal, reason, gap, pace, sector_ok, limit_price,
        unfilled_when_locked: bool = False,
        live: bool = False,
    ) -> EntryDecision:
        following = data[data.index > index]
        if following.empty:
            if live:
                return EntryDecision(
                    "confirmed", signal, f"{reason}，等待下一分钟成交确认",
                    str(data.loc[index, "time"]), open_gap_pct=gap,
                    amount_pace=pace, sector_confirmed=sector_ok,
                )
            return EntryDecision("signal_unfilled", signal, f"{reason}，但缺少下一分钟成交", str(data.loc[index, "time"]), open_gap_pct=gap)
        next_row = following.iloc[0]
        price = float(next_row.get("open") or next_row.get("close") or 0.0)
        volume = float(next_row.get("volume") or 0.0)
        locked = limit_price > 0 and price >= limit_price * 0.998
        if price <= 0 or volume <= 0 or (unfilled_when_locked and locked):
            return EntryDecision(
                "signal_unfilled", signal, f"{reason}，下一分钟无可成交量或仍封涨停",
                str(data.loc[index, "time"]), str(next_row.get("time") or ""),
                0.0, gap, pace, sector_ok,
            )
        return EntryDecision(
            "filled", signal, reason, str(data.loc[index, "time"]),
            str(next_row.get("time") or ""), price, gap, pace, sector_ok,
        )

    @staticmethod
    def _amount_pace(row: pd.Series, previous_amount: float, fallback: float) -> float:
        if previous_amount > 0:
            hh, mm, *_ = [int(part) for part in str(row["time"]).split(":")]
            elapsed = max((hh * 60 + mm) - (9 * 60 + 30) + 1, 1)
            # A股成交量明显呈开盘/收盘集中的 U 型，不能用全天 240 分钟线性外推。
            # 09:30 首分钟约按全天 8%，10:00 附近累计约按 25% 作为基准。
            if elapsed <= 30:
                expected_fraction = 0.08 + 0.17 * (elapsed / 30.0)
            elif elapsed <= 120:
                expected_fraction = 0.25 + 0.30 * ((elapsed - 30) / 90.0)
            else:
                expected_fraction = 0.55 + 0.45 * ((elapsed - 120) / 120.0)
            expected = previous_amount * min(expected_fraction, 1.0)
            if expected > 0:
                return float(row["cum_amount"]) / expected
        return float(fallback or 0.0)


__all__ = [
    "ENTRY_ACCELERATION", "ENTRY_COMPARE", "ENTRY_CONTINUATION", "ENTRY_FIXED",
    "ENTRY_HYBRID", "ENTRY_MODES", "ENTRY_WEAK", "EntryDecision",
    "MinuteEntryEvaluator", "normalize_minute_bars",
]
