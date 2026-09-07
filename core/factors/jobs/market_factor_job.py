"""Market-level batch factor job."""
from __future__ import annotations

import json
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


def _previous_limit_pool_date(con, trade_date: str) -> str:
    """Return the latest earlier date with an official limit-up pool."""
    if not _table_exists(con, "limit_up_pool_silver"):
        return ""
    try:
        row = con.execute(
            """
            SELECT MAX(CAST(trade_date AS VARCHAR))
            FROM limit_up_pool_silver
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
    previous_date = _previous_limit_pool_date(con, trade_date)
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
        if "profit_effect_score" in columns:
            selected.append("profit_effect_score")
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


def _weighted_available(parts: list[tuple[float | None, float]], default: float = 50.0) -> float:
    valid = [
        (float(score), float(weight))
        for score, weight in parts
        if score is not None and pd.notna(score) and weight > 0
    ]
    if not valid:
        return default
    weight_sum = sum(weight for _, weight in valid)
    return sum(score * weight for score, weight in valid) / weight_sum


def _promotion_metrics(con, trade_date: str, current_pool: pd.DataFrame) -> dict[str, dict[str, float | int | None]]:
    """Return raw and small-sample-adjusted board promotion rates."""
    previous_date = _previous_limit_pool_date(con, trade_date)
    previous_pool = _read_limit_pool(con, previous_date) if previous_date else pd.DataFrame()
    names = ("overall", "rate_1to2", "rate_2to3", "rate_3to4", "rate_high")
    empty = {
        name: {"rate": None, "adjusted_rate": None, "success": 0, "sample": 0}
        for name in names
    }
    if previous_pool.empty or current_pool.empty:
        return empty

    def board_map(frame: pd.DataFrame) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in frame.to_dict("records"):
            code = _normalize_code(row.get("code") or row.get("ts_code"))
            if not code:
                continue
            height = max(int(to_float(row.get("limit_times"), 1.0)), 1)
            result[code] = height
        return result

    previous = board_map(previous_pool)
    current = board_map(current_pool)

    def measure(*, level: int | None = None, high: bool = False) -> dict[str, float | int | None]:
        cohort = [
            (code, height)
            for code, height in previous.items()
            if level is None and not high
            or level is not None and height == level
            or high and height >= 4
        ]
        sample = len(cohort)
        if not sample:
            return {"rate": None, "adjusted_rate": None, "success": 0, "sample": 0}
        success = sum(1 for code, height in cohort if current.get(code, 0) >= height + 1)
        raw_rate = success / sample * 100.0
        # Beta(2, 2): four neutral prior samples prevent tiny cohorts from reading as 0/100.
        adjusted_rate = (success + 2.0) / (sample + 4.0) * 100.0
        return {
            "rate": round(raw_rate, 2),
            "adjusted_rate": round(adjusted_rate, 2),
            "success": success,
            "sample": sample,
        }

    return {
        "overall": measure(),
        "rate_1to2": measure(level=1),
        "rate_2to3": measure(level=2),
        "rate_3to4": measure(level=3),
        "rate_high": measure(high=True),
    }


def _promotion_history(con, trade_date: str, days: int = 5) -> list[dict[str, object]]:
    if not _table_exists(con, "limit_up_pool_silver"):
        return []
    try:
        rows = con.execute(
            """
            SELECT DISTINCT CAST(trade_date AS VARCHAR) AS trade_date
            FROM limit_up_pool_silver
            WHERE CAST(trade_date AS VARCHAR) <= ?
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [str(trade_date), max(int(days) + 1, 2)],
        ).fetchall()
    except Exception:
        return []
    dates = list(reversed([str(row[0]) for row in rows if row and row[0]]))
    history: list[dict[str, object]] = []
    for current_date in dates[1:]:
        metrics = _promotion_metrics(con, current_date, _read_limit_pool(con, current_date))
        item: dict[str, object] = {"trade_date": current_date}
        for key in ("rate_1to2", "rate_2to3", "rate_3to4", "rate_high"):
            item[key] = metrics[key]["rate"]
            item[f"{key}_adjusted"] = metrics[key]["adjusted_rate"]
            item[f"{key}_success"] = metrics[key]["success"]
            item[f"{key}_sample"] = metrics[key]["sample"]
        history.append(item)
    return history[-max(int(days), 1):]


