"""Sector-level batch factor job."""
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


class SectorFactorJob:
    name = "sector_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        sector = read_table(con, "sector_daily_silver", where="trade_date <= ?", params=[str(trade_date)])
        if sector.empty:
            result.ok = False
            result.add_message("sector_daily_silver 为空，无法计算板块指标")
            return result

        sector["trade_date"] = sector["trade_date"].astype(str)
        for col in ("pct_chg", "amount_yuan", "close", "pre_close", "vol_hand"):
            sector[col] = pd.to_numeric(sector.get(col), errors="coerce").fillna(0)
        missing_pct = sector["pct_chg"].abs() <= 1e-12
        can_calc_pct = (sector["pre_close"] > 0) & (sector["close"] > 0)
        sector.loc[missing_pct & can_calc_pct, "pct_chg"] = (
            (sector.loc[missing_pct & can_calc_pct, "close"] - sector.loc[missing_pct & can_calc_pct, "pre_close"])
            / sector.loc[missing_pct & can_calc_pct, "pre_close"]
            * 100.0
        )
        missing_amount = sector["amount_yuan"] <= 0
        can_calc_amount = (sector["vol_hand"] > 0) & (sector["close"] > 0)
        sector.loc[missing_amount & can_calc_amount, "amount_yuan"] = (
            sector.loc[missing_amount & can_calc_amount, "vol_hand"]
            * sector.loc[missing_amount & can_calc_amount, "close"]
        )
        today = sector[sector["trade_date"] == str(trade_date)].copy()
        if today.empty:
            result.ok = False
            result.add_message(f"sector_daily_silver 无 {trade_date} 数据")
            return result

        today["momentum_score"] = today["pct_chg"].map(lambda v: score_between(v, -5.0, 8.0))
        today["amount_score"] = percentile_score(today["amount_yuan"], higher_better=True)

        hist = sector[sector["trade_date"] < str(trade_date)].sort_values(["sector_code", "trade_date"])
        avg_amount_5 = hist.groupby("sector_code").tail(5).groupby("sector_code")["amount_yuan"].mean()
        positive_days_3 = hist.groupby("sector_code").tail(3).assign(
            positive=lambda x: (x["pct_chg"] > 0).astype(float)
        ).groupby("sector_code")["positive"].sum()

        ratio_scores = []
        persistence_scores = []
        for _, row in today.iterrows():
            code = str(row.get("sector_code") or "")
            base = to_float(avg_amount_5.get(code), to_float(row.get("amount_yuan")))
            ratio = to_float(row.get("amount_yuan")) / base if base > 0 else 1.0
            ratio_scores.append(score_between(ratio, 0.5, 2.5))
            pos_days = to_float(positive_days_3.get(code), 0.0)
            current_pos = 1.0 if to_float(row.get("pct_chg")) > 0 else 0.0
            persistence_scores.append((pos_days + current_pos) / 4.0 * 100.0)

        today["amount_ratio_score"] = ratio_scores
        today["persistence_score"] = persistence_scores
        today["mainline_score"] = [
            safe_weighted_score([
                (row.momentum_score, 0.45),
                (row.amount_score, 0.25),
                (row.amount_ratio_score, 0.15),
                (row.persistence_score, 0.15),
            ])
            for row in today.itertuples()
        ]
        today["rank"] = today["mainline_score"].rank(method="dense", ascending=False).astype(int)

        wide = today[[
            "trade_date",
            "sector_code",
            "sector_name",
            "sector_type",
            "momentum_score",
            "amount_score",
            "amount_ratio_score",
            "persistence_score",
            "mainline_score",
            "rank",
        ]].copy()
        wide["computed_at"] = now_iso()

        records = []
        for _, row in today.iterrows():
            entity_id = str(row.get("sector_code") or "")
            pct = to_float(row.get("pct_chg"))
            amount = to_float(row.get("amount_yuan"))
            records.extend([
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_pct_chg_1d", raw_value=pct, score=row["momentum_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_amount_percentile", raw_value=amount, score=row["amount_score"],
                    percentile=row["amount_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_amount_ratio_5d_score", raw_value=row["amount_ratio_score"],
                    score=row["amount_ratio_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_persistence_score", raw_value=row["persistence_score"],
                    score=row["persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_mainline_score", raw_value=row["mainline_score"],
                    score=row["mainline_score"], rank_value=row["rank"], direction="higher_better",
                ),
            ])
        long = long_records_to_frame(records)

        result.rows["factor_sector_wide"] = write_replace_partition(
            con, "factor_sector_wide", wide, where="trade_date = ?", params=[str(trade_date)]
        )
        result.rows["factor_value_long"] = write_replace_partition(
            con,
            "factor_value_long",
            long,
            where="trade_date = ? AND entity_type = ?",
            params=[str(trade_date), "sector"],
        )
        return result
