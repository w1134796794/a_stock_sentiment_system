"""Market-level batch factor job."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import CACHE_DIR
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


def _table_exists(con, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()[0])
    except Exception:
        return False


def _strict_limit_up_count(con, trade_date: str) -> int | None:
    try:
        if not _table_exists(con, "limit_up_pool_silver"):
            return None
        count = con.execute(
            "SELECT COUNT(*) FROM limit_up_pool_silver WHERE trade_date = ?",
            [str(trade_date)],
        ).fetchone()[0]
        return int(count)
    except Exception:
        return None


def _strict_limit_down_count(con, trade_date: str) -> int | None:
    try:
        if not _table_exists(con, "limit_down_pool_silver"):
            return None
        count = con.execute(
            "SELECT COUNT(*) FROM limit_down_pool_silver WHERE trade_date = ?",
            [str(trade_date)],
        ).fetchone()[0]
        return int(count)
    except Exception:
        return None


def _strict_limit_cache_count(trade_date: str, limit_type: str) -> int | None:
    folder = "limit_up" if limit_type == "U" else "limit_down"
    path = Path(CACHE_DIR) / "market" / folder / f"{trade_date}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        if "limit" in df.columns:
            df = df[df["limit"].astype(str).str.upper() == limit_type]
        return int(len(df))
    except Exception:
        return None


def _all_daily_amount_yuan(path: Path) -> float:
    if not path.exists():
        return 0.0
    try:
        df = pd.read_csv(path, usecols=lambda c: c in {"amount_yuan", "amount", "成交额"})
        if "amount_yuan" in df.columns:
            return float(pd.to_numeric(df["amount_yuan"], errors="coerce").fillna(0).sum())
        if "成交额" in df.columns:
            return float(pd.to_numeric(df["成交额"], errors="coerce").fillna(0).sum())
        if "amount" in df.columns:
            # Tushare daily.amount is in thousand yuan.
            return float(pd.to_numeric(df["amount"], errors="coerce").fillna(0).sum()) * 1000.0
    except Exception:
        return 0.0
    return 0.0


def _cached_amount_ratio_prev(trade_date: str, amount_today: float) -> float | None:
    base = Path(CACHE_DIR) / "stock" / "all_daily"
    if amount_today <= 0 or not base.exists():
        return None
    files = sorted(
        (p for p in base.glob("*.csv") if p.stem.isdigit() and p.stem < str(trade_date)),
        key=lambda p: p.stem,
    )
    if not files:
        return None
    amount_prev = _all_daily_amount_yuan(files[-1])
    if amount_prev <= 0:
        return None
    return amount_today / amount_prev


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

        limit_up_cache_count = _strict_limit_cache_count(trade_date, "U")
        limit_down_cache_count = _strict_limit_cache_count(trade_date, "D")
        limit_up_count = (
            limit_up_cache_count
            if limit_up_cache_count is not None
            else _strict_limit_up_count(con, trade_date)
        )
        limit_down_count = (
            limit_down_cache_count
            if limit_down_cache_count is not None
            else _strict_limit_down_count(con, trade_date)
        )
        if limit_up_count is None or limit_down_count is None:
            result.ok = False
            result.add_message("缺少 limit_list_d 涨跌停池，已拒绝使用 pct_chg 阈值推断涨跌停")
            return result
        amount_today = float(today["amount_yuan"].sum())

        by_day = stock.groupby("trade_date", as_index=False)["amount_yuan"].sum().sort_values("trade_date")
        prev_days = by_day[by_day["trade_date"] < str(trade_date)].tail(5)
        amount_base = float(prev_days["amount_yuan"].mean()) if not prev_days.empty else amount_today
        amount_ratio = amount_today / amount_base if amount_base > 0 else 1.0
        cached_amount_ratio = _cached_amount_ratio_prev(trade_date, amount_today)
        if cached_amount_ratio is not None:
            amount_ratio = cached_amount_ratio

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
