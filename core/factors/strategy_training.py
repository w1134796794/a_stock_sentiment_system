"""Strategy-specific training samples built from auditable minute fills."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import pandas as pd

from backtest.minute_entry import (
    ENTRY_ACCELERATION,
    ENTRY_CONTINUATION,
    ENTRY_WEAK,
    MinuteEntryEvaluator,
    normalize_minute_bars,
)
from core.signals.minute_amount_profile import MinuteAmountProfileRepository


@dataclass(frozen=True)
class StrategyTrainingSpec:
    horizon_days: int
    max_horizon_days: int


STRATEGY_TRAINING_SPECS: Dict[str, StrategyTrainingSpec] = {
    "mainline_leader": StrategyTrainingSpec(3, 5),
    "ultra_short_board": StrategyTrainingSpec(2, 2),
    "first_board_launch": StrategyTrainingSpec(2, 2),
    "capital_resonance": StrategyTrainingSpec(3, 3),
    "momentum_repair": StrategyTrainingSpec(3, 3),
    "weak_to_strong": StrategyTrainingSpec(3, 3),
    "trend_follow": StrategyTrainingSpec(5, 10),
}
MODE_MAP = {
    "weak_to_strong": ENTRY_WEAK,
    "continuation": ENTRY_CONTINUATION,
    "acceleration": ENTRY_ACCELERATION,
}


def strategy_training_spec(strategy_id: str, default_horizon: int = 3) -> StrategyTrainingSpec:
    return STRATEGY_TRAINING_SPECS.get(
        str(strategy_id), StrategyTrainingSpec(max(int(default_horizon), 1), max(int(default_horizon), 1)),
    )


class StrategyMinuteTrainingBuilder:
    """Filter a factor frame to strategy candidates with genuine minute fills."""

    def __init__(
        self,
        *,
        duckdb_path: Path,
        screening_dir: Optional[Path] = None,
        tick_dir: Optional[Path] = None,
        auction_dir: Optional[Path] = None,
    ) -> None:
        from config.settings import CACHE_DIR, WEB_DATA_DIR

        self.duckdb_path = Path(duckdb_path)
        self.screening_dir = Path(screening_dir or Path(WEB_DATA_DIR) / "screening" / "combinations")
        self.tick_dir = Path(tick_dir or Path(CACHE_DIR) / "stock" / "tick")
        self.auction_dir = Path(auction_dir or Path(CACHE_DIR) / "stock" / "auction")
        self.amount_profiles = MinuteAmountProfileRepository()
        self.audit: Dict[str, Any] = {}

    def apply(
        self,
        frame: pd.DataFrame,
        *,
        strategy_id: str,
        horizon_days: int,
        allowed_entry_modes: Iterable[str],
    ) -> pd.DataFrame:
        candidates = self._candidate_map(strategy_id)
        if frame.empty or not candidates:
            self.audit = {"candidate_rows": len(candidates), "filled_rows": 0, "excluded": {"missing_candidates": len(frame)}}
            return frame.iloc[0:0].copy()
        selected = frame[
            frame.apply(lambda row: (str(row["trade_date"]), str(row["code"]).zfill(6)) in candidates, axis=1)
        ].copy()
        exclusions: Counter[str] = Counter()
        output = []
        for _, row in selected.iterrows():
            key = (str(row["trade_date"]), str(row["code"]).zfill(6))
            candidate = candidates[key]
            labelled, reason = self._label_row(
                row, candidate, strategy_id=strategy_id, horizon_days=max(int(horizon_days), 1),
                allowed_entry_modes=list(allowed_entry_modes),
            )
            if labelled is None:
                exclusions[reason or "not_filled"] += 1
            else:
                output.append(labelled)
        self.audit = {
            "strategy_id": strategy_id,
            "candidate_rows": len(selected),
            "filled_rows": len(output),
            "excluded": dict(exclusions),
            "label_source": "next_minute_open_after_confirmed_signal",
            "horizon_days": int(horizon_days),
        }
        return pd.DataFrame(output).reset_index(drop=True) if output else selected.iloc[0:0].copy()

    def _candidate_map(self, strategy_id: str) -> Dict[tuple[str, str], Dict[str, Any]]:
        directory = self.screening_dir / str(strategy_id)
        rows: Dict[tuple[str, str], Dict[str, Any]] = {}
        for path in sorted(directory.glob("screening_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            date = str(payload.get("trade_date") or path.stem[-8:])
            for row in payload.get("final") or []:
                if not isinstance(row, Mapping):
                    continue
                code = str(row.get("code") or "").split(".", 1)[0].zfill(6)
                if code:
                    rows[(date, code)] = dict(row)
        return rows

    def _label_row(
        self,
        row: pd.Series,
        candidate: Mapping[str, Any],
        *,
        strategy_id: str,
        horizon_days: int,
        allowed_entry_modes: list[str],
    ) -> tuple[Optional[Dict[str, Any]], str]:
        entry_date = str(row.get("entry_date") or "")
        code = str(row.get("code") or "").zfill(6)
        ts_code = self._ts_code(code)
        bars = self._load_tick(ts_code, entry_date)
        if bars.empty:
            return None, "missing_minutes"
        daily = self._daily_window(code, str(row.get("trade_date") or ""), entry_date, horizon_days)
        if daily.empty or len(daily[daily["trade_date"] >= entry_date]) < horizon_days:
            return None, "missing_forward_daily"
        previous = daily[daily["trade_date"] < entry_date].tail(1)
        if previous.empty:
            return None, "missing_previous_daily"
        prev = previous.iloc[0]
        minute = normalize_minute_bars(bars)
        if minute.empty:
            return None, "invalid_minutes"
        first_open = float(minute.iloc[0]["open"] or 0.0)
        prev_close = float(prev.get("close") or 0.0)
        if first_open <= 0 or prev_close <= 0:
            return None, "missing_open"
        open_gap = first_open / prev_close - 1.0
        mode = self._entry_mode(open_gap, allowed_entry_modes)
        if not mode:
            return None, "gap_not_supported"
        sector_sync = self._sector_sync_callback(
            str(row.get("trade_date") or ""), entry_date, code,
            str(candidate.get("resonance_sectors") or row.get("primary_sector") or ""),
        )
        if sector_sync is None:
            return None, "missing_sector_minutes"
        auction = self._load_auction(ts_code, entry_date)
        if mode == ENTRY_CONTINUATION and not auction:
            return None, "missing_auction"
        context = candidate.get("context") or {}
        limit_pct = float(context.get("limit_pct") or row.get("limit_pct") or 10.0)
        evaluator = MinuteEntryEvaluator()
        decision = evaluator.evaluate(
            mode=mode,
            bars=minute,
            open_gap=open_gap,
            prev_close=prev_close,
            previous_amount=float(prev.get("amount_yuan") or 0.0),
            previous_volume=float(prev.get("vol_hand") or 0.0),
            auction_amount=float(auction.get("amount") or auction.get("成交额") or 0.0),
            auction_volume=float(auction.get("volume") or auction.get("成交量") or 0.0),
            plan_amount_ratio=float(context.get("amount_ratio") or 0.0),
            limit_price=prev_close * (1.0 + limit_pct / 100.0),
            is_leader=strategy_id_is_leader(candidate, allowed_entry_modes),
            sector_sync=sector_sync,
            expected_amount_fraction=lambda time_text: self._expected_amount_fraction(
                float(prev.get("amount_yuan") or 0.0), time_text,
            ),
        )
        if not decision.filled:
            return None, decision.status or "not_filled"

        future = daily[daily["trade_date"] >= entry_date].head(horizon_days)
        entry_price = float(decision.entry_price)
        raw_return = float(future.iloc[-1]["close"]) / entry_price - 1.0
        mfe = float(future["high"].max()) / entry_price - 1.0
        mae = float(future["low"].min()) / entry_price - 1.0
        old_benchmark = float(row.get("raw_forward_return") or 0.0) - float(row.get("next_3d_excess_return") or 0.0)
        result = row.to_dict()
        result.update({
            "entry_open": entry_price,
            "entry_time": decision.entry_time,
            "entry_signal": decision.signal,
            "raw_forward_return": raw_return,
            "next_3d_excess_return": raw_return - old_benchmark,
            "target_return": raw_return - old_benchmark,
            "mfe_3d": mfe,
            "mae_3d": mae,
            "tradable_next_day": 1,
            "label_horizon_days": horizon_days,
            "label_source": "confirmed_minute_next_open",
        })
        result["stop_before_profit"] = self._stop_before_profit(future, entry_price)
        strong = mfe >= 0.08 and result["next_3d_excess_return"] > 0
        avoid = mae <= -0.04 and mfe < 0.02
        result["label_class"] = 2 if strong else 0 if avoid else 1
        result["label_name"] = {2: "strong_buy", 1: "hold", 0: "avoid"}[result["label_class"]]
        result["label_strong_buy"] = int(result["label_class"] == 2)
        result["label_hold"] = int(result["label_class"] == 1)
        result["label_avoid"] = int(result["label_class"] == 0)
        result["label_success"] = result["label_strong_buy"]
        return result, ""

    @staticmethod
    def _entry_mode(open_gap: float, allowed: Iterable[str]) -> str:
        allowed_set = set(allowed)
        wanted = "weak_to_strong" if -0.03 <= open_gap <= 0.01 else "continuation" if open_gap <= 0.05 else "acceleration"
        return MODE_MAP.get(wanted, "") if wanted in allowed_set else ""

    def _daily_window(self, code: str, plan_date: str, entry_date: str, horizon: int) -> pd.DataFrame:
        import duckdb  # type: ignore

        con = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT trade_date, open, high, low, close, vol_hand, amount_yuan "
                "FROM stock_daily_silver WHERE code=? AND trade_date>=? ORDER BY trade_date LIMIT ?",
                [code, plan_date, horizon + 1],
            ).fetchdf()
        finally:
            con.close()
        rows["trade_date"] = rows.get("trade_date", pd.Series(dtype=str)).astype(str)
        return rows

    def _sector_sync_callback(self, plan_date: str, entry_date: str, code: str, sectors: str):
        sector = str(sectors).replace("，", ",").split(",", 1)[0].strip()
        if not sector:
            return None
        import duckdb  # type: ignore

        con = duckdb.connect(str(self.duckdb_path), read_only=True)
        try:
            peers = con.execute(
                "SELECT code, ts_code FROM factor_stock_wide WHERE trade_date=? AND code<>? "
                "AND resonance_sectors LIKE ? ORDER BY total_score DESC LIMIT 8",
                [plan_date, code, f"%{sector}%"],
            ).fetchall()
        finally:
            con.close()
        frames = [self._load_tick(str(ts_code or self._ts_code(str(peer))), entry_date) for peer, ts_code in peers]
        frames = [normalize_minute_bars(frame) for frame in frames if frame is not None and not frame.empty]
        if len(frames) < 3:
            return None

        def synced(time_text: str) -> Optional[bool]:
            states = []
            for frame in frames:
                eligible = frame[frame["time"] <= str(time_text)]
                if eligible.empty:
                    continue
                states.append(float(eligible.iloc[-1]["close"]) >= float(frame.iloc[0]["open"]))
            return None if len(states) < 3 else sum(states) / len(states) >= 0.60

        return synced

    def _load_tick(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        path = self.tick_dir / f"{ts_code}_{trade_date}.csv"
        try:
            return pd.read_csv(path) if path.exists() else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    def _load_auction(self, ts_code: str, trade_date: str) -> Dict[str, Any]:
        path = self.auction_dir / f"{ts_code}_{trade_date}.json"
        try:
            return dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
        except Exception:
            return {}

    @staticmethod
    def _ts_code(code: str) -> str:
        return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ"

    @staticmethod
    def _stop_before_profit(future: pd.DataFrame, entry: float) -> int:
        for row in future.itertuples(index=False):
            hit_stop = float(row.low) <= entry * 0.96
            hit_profit = float(row.high) >= entry * 1.06
            if hit_stop and hit_profit:
                return 1
            if hit_stop:
                return 1
            if hit_profit:
                return 0
        return 0

    def _expected_amount_fraction(self, previous_amount: float, time_text: str) -> Optional[float]:
        fraction, samples = self.amount_profiles.expected_fraction(previous_amount, time_text)
        return fraction if fraction is not None and samples >= 10 else None


def strategy_id_is_leader(candidate: Mapping[str, Any], allowed_entry_modes: Iterable[str]) -> bool:
    strategy_id = str(candidate.get("strategy_id") or "")
    return strategy_id in {"mainline_leader", "ultra_short_board"} or (
        "acceleration" in set(allowed_entry_modes) and float(candidate.get("gold_rank") or 999) <= 3
    )


__all__ = [
    "STRATEGY_TRAINING_SPECS", "StrategyMinuteTrainingBuilder", "StrategyTrainingSpec",
    "strategy_training_spec",
]
