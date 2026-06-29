"""Point-in-time Dragon-Tiger list factors for stocks and sectors."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from config.settings import CACHE_DIR
from core.analysis.hm_reputation import HotMoneyReputationRegistry
from core.factors.jobs.gold_utils import (
    FactorJobResult,
    now_iso,
    read_recent_trade_dates,
    read_table,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
)


STOCK_COLUMNS = [
    "trade_date", "signal_date", "effective_date", "code", "ts_code", "name",
    "lhb_present", "list_reason_count", "appearance_days_5d", "positive_days_5d",
    "daily_amount_yuan", "lhb_buy_yuan", "lhb_sell_yuan", "lhb_net_buy_yuan",
    "lhb_net_buy_ratio", "lhb_net_buy_score", "institution_buy_yuan",
    "institution_sell_yuan", "institution_net_buy_yuan", "institution_net_buy_ratio",
    "institution_net_buy_score", "institution_buy_seats", "institution_sell_seats",
    "institution_consensus_score", "hot_money_net_buy_yuan", "hot_money_quality_score",
    "good_hot_money_buyers", "repeat_persistence_score", "seat_concentration",
    "crowding_penalty_score", "sector_lhb_resonance_score", "lhb_composite_score",
    "computed_at",
]

SECTOR_COLUMNS = [
    "trade_date", "signal_date", "effective_date", "sector_code", "sector_name",
    "sector_type", "lhb_stock_count", "lhb_net_buy_yuan", "institution_net_buy_yuan",
    "hot_money_net_buy_yuan", "good_hot_money_buyers", "sector_amount_yuan",
    "sector_lhb_net_buy_ratio", "sector_lhb_breadth_score", "sector_lhb_net_buy_score",
    "sector_lhb_institution_score", "sector_lhb_resonance_score", "computed_at",
]


def _effective_date(trade_date: str) -> str:
    try:
        from backtest.trade_calendar import TradeCalendar

        return TradeCalendar().next(str(trade_date))
    except Exception:
        return ""


def _primary_daily(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return daily
    work = daily.copy()
    for col in ("listed_amount_yuan", "net_buy_yuan", "amount_yuan"):
        work[col] = pd.to_numeric(work.get(col), errors="coerce").fillna(0.0)
    return (
        work.sort_values(["trade_date", "code", "listed_amount_yuan"], ascending=[True, True, False])
        .drop_duplicates(["trade_date", "code"], keep="first")
    )


def _dedupe_seats(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    work = frame.copy()
    keys = [col for col in ("trade_date", "code", "seat_name", "buy_yuan", "sell_yuan") if col in work.columns]
    return work.drop_duplicates(keys, keep="first") if keys else work


def _hot_money_quality(rows: pd.DataFrame, registry: HotMoneyReputationRegistry) -> tuple[float, int]:
    if rows.empty:
        return 50.0, 0
    weighted = 0.0
    total_weight = 0.0
    good_buyers = set()
    for row in rows.to_dict("records"):
        lookup = registry.lookup(
            hm_name=str(row.get("actor_name") or ""),
            org_text=str(row.get("seat_name") or ""),
        )
        net = to_float(row.get("net_buy_yuan"))
        weight = abs(net)
        directional = 50.0 + (lookup.score - 50.0) * (1.0 if net > 0 else -1.0 if net < 0 else 0.0)
        weighted += max(0.0, min(100.0, directional)) * weight
        total_weight += weight
        if net > 0 and lookup.label == "白":
            good_buyers.add(lookup.name)
    return (weighted / total_weight if total_weight > 0 else 50.0), len(good_buyers)


def _concentration(rows: pd.DataFrame) -> float:
    if rows.empty or "buy_yuan" not in rows.columns:
        return 0.0
    buys = pd.to_numeric(rows["buy_yuan"], errors="coerce").fillna(0.0).clip(lower=0)
    total = float(buys.sum())
    return float(buys.max() / total) if total > 0 else 0.0


def _crowding_score(concentration: float, turnover_rate: float, appearance_days: int) -> float:
    concentration_score = score_between(concentration, 0.30, 0.75)
    turnover_score = score_between(turnover_rate, 15.0, 45.0)
    repeat_score = score_between(appearance_days, 2.0, 5.0)
    return safe_weighted_score([
        (concentration_score, 0.55), (turnover_score, 0.25), (repeat_score, 0.20),
    ], default=0.0)


def _membership_rows(codes: Iterable[str], cache_dir: Path | None = None) -> List[Dict[str, str]]:
    root = Path(cache_dir or CACHE_DIR) / "sector" / "stock_sectors"
    rows: List[Dict[str, str]] = []
    if not root.exists():
        return rows
    for code in dict.fromkeys(str(code).split(".")[0].zfill(6) for code in codes):
        files = list(root.glob(f"{code}.*.csv"))
        if not files:
            continue
        try:
            frame = pd.read_csv(files[0])
        except Exception:
            continue
        for row in frame.to_dict("records"):
            typ = str(row.get("type") or "").strip().upper()
            if typ not in {"N", "I", "概念", "行业"}:
                continue
            sector_code = str(row.get("ts_code") or "").strip()
            if not sector_code:
                continue
            rows.append({
                "code": code,
                "sector_code": sector_code,
                "sector_name": str(row.get("name") or sector_code),
                "sector_type": "概念" if typ in {"N", "概念"} else "行业",
            })
    return rows


class LHBFactorJob:
    name = "lhb_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        effective_date = _effective_date(trade_date)
        daily = read_recent_trade_dates(con, "lhb_daily_silver", trade_date, days=5)
        institution = read_recent_trade_dates(con, "lhb_institution_silver", trade_date, days=5)
        hot_money = read_recent_trade_dates(con, "lhb_hot_money_silver", trade_date, days=5)

        for frame in (daily, institution, hot_money):
            if not frame.empty and "trade_date" in frame.columns:
                frame["trade_date"] = frame["trade_date"].astype(str)
            if not frame.empty and "code" in frame.columns:
                frame["code"] = frame["code"].astype(str).str.split(".").str[0].str.zfill(6)

        today_daily = daily[daily["trade_date"] == str(trade_date)].copy() if not daily.empty else pd.DataFrame()
        today_inst = institution[institution["trade_date"] == str(trade_date)].copy() if not institution.empty else pd.DataFrame()
        today_hot = hot_money[hot_money["trade_date"] == str(trade_date)].copy() if not hot_money.empty else pd.DataFrame()
        today_inst = _dedupe_seats(today_inst)
        today_hot = _dedupe_seats(today_hot)

        codes = set(today_daily.get("code", pd.Series(dtype=str)).astype(str))
        codes.update(today_inst.get("code", pd.Series(dtype=str)).astype(str))
        codes.update(today_hot.get("code", pd.Series(dtype=str)).astype(str))
        if not codes:
            stock_wide = pd.DataFrame(columns=STOCK_COLUMNS)
            sector_wide = pd.DataFrame(columns=SECTOR_COLUMNS)
            result.add_message("当日无龙虎榜数据，因子按中性降级")
        else:
            stock_wide = self._build_stock_wide(
                con, str(trade_date), effective_date, codes, daily, today_daily, today_inst, today_hot,
            )
            sector_wide, stock_sector_scores = self._build_sector_wide(
                con, str(trade_date), effective_date, stock_wide,
            )
            stock_wide["sector_lhb_resonance_score"] = stock_wide["code"].map(stock_sector_scores).fillna(50.0)
            stock_wide["lhb_composite_score"] = stock_wide.apply(self._composite_score, axis=1)
            stock_wide = stock_wide[STOCK_COLUMNS]

        result.rows["factor_lhb_stock_wide"] = write_replace_partition(
            con, "factor_lhb_stock_wide", stock_wide,
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        result.rows["factor_lhb_sector_wide"] = write_replace_partition(
            con, "factor_lhb_sector_wide", sector_wide,
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        return result

    @staticmethod
    def _build_stock_wide(
        con, trade_date: str, effective_date: str, codes: set[str], daily: pd.DataFrame,
        today_daily: pd.DataFrame, today_inst: pd.DataFrame, today_hot: pd.DataFrame,
    ) -> pd.DataFrame:
        primary_today = _primary_daily(today_daily)
        primary_map = primary_today.set_index("code").to_dict("index") if not primary_today.empty else {}
        reason_counts = today_daily.groupby("code")["reason"].nunique().to_dict() if not today_daily.empty else {}
        history_primary = _primary_daily(daily)
        appearance = history_primary.groupby("code")["trade_date"].nunique().to_dict() if not history_primary.empty else {}
        positive_days = (
            history_primary.assign(positive=pd.to_numeric(history_primary["net_buy_yuan"], errors="coerce").fillna(0) > 0)
            .groupby("code")["positive"].sum().to_dict()
            if not history_primary.empty else {}
        )
        stock_daily = read_table(con, "stock_daily_silver", where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date])
        if not stock_daily.empty:
            stock_daily["code"] = stock_daily["code"].astype(str).str.split(".").str[0].str.zfill(6)
        stock_map = stock_daily.drop_duplicates("code").set_index("code").to_dict("index") if not stock_daily.empty else {}
        registry = HotMoneyReputationRegistry.load()
        computed_at = now_iso()
        rows = []
        for code in sorted(codes):
            summary = primary_map.get(code, {})
            stock_row = stock_map.get(code, {})
            inst_rows = today_inst[today_inst["code"] == code] if not today_inst.empty else pd.DataFrame()
            actual_inst = inst_rows[inst_rows["is_institution"].astype(bool)] if not inst_rows.empty else pd.DataFrame()
            hot_rows = today_hot[today_hot["code"] == code] if not today_hot.empty else pd.DataFrame()
            amount = to_float(stock_row.get("amount_yuan"), to_float(summary.get("amount_yuan")))
            net = to_float(summary.get("net_buy_yuan"))
            ratio = net / amount * 100.0 if amount > 0 else 0.0
            inst_buy = float(pd.to_numeric(actual_inst.get("buy_yuan"), errors="coerce").fillna(0).sum()) if not actual_inst.empty else 0.0
            inst_sell = float(pd.to_numeric(actual_inst.get("sell_yuan"), errors="coerce").fillna(0).sum()) if not actual_inst.empty else 0.0
            inst_net = float(pd.to_numeric(actual_inst.get("net_buy_yuan"), errors="coerce").fillna(0).sum()) if not actual_inst.empty else 0.0
            inst_ratio = inst_net / amount * 100.0 if amount > 0 else 0.0
            inst_buy_seats = int((pd.to_numeric(actual_inst.get("net_buy_yuan"), errors="coerce").fillna(0) > 0).sum()) if not actual_inst.empty else 0
            inst_sell_seats = int((pd.to_numeric(actual_inst.get("net_buy_yuan"), errors="coerce").fillna(0) < 0).sum()) if not actual_inst.empty else 0
            inst_total = inst_buy_seats + inst_sell_seats
            inst_consensus = 50.0 + ((inst_buy_seats - inst_sell_seats) / inst_total * 50.0 if inst_total else 0.0)
            hot_net = float(pd.to_numeric(hot_rows.get("net_buy_yuan"), errors="coerce").fillna(0).sum()) if not hot_rows.empty else 0.0
            hot_quality, good_buyers = _hot_money_quality(hot_rows, registry)
            app_days = int(appearance.get(code, 0))
            pos_days = int(positive_days.get(code, 0))
            repeat_score = safe_weighted_score([
                (score_between(app_days, 1.0, 5.0), 0.55),
                ((pos_days / app_days * 100.0) if app_days else 50.0, 0.45),
            ])
            all_seats = pd.concat([inst_rows, hot_rows], ignore_index=True) if not inst_rows.empty or not hot_rows.empty else pd.DataFrame()
            concentration = _concentration(all_seats)
            crowding = _crowding_score(concentration, to_float(summary.get("turnover_rate")), app_days)
            rows.append({
                "trade_date": trade_date,
                "signal_date": trade_date,
                "effective_date": effective_date,
                "code": code,
                "ts_code": str(summary.get("ts_code") or stock_row.get("ts_code") or ""),
                "name": str(summary.get("name") or stock_row.get("name") or ""),
                "lhb_present": 1,
                "list_reason_count": int(reason_counts.get(code, 0)),
                "appearance_days_5d": app_days,
                "positive_days_5d": pos_days,
                "daily_amount_yuan": amount,
                "lhb_buy_yuan": to_float(summary.get("listed_buy_yuan")),
                "lhb_sell_yuan": to_float(summary.get("listed_sell_yuan")),
                "lhb_net_buy_yuan": net,
                "lhb_net_buy_ratio": ratio,
                "lhb_net_buy_score": score_between(ratio, -8.0, 12.0),
                "institution_buy_yuan": inst_buy,
                "institution_sell_yuan": inst_sell,
                "institution_net_buy_yuan": inst_net,
                "institution_net_buy_ratio": inst_ratio,
                "institution_net_buy_score": score_between(inst_ratio, -5.0, 8.0) if inst_total else 50.0,
                "institution_buy_seats": inst_buy_seats,
                "institution_sell_seats": inst_sell_seats,
                "institution_consensus_score": inst_consensus,
                "hot_money_net_buy_yuan": hot_net,
                "hot_money_quality_score": hot_quality,
                "good_hot_money_buyers": good_buyers,
                "repeat_persistence_score": repeat_score,
                "seat_concentration": concentration,
                "crowding_penalty_score": crowding,
                "sector_lhb_resonance_score": 50.0,
                "lhb_composite_score": 50.0,
                "computed_at": computed_at,
            })
        return pd.DataFrame(rows, columns=STOCK_COLUMNS)

    @staticmethod
    def _build_sector_wide(
        con, trade_date: str, effective_date: str, stock_wide: pd.DataFrame,
    ) -> tuple[pd.DataFrame, Dict[str, float]]:
        memberships = pd.DataFrame(_membership_rows(stock_wide["code"].tolist()))
        if memberships.empty:
            return pd.DataFrame(columns=SECTOR_COLUMNS), {}
        joined = memberships.merge(stock_wide, on="code", how="inner")
        sector_daily = read_table(con, "sector_daily_silver", where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date])
        sector_amount = {
            str(row.get("sector_code") or ""): to_float(row.get("amount_yuan"))
            for row in sector_daily.to_dict("records")
        } if not sector_daily.empty else {}
        rows = []
        computed_at = now_iso()
        for sector_code, group in joined.groupby("sector_code"):
            amount = sector_amount.get(str(sector_code), 0.0)
            net = float(group.drop_duplicates("code")["lhb_net_buy_yuan"].sum())
            inst_net = float(group.drop_duplicates("code")["institution_net_buy_yuan"].sum())
            hot_net = float(group.drop_duplicates("code")["hot_money_net_buy_yuan"].sum())
            count = int(group["code"].nunique())
            ratio = net / amount * 100.0 if amount > 0 else 0.0
            breadth_score = score_between(count, 1.0, 4.0)
            net_score = score_between(ratio, -3.0, 6.0)
            stock_amount = float(group.drop_duplicates("code")["daily_amount_yuan"].sum())
            inst_ratio = inst_net / stock_amount * 100.0 if stock_amount > 0 else 0.0
            inst_score = score_between(inst_ratio, -3.0, 6.0)
            resonance = safe_weighted_score([
                (breadth_score, 0.40), (net_score, 0.35), (inst_score, 0.25),
            ])
            first = group.iloc[0]
            rows.append({
                "trade_date": trade_date,
                "signal_date": trade_date,
                "effective_date": effective_date,
                "sector_code": str(sector_code),
                "sector_name": str(first.get("sector_name") or sector_code),
                "sector_type": str(first.get("sector_type") or ""),
                "lhb_stock_count": count,
                "lhb_net_buy_yuan": net,
                "institution_net_buy_yuan": inst_net,
                "hot_money_net_buy_yuan": hot_net,
                "good_hot_money_buyers": int(group.drop_duplicates("code")["good_hot_money_buyers"].sum()),
                "sector_amount_yuan": amount,
                "sector_lhb_net_buy_ratio": ratio,
                "sector_lhb_breadth_score": breadth_score,
                "sector_lhb_net_buy_score": net_score,
                "sector_lhb_institution_score": inst_score,
                "sector_lhb_resonance_score": resonance,
                "computed_at": computed_at,
            })
        sector_wide = pd.DataFrame(rows, columns=SECTOR_COLUMNS)
        score_map = sector_wide.set_index("sector_code")["sector_lhb_resonance_score"].to_dict()
        stock_scores = joined.assign(
            score=joined["sector_code"].map(score_map).fillna(50.0)
        ).groupby("code")["score"].max().to_dict()
        return sector_wide, stock_scores

    @staticmethod
    def _composite_score(row: pd.Series) -> float:
        base = safe_weighted_score([
            (row.get("lhb_net_buy_score"), 0.25),
            (row.get("institution_net_buy_score"), 0.20),
            (row.get("institution_consensus_score"), 0.10),
            (row.get("hot_money_quality_score"), 0.10),
            (row.get("repeat_persistence_score"), 0.15),
            (row.get("sector_lhb_resonance_score"), 0.20),
        ])
        return max(0.0, min(100.0, base - to_float(row.get("crowding_penalty_score")) * 0.15))


__all__ = ["LHBFactorJob"]
