"""Market-level batch factor job."""
from __future__ import annotations

import pandas as pd

from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    read_table,
    safe_weighted_score,
    score_between,
    write_replace_partition,
    now_iso,
)


class MarketFactorJob:
    name = "market_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        stock = read_table(con, "stock_daily_silver", where="trade_date <= ?", params=[str(trade_date)])
        if stock.empty:
            result.ok = False
            result.add_message("stock_daily_silver 为空，无法计算大盘指标")
            return result

        stock["trade_date"] = stock["trade_date"].astype(str)
        stock["pct_chg"] = pd.to_numeric(stock.get("pct_chg"), errors="coerce").fillna(0)
        stock["amount_yuan"] = pd.to_numeric(stock.get("amount_yuan"), errors="coerce").fillna(0)
        today = stock[stock["trade_date"] == str(trade_date)].copy()
        if today.empty:
            result.ok = False
            result.add_message(f"stock_daily_silver 无 {trade_date} 数据")
            return result

        total_count = max(len(today), 1)
        up_ratio = float((today["pct_chg"] > 0).sum() / total_count)
        down_ratio = float((today["pct_chg"] < 0).sum() / total_count)
        avg_pct = float(today["pct_chg"].mean())
        limit_up_count = int((today["pct_chg"] >= 9.5).sum())
        limit_down_count = int((today["pct_chg"] <= -9.5).sum())
        amount_today = float(today["amount_yuan"].sum())

        by_day = stock.groupby("trade_date", as_index=False)["amount_yuan"].sum().sort_values("trade_date")
        prev_days = by_day[by_day["trade_date"] < str(trade_date)].tail(5)
        amount_base = float(prev_days["amount_yuan"].mean()) if not prev_days.empty else amount_today
        amount_ratio = amount_today / amount_base if amount_base > 0 else 1.0

        width_score = up_ratio * 100.0
        trend_score = score_between(avg_pct, -3.0, 3.0)
        volume_score = score_between(amount_ratio, 0.5, 1.8)
        emotion_score = score_between(limit_up_count - limit_down_count, -50.0, 120.0)
        market_score = safe_weighted_score([
            (trend_score, 0.25),
            (volume_score, 0.25),
            (width_score, 0.25),
            (emotion_score, 0.25),
        ])

        wide = pd.DataFrame([{
            "trade_date": str(trade_date),
            "market_score": market_score,
            "trend_score": trend_score,
            "volume_score": volume_score,
            "width_score": width_score,
            "emotion_score": emotion_score,
            "up_ratio": up_ratio,
            "down_ratio": down_ratio,
            "avg_pct_chg": avg_pct,
            "amount_yuan": amount_today,
            "amount_ratio_5d": amount_ratio,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "computed_at": now_iso(),
        }])

        long = long_records_to_frame([
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_width_up_ratio", raw_value=up_ratio, score=width_score,
                percentile=width_score, direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_avg_pct_chg", raw_value=avg_pct, score=trend_score,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_amount_ratio_5d", raw_value=amount_ratio, score=volume_score,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_limit_up_count", raw_value=limit_up_count, score=score_between(limit_up_count, 0, 120),
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_limit_down_count", raw_value=limit_down_count,
                score=score_between(limit_down_count, 0, 80, invert=True),
                direction="lower_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_market_score", raw_value=market_score, score=market_score,
                direction="higher_better",
            ),
        ])

        result.rows["factor_market_wide"] = write_replace_partition(
            con, "factor_market_wide", wide, where="trade_date = ?", params=[str(trade_date)]
        )
        result.rows["factor_value_long"] = write_replace_partition(
            con,
            "factor_value_long",
            long,
            where="trade_date = ? AND entity_type = ?",
            params=[str(trade_date), "market"],
        )
        return result
