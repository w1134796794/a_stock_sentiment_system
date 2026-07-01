"""Recompute score curves and screening artifacts without refetching source data."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR
from core.factors.jobs.stock_factor_job import (
    _activity_ratio_score,
    _amount_ratio_target_score,
    _new_high_position_score,
)
from core.screening.screening_engine import ScreeningEngine


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recompute saturated stock scores and screening JSON files.")
    parser.add_argument("--start", required=True, help="Start trade date YYYYMMDD")
    parser.add_argument("--end", required=True, help="End trade date YYYYMMDD")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--duckdb-path", default=str(FACTOR_DB_PATH))
    parser.add_argument("--screening-dir", default=str(WEB_DATA_DIR / "screening"))
    parser.add_argument("--skip-screening", action="store_true")
    return parser.parse_args()


def _score_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    numeric = [
        "pct_chg", "limit_pct", "vol_ratio", "amount_ratio", "new_high_ratio",
        "liquidity_score", "sector_resonance_score", "board_height",
        "board_height_score", "seal_time_score", "float_mv_fit_score",
        "signal_total_adjustment",
    ]
    for column in numeric:
        out[column] = pd.to_numeric(out.get(column), errors="coerce").fillna(0.0)

    limit_pct = out["limit_pct"].where(out["limit_pct"] > 0, 10.0)
    out["pct_score"] = ((out["pct_chg"] + limit_pct) / (2.0 * limit_pct) * 100.0).clip(0.0, 100.0)
    out["vol_ratio_score"] = out["vol_ratio"].map(_activity_ratio_score)
    out["amount_ratio_score"] = out["amount_ratio"].map(_amount_ratio_target_score)
    out["new_high_score"] = out["new_high_ratio"].map(_new_high_position_score)
    out["tech_score"] = out["pct_score"] * 0.55 + out["new_high_score"] * 0.45
    out["volume_score"] = out["vol_ratio_score"] * 0.50 + out["amount_ratio_score"] * 0.50
    raw_board = (
        out["board_height_score"] * 0.50
        + out["seal_time_score"] * 0.30
        + out["float_mv_fit_score"] * 0.20
    )
    out["board_score"] = raw_board.where(out["board_height"] > 0, 50.0)
    out["total_score"] = (
        out["tech_score"] * 0.25
        + out["volume_score"] * 0.12
        + out["liquidity_score"] * 0.13
        + out["sector_resonance_score"] * 0.30
        + out["board_score"] * 0.20
    ).clip(0.0, 100.0)
    out["enhanced_total_score"] = (out["total_score"] + out["signal_total_adjustment"]).clip(0.0, 100.0)
    out["rank"] = out.groupby("trade_date")["total_score"].rank(method="dense", ascending=False).astype(int)
    out["computed_at"] = datetime.now().isoformat(timespec="seconds")
    return out[[
        "trade_date", "code", "tech_score", "volume_score", "board_score",
        "total_score", "enhanced_total_score", "rank", "vol_ratio_score",
        "amount_ratio_score", "new_high_score", "computed_at",
    ]]


def _update_month(con, month: str, start: str, end: str) -> int:
    lower = max(start, f"{month}01")
    upper = min(end, f"{month}31")
    frame = con.execute(
        """
        SELECT CAST(trade_date AS VARCHAR) AS trade_date, CAST(code AS VARCHAR) AS code,
               pct_chg, limit_pct, vol_ratio, amount_ratio, new_high_ratio,
               liquidity_score, sector_resonance_score, board_height,
               board_height_score, seal_time_score, float_mv_fit_score,
               signal_total_adjustment
        FROM factor_stock_wide
        WHERE CAST(trade_date AS VARCHAR) BETWEEN ? AND ?
        """,
        [lower, upper],
    ).fetchdf()
    if frame.empty:
        return 0
    scored = _score_frame(frame)
    con.register("_factor_recalc_df", scored)
    con.execute("CREATE OR REPLACE TEMP TABLE _factor_recalc AS SELECT * FROM _factor_recalc_df")
    con.execute(
        """
        UPDATE factor_stock_wide AS target SET
          tech_score = source.tech_score,
          volume_score = source.volume_score,
          board_score = source.board_score,
          total_score = source.total_score,
          enhanced_total_score = source.enhanced_total_score,
          rank = source.rank,
          computed_at = source.computed_at
        FROM _factor_recalc AS source
        WHERE CAST(target.trade_date AS VARCHAR) = source.trade_date
          AND CAST(target.code AS VARCHAR) = source.code
        """
    )
    con.execute(
        """
        UPDATE factor_value_long AS target SET
          score = CASE target.factor_id
            WHEN 'stk_vol_ratio_5d' THEN source.vol_ratio_score
            WHEN 'stk_amount_ratio_5d' THEN source.amount_ratio_score
            WHEN 'stk_new_high_20d' THEN source.new_high_score
            WHEN 'stk_board_position' THEN source.board_score
            WHEN 'stk_total_score' THEN source.total_score
            ELSE target.score
          END,
          raw_value = CASE
            WHEN target.factor_id = 'stk_total_score' THEN source.total_score
            ELSE target.raw_value
          END,
          rank_value = CASE
            WHEN target.factor_id = 'stk_total_score' THEN source.rank
            ELSE target.rank_value
          END,
          computed_at = source.computed_at
        FROM _factor_recalc AS source
        WHERE target.entity_type = 'stock'
          AND target.factor_id IN (
            'stk_vol_ratio_5d', 'stk_amount_ratio_5d', 'stk_new_high_20d',
            'stk_board_position', 'stk_total_score'
          )
          AND CAST(target.trade_date AS VARCHAR) = source.trade_date
          AND CAST(target.entity_id AS VARCHAR) = source.code
        """
    )
    con.unregister("_factor_recalc_df")
    return int(len(scored))


def main() -> int:
    args = _args()
    start = str(args.start)
    end = str(args.end)
    if len(start) != 8 or len(end) != 8 or start > end:
        raise SystemExit("Date range must be YYYYMMDD and start <= end")

    import duckdb

    db_path = Path(args.duckdb_path)
    screening_dir = Path(args.screening_dir)
    with duckdb.connect(str(db_path)) as con:
        dates = [
            row[0]
            for row in con.execute(
                """
                SELECT DISTINCT CAST(trade_date AS VARCHAR) AS trade_date
                FROM factor_stock_wide
                WHERE CAST(trade_date AS VARCHAR) BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                [start, end],
            ).fetchall()
        ]
        months = sorted({date[:6] for date in dates})
        total = 0
        for index, month in enumerate(months, start=1):
            rows = _update_month(con, month, start, end)
            total += rows
            logger.info(f"[score-recompute] {index}/{len(months)} {month}: {rows} rows")

    if not args.skip_screening:
        engine = ScreeningEngine(duckdb_path=db_path, output_dir=screening_dir)
        for index, trade_date in enumerate(dates, start=1):
            result = engine.run(trade_date, profile=args.profile, persist=True)
            if not result.ok:
                logger.warning(f"[screening-recompute] {trade_date}: {result.message}")
            if index == 1 or index % 10 == 0 or index == len(dates):
                logger.info(
                    f"[screening-recompute] {index}/{len(dates)} {trade_date}: "
                    f"final={len(result.final)}"
                )

    logger.info(f"Recomputed {total} stock rows across {len(dates)} trade dates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
