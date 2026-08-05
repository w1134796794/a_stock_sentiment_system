"""Market-level batch factor job."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import CACHE_DIR
from core.factors.jobs.first_board_factors import first_board_market_metrics
from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    now_iso,
    read_recent_trade_dates,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
)
from core.models.market_state import MarketStateSnapshot, classify_emotion_phase


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


def _normalize_code(value: object) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _read_limit_pool(con, trade_date: str) -> pd.DataFrame:
    if not _table_exists(con, "limit_up_pool_silver"):
        return pd.DataFrame()
    try:
        columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info('limit_up_pool_silver')").fetchall()
        }
        open_times_expr = "open_times" if "open_times" in columns else "0 AS open_times"
        return con.execute(
            f"""
            SELECT code, ts_code, limit_times, {open_times_expr}
            FROM limit_up_pool_silver
            WHERE CAST(trade_date AS VARCHAR) = ?
            """,
            [str(trade_date)],
        ).fetchdf()
    except Exception:
        return pd.DataFrame()


def _previous_trade_date(con, trade_date: str) -> str:
    try:
        row = con.execute(
            """
            SELECT MAX(CAST(trade_date AS VARCHAR))
            FROM stock_daily_silver
            WHERE CAST(trade_date AS VARCHAR) < ?
            """,
            [str(trade_date)],
        ).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _echelon_integrity(limit_pool: pd.DataFrame) -> float | None:
    """Measure whether the existing board-height ladder is filled from 1 upward."""
    if limit_pool.empty or "limit_times" not in limit_pool.columns:
        return None
    heights = pd.to_numeric(limit_pool["limit_times"], errors="coerce").dropna()
    heights = heights.round().clip(lower=1, upper=5).astype(int)
    if heights.empty:
        return None
    counts = heights.value_counts().to_dict()
    highest = int(heights.max())
    levels = list(range(1, highest + 1))
    coverage = sum(int(counts.get(level, 0) > 0) for level in levels) / len(levels)
    if highest <= 1:
        monotonic = 1.0
    else:
        monotonic = sum(
            int(counts.get(level, 0) >= counts.get(level + 1, 0) > 0)
            for level in range(1, highest)
        ) / (highest - 1)
    return round(0.70 * coverage + 0.30 * monotonic, 4)


def _previous_limit_feedback(
    con,
    trade_date: str,
) -> tuple[float | None, float | None, float | None]:
    """Evaluate yesterday's official limit-up cohort on today's actual prices."""
    previous_date = _previous_trade_date(con, trade_date)
    if not previous_date:
        return None, None, None
    pool = _read_limit_pool(con, previous_date)
    if pool.empty:
        return None, None, None
    try:
        quotes = con.execute(
            """
            SELECT code, ts_code, open, close, pre_close
            FROM stock_daily_silver
            WHERE CAST(trade_date AS VARCHAR) = ?
            """,
            [str(trade_date)],
        ).fetchdf()
    except Exception:
        return None, None, None
    if quotes.empty:
        return None, None, None
    pool = pool.copy()
    quotes = quotes.copy()
    pool["code6"] = pool["code"].where(pool["code"].notna(), pool.get("ts_code")).map(_normalize_code)
    quotes["code6"] = quotes["code"].where(
        quotes["code"].notna(), quotes.get("ts_code"),
    ).map(_normalize_code)
    merged = pool.merge(quotes, on="code6", how="inner", suffixes=("_pool", ""))
    for col in ("open", "close", "pre_close"):
        merged[col] = pd.to_numeric(merged.get(col), errors="coerce")
    merged = merged[merged["pre_close"] > 0].copy()
    if merged.empty:
        return None, None, None
    open_gap = (merged["open"] / merged["pre_close"] - 1.0) * 100.0
    premium = float(open_gap.mean())
    positive = float((merged["close"] > merged["pre_close"]).mean())
    board_height = pd.to_numeric(merged.get("limit_times"), errors="coerce")
    first_board = merged[board_height.fillna(1.0) <= 1.0]
    first_board_gap = (
        float((first_board["open"] > first_board["pre_close"]).mean())
        if not first_board.empty else None
    )
    return premium, positive, first_board_gap


def _previous_market_rows(con, trade_date: str, limit: int = 30) -> pd.DataFrame:
    if not _table_exists(con, "factor_market_wide"):
        return pd.DataFrame()
    try:
        columns = {
            str(row[0])
            for row in con.execute("DESCRIBE factor_market_wide").fetchall()
        }
        selected = ["trade_date", "market_score"]
        if "emotion_phase" in columns:
            selected.append("emotion_phase")
        if "cycle_duration" in columns:
            selected.append("cycle_duration")
        sql_columns = ", ".join(f'"{column}"' for column in selected)
        return con.execute(
            f"""
            SELECT {sql_columns}
            FROM factor_market_wide
            WHERE CAST(trade_date AS VARCHAR) < ?
            ORDER BY CAST(trade_date AS VARCHAR) DESC
            LIMIT ?
            """,
            [str(trade_date), int(limit)],
        ).fetchdf()
    except Exception:
        return pd.DataFrame()


def _phase_family(phase: str) -> str:
    return "risk_on" if phase in {"active", "boom"} else str(phase)


def _cycle_duration(con, trade_date: str, current_phase: str) -> int:
    family = _phase_family(current_phase)
    duration = 1
    for row in _previous_market_rows(con, trade_date).to_dict("records"):
        previous_phase = str(row.get("emotion_phase") or "")
        if not previous_phase:
            previous_score = float(row.get("market_score") or 50.0)
            previous_phase, _ = classify_emotion_phase(previous_score)
        if _phase_family(previous_phase) != family:
            break
        duration += 1
    return duration


def _previous_market_score(con, trade_date: str) -> float | None:
    history = _previous_market_rows(con, trade_date, limit=1)
    if history.empty:
        return None
    try:
        return float(history.iloc[0].get("market_score"))
    except (TypeError, ValueError):
        return None


class MarketFactorJob:
    name = "market_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        stock = read_recent_trade_dates(
            con,
            "stock_daily_silver",
            trade_date,
            days=6,
            columns=("trade_date", "pct_chg", "amount_yuan"),
        )
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

        limit_up_count = _strict_limit_up_count(con, trade_date)
        limit_down_count = _strict_limit_down_count(con, trade_date)
        if limit_up_count is None:
            limit_up_count = _strict_limit_cache_count(trade_date, "U")
        if limit_down_count is None:
            limit_down_count = _strict_limit_cache_count(trade_date, "D")
        if limit_up_count is None or limit_down_count is None:
            result.ok = False
            result.add_message("缺少 limit_list_d 涨跌停池，已拒绝使用 pct_chg 阈值推断涨跌停")
            return result
        amount_today = float(today["amount_yuan"].sum())

        by_day = stock.groupby("trade_date", as_index=False)["amount_yuan"].sum().sort_values("trade_date")
        prev_days = by_day[by_day["trade_date"] < str(trade_date)].tail(5)
        if not prev_days.empty:
            amount_base = float(prev_days["amount_yuan"].mean())
            amount_ratio = amount_today / amount_base if amount_base > 0 else 1.0
        else:
            # Historical recomputation must not be overwritten by the latest
            # filesystem cache, which may contain data after trade_date.
            cached_amount_ratio = _cached_amount_ratio_prev(trade_date, amount_today)
            amount_ratio = cached_amount_ratio if cached_amount_ratio is not None else 1.0

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
        limit_pool = _read_limit_pool(con, trade_date)
        if not limit_pool.empty and "open_times" in limit_pool.columns:
            open_times = pd.to_numeric(limit_pool["open_times"], errors="coerce").fillna(0.0)
            broken_rate = float((open_times > 0).sum() / max(limit_up_count, 1) * 100.0)
        else:
            broken_rate = 0.0
        echelon_integrity = _echelon_integrity(limit_pool)
        prev_premium, prev_positive, prev_first_board_gap = _previous_limit_feedback(
            con, trade_date,
        )
        first_board_market = first_board_market_metrics(
            con, str(trade_date), limit_pool,
        )
        previous_market_score = _previous_market_score(con, trade_date)
        market_score_change = (
            market_score - previous_market_score
            if previous_market_score is not None else None
        )
        market_context = {
            "market_score_change": market_score_change,
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "broken_rate": broken_rate,
            "echelon_integrity": echelon_integrity,
            "prev_limit_up_premium": prev_premium,
            "prev_limit_up_positive": prev_positive,
            "prev_first_board_gap_up": prev_first_board_gap,
            "first_board_sector_resonance_ratio": first_board_market[
                "first_board_sector_resonance_ratio"
            ],
            "first_board_cluster_count": first_board_market["first_board_cluster_count"],
            "first_board_follow_through_ratio": first_board_market[
                "first_board_follow_through_ratio"
            ],
            "market_emotion_divergence": abs(trend_score - emotion_score),
        }
        provisional_phase, _ = classify_emotion_phase(market_score, market_context)
        market_context["cycle_duration"] = _cycle_duration(
            con, trade_date, provisional_phase,
        )
        market_state = MarketStateSnapshot.resolve(
            market_score, trade_date=trade_date, context=market_context,
        )

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
            "broken_rate": broken_rate,
            "market_score_change": market_score_change,
            "cycle_duration": market_context["cycle_duration"],
            "market_emotion_divergence": market_context["market_emotion_divergence"],
            "echelon_integrity": echelon_integrity,
            "prev_limit_up_premium": prev_premium,
            "prev_limit_up_positive": prev_positive,
            "prev_first_board_gap_up": prev_first_board_gap,
            "first_board_sector_resonance_ratio": first_board_market[
                "first_board_sector_resonance_ratio"
            ],
            "first_board_cluster_count": first_board_market["first_board_cluster_count"],
            "first_board_follow_through_ratio": first_board_market[
                "first_board_follow_through_ratio"
            ],
            "emotion_phase": market_state.phase,
            "emotion_phase_label": market_state.phase_label,
            "emotion_phase_reason": "；".join(market_state.phase_reasons),
            "market_position_scale": market_state.position_scale,
            "market_risk_flags": ",".join(market_state.risk_flags),
            "computed_at": now_iso(),
        }])
        for column in (
            "market_score",
            "trend_score",
            "volume_score",
            "width_score",
            "emotion_score",
            "up_ratio",
            "down_ratio",
            "avg_pct_chg",
            "amount_yuan",
            "amount_ratio_5d",
            "limit_up_count",
            "limit_down_count",
            "broken_rate",
            "market_score_change",
            "cycle_duration",
            "market_emotion_divergence",
            "echelon_integrity",
            "prev_limit_up_premium",
            "prev_limit_up_positive",
            "prev_first_board_gap_up",
            "first_board_sector_resonance_ratio",
            "first_board_cluster_count",
            "first_board_follow_through_ratio",
            "market_position_scale",
        ):
            wide[column] = pd.to_numeric(wide[column], errors="coerce")

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
                factor_id="mkt_broken_rate", raw_value=broken_rate,
                score=score_between(broken_rate, 0, 60, invert=True),
                direction="lower_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_market_score", raw_value=market_score, score=market_score,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="F1_cycle_duration",
                raw_value=market_context["cycle_duration"],
                score=score_between(market_context["cycle_duration"], 1, 15, invert=True),
                direction="lower_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="F2_market_emotion_divergence",
                raw_value=market_context["market_emotion_divergence"],
                score=score_between(
                    market_context["market_emotion_divergence"], 0, 60, invert=True,
                ),
                direction="lower_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="echelon_integrity",
                raw_value=echelon_integrity,
                score=None if echelon_integrity is None else echelon_integrity * 100.0,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="prev_limit_up_premium",
                raw_value=prev_premium,
                score=None if prev_premium is None else score_between(prev_premium, -3, 3),
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="prev_limit_up_positive",
                raw_value=prev_positive,
                score=None if prev_positive is None else prev_positive * 100.0,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="prev_first_board_gap_up",
                raw_value=prev_first_board_gap,
                score=None if prev_first_board_gap is None else prev_first_board_gap * 100.0,
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="first_board_sector_resonance_ratio",
                raw_value=first_board_market["first_board_sector_resonance_ratio"],
                score=(
                    None if first_board_market["first_board_sector_resonance_ratio"] is None
                    else to_float(first_board_market["first_board_sector_resonance_ratio"]) * 100.0
                ),
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="first_board_cluster_count",
                raw_value=first_board_market["first_board_cluster_count"],
                score=(
                    None if first_board_market["first_board_cluster_count"] is None
                    else score_between(first_board_market["first_board_cluster_count"], 0, 8)
                ),
                direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="first_board_follow_through_ratio",
                raw_value=first_board_market["first_board_follow_through_ratio"],
                score=(
                    None if first_board_market["first_board_follow_through_ratio"] is None
                    else to_float(first_board_market["first_board_follow_through_ratio"]) * 100.0
                ),
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
