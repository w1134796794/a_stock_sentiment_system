"""Stock-level batch factor job."""
from __future__ import annotations

import pandas as pd

from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    percentile_score,
    read_table,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
    now_iso,
)


def _activity_ratio_score(value: float) -> float:
    """Score turnover/volume expansion: moderate confirmation beats exhaustion."""
    v = to_float(value, 1.0)
    if v <= 0:
        return 0.0
    if v < 0.6:
        return max(20.0, 20.0 + (v / 0.6) * 20.0)
    if v < 1.0:
        return 40.0 + ((v - 0.6) / 0.4) * 30.0
    if v < 2.2:
        return 70.0 + ((v - 1.0) / 1.2) * 30.0
    if v < 3.0:
        return 100.0 - ((v - 2.2) / 0.8) * 25.0
    if v < 5.0:
        return 75.0 - ((v - 3.0) / 2.0) * 45.0
    return 20.0


class StockFactorJob:
    name = "stock_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        stock = read_table(con, "stock_daily_silver", where="trade_date <= ?", params=[str(trade_date)])
        if stock.empty:
            result.ok = False
            result.add_message("stock_daily_silver 为空，无法计算个股指标")
            return result

        stock["trade_date"] = stock["trade_date"].astype(str)
        for col in ("pct_chg", "vol_hand", "amount_yuan", "high", "close"):
            stock[col] = pd.to_numeric(stock.get(col), errors="coerce").fillna(0)
        stock = stock.sort_values(["code", "trade_date"])
        today = stock[stock["trade_date"] == str(trade_date)].copy()
        if today.empty:
            result.ok = False
            result.add_message(f"stock_daily_silver 无 {trade_date} 数据")
            return result

        hist = stock[stock["trade_date"] < str(trade_date)].copy()
        existing_wide = read_table(con, "factor_stock_wide", where="trade_date < ?", params=[str(trade_date)])
        amount_hist = hist[["trade_date", "code", "amount_yuan"]].copy() if not hist.empty else pd.DataFrame()
        vol_hist = hist[["trade_date", "code", "vol_hand"]].copy() if not hist.empty else pd.DataFrame()
        if not existing_wide.empty and {"trade_date", "code", "amount_yuan"}.issubset(existing_wide.columns):
            prev_amount = existing_wide[["trade_date", "code", "amount_yuan"]].copy()
            amount_hist = pd.concat([amount_hist, prev_amount], ignore_index=True)
        if not existing_wide.empty and {"trade_date", "code", "vol_hand"}.issubset(existing_wide.columns):
            prev_vol = existing_wide[["trade_date", "code", "vol_hand"]].copy()
            vol_hist = pd.concat([vol_hist, prev_vol], ignore_index=True)
        if not amount_hist.empty:
            amount_hist["trade_date"] = amount_hist["trade_date"].astype(str)
            amount_hist["amount_yuan"] = pd.to_numeric(amount_hist["amount_yuan"], errors="coerce").fillna(0)
            amount_hist = amount_hist.drop_duplicates(["trade_date", "code"], keep="last")
            amount_hist = amount_hist.sort_values(["code", "trade_date"])
        if not vol_hist.empty:
            vol_hist["trade_date"] = vol_hist["trade_date"].astype(str)
            vol_hist["vol_hand"] = pd.to_numeric(vol_hist["vol_hand"], errors="coerce").fillna(0)
            vol_hist = vol_hist.drop_duplicates(["trade_date", "code"], keep="last")
            vol_hist = vol_hist.sort_values(["code", "trade_date"])

        avg_vol_5 = (
            vol_hist.groupby("code").tail(5).groupby("code")["vol_hand"].mean()
            if not vol_hist.empty else pd.Series(dtype=float)
        )
        avg_amount_5 = (
            amount_hist.groupby("code").tail(5).groupby("code")["amount_yuan"].mean()
            if not amount_hist.empty else pd.Series(dtype=float)
        )
        high_20 = hist.groupby("code").tail(20).groupby("code")["high"].max()

        vol_ratio = []
        amount_ratio = []
        new_high_ratio = []
        for _, row in today.iterrows():
            code = str(row.get("code") or "")
            vol_base = to_float(avg_vol_5.get(code), to_float(row.get("vol_hand")))
            amount_base = to_float(avg_amount_5.get(code), to_float(row.get("amount_yuan")))
            high_base = to_float(high_20.get(code), to_float(row.get("high")))
            vol_ratio.append(to_float(row.get("vol_hand")) / vol_base if vol_base > 0 else 1.0)
            amount_ratio.append(to_float(row.get("amount_yuan")) / amount_base if amount_base > 0 else 1.0)
            new_high_ratio.append(to_float(row.get("close")) / high_base if high_base > 0 else 1.0)

        today["pct_score"] = today["pct_chg"].map(lambda v: score_between(v, -10.0, 10.0))
        today["vol_ratio"] = vol_ratio
        today["amount_ratio"] = amount_ratio
        today["new_high_ratio"] = new_high_ratio
        today["vol_ratio_score"] = today["vol_ratio"].map(_activity_ratio_score)
        today["amount_ratio_score"] = today["amount_ratio"].map(_activity_ratio_score)
        today["new_high_score"] = today["new_high_ratio"].map(lambda v: score_between(v, 0.85, 1.02))
        today["liquidity_score"] = percentile_score(today["amount_yuan"], higher_better=True)
        today["tech_score"] = [
            safe_weighted_score([(row.pct_score, 0.55), (row.new_high_score, 0.45)])
            for row in today.itertuples()
        ]
        today["volume_score"] = [
            safe_weighted_score([(row.vol_ratio_score, 0.5), (row.amount_ratio_score, 0.5)])
            for row in today.itertuples()
        ]
        today["sector_resonance_score"] = 50.0
        today["total_score"] = [
            safe_weighted_score([
                (row.tech_score, 0.40),
                (row.volume_score, 0.15),
                (row.liquidity_score, 0.30),
                (row.sector_resonance_score, 0.15),
            ])
            for row in today.itertuples()
        ]
        today["rank"] = today["total_score"].rank(method="dense", ascending=False).astype(int)

        wide = today[[
            "trade_date",
            "code",
            "ts_code",
            "name",
            "tech_score",
            "volume_score",
            "liquidity_score",
            "sector_resonance_score",
            "total_score",
            "rank",
            "pct_chg",
            "vol_ratio",
            "amount_ratio",
            "new_high_ratio",
            "vol_hand",
            "amount_yuan",
        ]].copy()
        wide["computed_at"] = now_iso()

        records = []
        for _, row in today.iterrows():
            entity_id = str(row.get("code") or "")
            records.extend([
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_pct_chg_1d", raw_value=row["pct_chg"], score=row["pct_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_vol_ratio_5d", raw_value=row["vol_ratio"], score=row["vol_ratio_score"],
                    direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_amount_ratio_5d", raw_value=row["amount_ratio"], score=row["amount_ratio_score"],
                    direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_new_high_20d", raw_value=row["new_high_ratio"], score=row["new_high_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_liquidity_percentile", raw_value=row["amount_yuan"],
                    score=row["liquidity_score"], percentile=row["liquidity_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_total_score", raw_value=row["total_score"], score=row["total_score"],
                    rank_value=row["rank"], direction="higher_better",
                ),
            ])
        long = long_records_to_frame(records)

        result.rows["factor_stock_wide"] = write_replace_partition(
            con, "factor_stock_wide", wide, where="trade_date = ?", params=[str(trade_date)]
        )
        result.rows["factor_value_long"] = write_replace_partition(
            con,
            "factor_value_long",
            long,
            where="trade_date = ? AND entity_type = ?",
            params=[str(trade_date), "stock"],
        )
        return result