def _series_slope(values: list[tuple[int, float]]) -> float | None:
    if len(values) < 2:
        return None
    x_mean = sum(x for x, _ in values) / len(values)
    y_mean = sum(y for _, y in values) / len(values)
    denominator = sum((x - x_mean) ** 2 for x, _ in values)
    if denominator <= 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in values) / denominator


def _promotion_trend_from_history(history: list[dict[str, object]]) -> dict[str, object]:
    keys = ("rate_1to2", "rate_2to3", "rate_3to4", "rate_high")
    weights = {"rate_1to2": 0.30, "rate_2to3": 0.30, "rate_3to4": 0.20, "rate_high": 0.20}
    tier_scores: dict[str, float | None] = {}
    tier_slopes: dict[str, float | None] = {}
    for key in keys:
        values = [
            (index, float(value))
            for index, row in enumerate(history)
            if (value := row.get(f"{key}_adjusted")) is not None and pd.notna(value)
        ]
        slope = _series_slope(values)
        tier_slopes[key] = round(slope, 2) if slope is not None else None
        if not values:
            tier_scores[key] = None
            continue
        recency = list(range(1, len(values) + 1))
        average = sum(value * weight for (_, value), weight in zip(values, recency, strict=False)) / sum(recency)
        tier_scores[key] = round(max(0.0, min(100.0, average + (slope or 0.0) * 2.0)), 2)

    valid_scores = [(tier_scores[key], weights[key]) for key in keys if tier_scores[key] is not None]
    score = (
        sum(float(value) * weight for value, weight in valid_scores)
        / sum(weight for _, weight in valid_scores)
        if valid_scores else None
    )
    valid_slopes = [float(value) for value in tier_slopes.values() if value is not None]
    weighted_slope_parts = [
        (float(tier_slopes[key]), weights[key])
        for key in keys if tier_slopes[key] is not None
    ]
    overall_slope = (
        sum(value * weight for value, weight in weighted_slope_parts)
        / sum(weight for _, weight in weighted_slope_parts)
        if weighted_slope_parts else None
    )
    positive = sum(value >= 2.0 for value in valid_slopes)
    negative = sum(value <= -2.0 for value in valid_slopes)
    first_slope = tier_slopes.get("rate_1to2")
    high_slope = tier_slopes.get("rate_high")
    mid_rising = any((tier_slopes.get(key) or 0.0) >= 2.0 for key in ("rate_2to3", "rate_3to4"))
    if len(history) < 3 or len(valid_slopes) < 2:
        label = "样本不足"
    elif first_slope is not None and high_slope is not None and first_slope <= -2.0 < high_slope:
        label = "高位抱团"
    elif positive >= 3:
        label = "接力升温"
    elif negative >= 3:
        label = "接力退潮"
    elif mid_rising:
        label = "主线发酵"
    else:
        label = "接力分化"
    return {
        "score": round(score, 2) if score is not None else None,
        "label": label,
        "slope": round(overall_slope, 2) if overall_slope is not None else None,
        "sample_days": len(history),
        "tier_scores": tier_scores,
        "tier_slopes": tier_slopes,
        "history": history,
    }


def _promotion_trend(con, trade_date: str, days: int = 5) -> dict[str, object]:
    return _promotion_trend_from_history(_promotion_history(con, trade_date, days=days))


def _profit_effect_label(score: float) -> str:
    if score >= 75:
        return "赚钱效应强"
    if score >= 60:
        return "赚钱效应较好"
    if score >= 45:
        return "赚钱效应分化"
    if score >= 30:
        return "赚钱效应较差"
    return "明显亏钱效应"


def _profit_effect_trend(score: float, history: pd.DataFrame) -> tuple[float | None, str]:
    if history.empty or "profit_effect_score" not in history.columns:
        return None, "趋势待积累"
    values = pd.to_numeric(history["profit_effect_score"], errors="coerce").dropna().head(3)
    if values.empty:
        return None, "趋势待积累"
    change = score - float(values.mean())
    if change > 8:
        label = "赚钱效应扩散"
    elif change < -8:
        label = "赚钱效应退潮"
    else:
        label = "赚钱效应稳定"
    return round(change, 2), label


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
        median_pct = float(today["pct_chg"].median())

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
        promotion = _promotion_metrics(con, trade_date, limit_pool)
        promotion_trend = _promotion_trend(con, trade_date, days=5)
        breadth_profit_score = _weighted_available([
            (score_between(up_ratio, 0.30, 0.70), 0.60),
            (score_between(median_pct, -2.0, 2.0), 0.40),
        ])
        premium_profit_score = _weighted_available([
            (None if prev_premium is None else score_between(prev_premium, -3.0, 3.0), 0.55),
            (None if prev_positive is None else prev_positive * 100.0, 0.45),
        ])
        continuation_profit_score = _weighted_available([
            (promotion["overall"]["adjusted_rate"], 0.35),
            (promotion["rate_1to2"]["adjusted_rate"], 0.25),
            (promotion["rate_2to3"]["adjusted_rate"], 0.20),
            (promotion["rate_3to4"]["adjusted_rate"], 0.10),
            (promotion["rate_high"]["adjusted_rate"], 0.10),
        ])
        safety_profit_score = _weighted_available([
            (score_between(broken_rate, 10.0, 60.0, invert=True), 0.55),
            (emotion_score, 0.45),
        ])
        profit_effect_score = _weighted_available([
            (breadth_profit_score, 0.25),
            (premium_profit_score, 0.30),
            (continuation_profit_score, 0.25),
            (safety_profit_score, 0.20),
        ])
        profit_effect_label = _profit_effect_label(profit_effect_score)
        profit_effect_change_3d, profit_effect_trend = _profit_effect_trend(
            profit_effect_score,
            _previous_market_rows(con, trade_date, limit=3),
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
            "median_pct_chg": median_pct,
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
            "profit_effect_score": profit_effect_score,
            "profit_effect_label": profit_effect_label,
            "profit_effect_trend": profit_effect_trend,
            "profit_effect_change_3d": profit_effect_change_3d,
            "profit_breadth_score": breadth_profit_score,
            "profit_premium_score": premium_profit_score,
            "profit_continuation_score": continuation_profit_score,
            "profit_safety_score": safety_profit_score,
            "promotion_overall_rate": promotion["overall"]["rate"],
            "promotion_overall_success": promotion["overall"]["success"],
            "promotion_overall_sample": promotion["overall"]["sample"],
            "promotion_1to2_rate": promotion["rate_1to2"]["rate"],
            "promotion_1to2_success": promotion["rate_1to2"]["success"],
            "promotion_1to2_sample": promotion["rate_1to2"]["sample"],
            "promotion_2to3_rate": promotion["rate_2to3"]["rate"],
            "promotion_2to3_success": promotion["rate_2to3"]["success"],
            "promotion_2to3_sample": promotion["rate_2to3"]["sample"],
            "promotion_3to4_rate": promotion["rate_3to4"]["rate"],
            "promotion_3to4_success": promotion["rate_3to4"]["success"],
            "promotion_3to4_sample": promotion["rate_3to4"]["sample"],
            "promotion_high_rate": promotion["rate_high"]["rate"],
            "promotion_high_success": promotion["rate_high"]["success"],
            "promotion_high_sample": promotion["rate_high"]["sample"],
            "promotion_trend_score": promotion_trend["score"],
            "promotion_trend_label": promotion_trend["label"],
            "promotion_trend_slope": promotion_trend["slope"],
            "promotion_trend_sample_days": promotion_trend["sample_days"],
            "promotion_trend_json": json.dumps(promotion_trend, ensure_ascii=False),
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
            "median_pct_chg",
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
            "profit_effect_score",
            "profit_effect_change_3d",
            "profit_breadth_score",
            "profit_premium_score",
            "profit_continuation_score",
            "profit_safety_score",
            "promotion_overall_rate",
            "promotion_overall_success",
            "promotion_overall_sample",
            "promotion_1to2_rate",
            "promotion_1to2_success",
            "promotion_1to2_sample",
            "promotion_2to3_rate",
            "promotion_2to3_success",
            "promotion_2to3_sample",
            "promotion_3to4_rate",
            "promotion_3to4_success",
            "promotion_3to4_sample",
            "promotion_high_rate",
            "promotion_high_success",
            "promotion_high_sample",
            "promotion_trend_score",
            "promotion_trend_slope",
            "promotion_trend_sample_days",
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
                factor_id="mkt_profit_effect_score", raw_value=profit_effect_score,
                score=profit_effect_score, direction="higher_better",
            ),
            make_long_record(
                trade_date=trade_date, entity_type="market", entity_id="market",
                factor_id="mkt_promotion_trend_score", raw_value=promotion_trend["score"],
                score=promotion_trend["score"], direction="higher_better",
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
