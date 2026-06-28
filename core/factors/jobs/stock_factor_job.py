"""Stock-level batch factor job."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import CACHE_DIR

from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    percentile_score,
    read_recent_trade_dates,
    read_table,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
    now_iso,
)
from core.utils.price_limit import get_price_limit_pct_points, limit_progress


# ---------------------------------------------------------------------------
# 打板身位（board）子类评分 —— 连板高度 / 封板时间 / 流通市值适配
#
# 设计：这些维度只对「当日涨停」的票有意义，非涨停票给中性 50 分（不污染排序）。
# 评分均为非单调：连板高度在加速期最优、过高衰减；流通市值偏中小盘弹性更好。
# ---------------------------------------------------------------------------

# 连板高度 -> 梯队分（非单调）：首板偏强、二板加速最优，>=6 高位风险衰减
_BOARD_HEIGHT_TABLE = {0: 50.0, 1: 70.0, 2: 90.0, 3: 85.0, 4: 70.0, 5: 55.0}


def _board_height_score(boards: float) -> float:
    n = int(to_float(boards, 0))
    if n <= 0:
        return 50.0
    return _BOARD_HEIGHT_TABLE.get(n, 35.0)


def _seal_time_score(first_time: str, open_times: float) -> float:
    """封板时间质量分：首封越早越强；每次炸板扣分。非涨停（无封板时间）= 中性 50。"""
    ft = str(first_time or "").strip()
    if not ft or ft in ("0", "0.0", "nan", "None", "00:00:00"):
        return 50.0
    if ft <= "09:35:00":
        base = 95.0
    elif ft <= "10:00:00":
        base = 82.0
    elif ft <= "10:30:00":
        base = 68.0
    elif ft <= "11:30:00":
        base = 52.0
    elif ft <= "14:00:00":
        base = 38.0
    else:
        base = 22.0
    base -= min(int(to_float(open_times, 0)), 4) * 6.0
    return max(0.0, min(100.0, base))


def _float_mv_fit_score(float_mv_wan: float) -> float:
    """流通市值适配分：输入按 Tushare 习惯为万元，换算亿元后做区间打分。

    中小盘弹性更优（理想 ~20-80 亿）；过小有流动性风险、过大弹性差，两端衰减。
    缺失（非涨停票无该字段）返回中性 50。
    """
    mv_wan = to_float(float_mv_wan, 0.0)
    if mv_wan <= 0:
        return 50.0
    yi = mv_wan / 10000.0
    if yi < 5:
        return 45.0
    if yi < 20:
        return 60.0 + (yi - 5) / 15.0 * 30.0
    if yi <= 80:
        return 90.0
    if yi <= 200:
        return 90.0 - (yi - 80) / 120.0 * 35.0
    if yi <= 500:
        return 55.0 - (yi - 200) / 300.0 * 25.0
    return 30.0


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


def _amount_ratio_target_score(value: float) -> float:
    """成交额确认分：0.8-1.5 倍最优，过弱和过热均惩罚。"""
    v = to_float(value, 0.0)
    if v <= 0:
        return 0.0
    if v < 0.4:
        return max(0.0, v / 0.4 * 20.0)
    if v < 0.8:
        return 20.0 + (v - 0.4) / 0.4 * 60.0
    if v <= 1.5:
        return 100.0
    if v <= 2.2:
        return 100.0 - (v - 1.5) / 0.7 * 45.0
    if v <= 3.0:
        return 55.0 - (v - 2.2) / 0.8 * 35.0
    return max(0.0, 20.0 - (v - 3.0) * 5.0)


def _stock_sector_scores(code: str, sector_scores: dict, cache_dir: Path = CACHE_DIR) -> dict:
    """Map cached stock memberships to point-in-time sector factor scores."""
    neutral = {
        "sector_heat_score": 50.0,
        "sector_persistence_score": 50.0,
        "sector_mainline_score": 50.0,
        "sector_resonance_score": 50.0,
        "resonance_sectors": "",
    }
    if not sector_scores:
        return neutral
    code6 = str(code or "").split(".")[0].zfill(6)
    membership_dir = Path(cache_dir) / "sector" / "stock_sectors"
    files = list(membership_dir.glob(f"{code6}.*.csv"))
    if not files:
        return neutral
    try:
        memberships = pd.read_csv(files[0])
    except Exception:
        return neutral

    matched = []
    for row in memberships.to_dict("records"):
        sector_type = str(row.get("type") or "").strip().upper()
        if sector_type not in {"N", "I", "概念", "行业"}:
            continue
        sector_code = str(row.get("ts_code") or "").split(".")[0]
        values = sector_scores.get(sector_code)
        if not values:
            continue
        momentum = to_float(values.get("momentum_score"), 50.0)
        amount = to_float(values.get("amount_score"), 50.0)
        amount_ratio = to_float(values.get("amount_ratio_score"), 50.0)
        matched.append({
            "name": str(values.get("sector_name") or row.get("name") or sector_code),
            "type": sector_type,
            "heat": safe_weighted_score([(momentum, 0.55), (amount, 0.25), (amount_ratio, 0.20)]),
            "persistence": to_float(values.get("persistence_score"), 50.0),
            "mainline": to_float(values.get("mainline_score"), 50.0),
        })
    if not matched:
        return neutral

    matched.sort(key=lambda item: (item["mainline"], item["heat"]), reverse=True)
    leaders = matched[:3]
    heat = sum(item["heat"] for item in leaders) / len(leaders)
    persistence = sum(item["persistence"] for item in leaders) / len(leaders)
    mainline = sum(item["mainline"] for item in leaders) / len(leaders)
    concept_best = max((item["mainline"] for item in matched if item["type"] in {"N", "概念"}), default=0.0)
    industry_best = max((item["mainline"] for item in matched if item["type"] in {"I", "行业"}), default=0.0)
    dual_resonance = min(concept_best, industry_best)
    resonance = safe_weighted_score([
        (heat, 0.30), (persistence, 0.25), (mainline, 0.35), (dual_resonance, 0.10),
    ])
    return {
        "sector_heat_score": round(heat, 4),
        "sector_persistence_score": round(persistence, 4),
        "sector_mainline_score": round(mainline, 4),
        "sector_resonance_score": round(resonance, 4),
        "resonance_sectors": ",".join(item["name"] for item in leaders),
    }


class StockFactorJob:
    name = "stock_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        stock = read_recent_trade_dates(
            con,
            "stock_daily_silver",
            trade_date,
            days=21,
            columns=(
                "trade_date", "code", "ts_code", "name", "pct_chg", "vol_hand",
                "amount_yuan", "high", "close", "pre_close", "circ_mv",
            ),
        )
        if stock.empty:
            result.ok = False
            result.add_message("stock_daily_silver 为空，无法计算个股指标")
            return result

        stock["trade_date"] = stock["trade_date"].astype(str)
        for col in ("pct_chg", "vol_hand", "amount_yuan", "high", "close", "pre_close"):
            stock[col] = pd.to_numeric(stock.get(col), errors="coerce").fillna(0)
        stock = stock.sort_values(["code", "trade_date"])
        today = stock[stock["trade_date"] == str(trade_date)].copy()
        if today.empty:
            result.ok = False
            result.add_message(f"stock_daily_silver 无 {trade_date} 数据")
            return result

        hist = stock[stock["trade_date"] < str(trade_date)].copy()

        limit_pool = read_table(con, "limit_up_pool_silver", where="trade_date = ?", params=[str(trade_date)])
        pool_by_code: dict = {}
        if not limit_pool.empty and "code" in limit_pool.columns:
            limit_pool["code"] = limit_pool["code"].astype(str)
            pool_by_code = limit_pool.drop_duplicates("code", keep="last").set_index("code").to_dict("index")

        amount_hist = hist[["trade_date", "code", "amount_yuan"]].copy() if not hist.empty else pd.DataFrame()
        vol_hist = hist[["trade_date", "code", "vol_hand"]].copy() if not hist.empty else pd.DataFrame()
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

        today["limit_pct"] = [
            get_price_limit_pct_points(row.get("code"), row.get("name"), row.get("pre_close")) or 10.0
            for _, row in today.iterrows()
        ]
        today["limit_progress"] = [
            limit_progress(row.get("pct_chg"), row.get("code"), row.get("name"), row.get("pre_close"))
            for _, row in today.iterrows()
        ]
        today["limit_progress_score"] = today["limit_progress"].map(lambda v: score_between(v, -1.0, 1.0))
        today["pct_score"] = [
            score_between(row.get("pct_chg"), -float(row.get("limit_pct") or 10.0), float(row.get("limit_pct") or 10.0))
            for _, row in today.iterrows()
        ]
        today["vol_ratio"] = vol_ratio
        today["amount_ratio"] = amount_ratio
        today["new_high_ratio"] = new_high_ratio
        today["vol_ratio_score"] = today["vol_ratio"].map(_activity_ratio_score)
        today["amount_ratio_score"] = today["amount_ratio"].map(_amount_ratio_target_score)
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
        sector_frame = read_table(
            con, "factor_sector_wide", where="trade_date = ?", params=[str(trade_date)]
        )
        sector_scores = {
            str(row.get("sector_code") or "").split(".")[0]: row
            for row in sector_frame.to_dict("records")
        } if not sector_frame.empty else {}
        membership_dir = Path(CACHE_DIR) / "sector" / "stock_sectors"
        cached_sector_codes = {
            path.name.split(".")[0]
            for path in membership_dir.glob("*.csv")
        } if membership_dir.exists() else set()
        sector_values = []
        for _, row in today.iterrows():
            code = str(row.get("code") or "")
            if code in cached_sector_codes:
                sector_values.append(_stock_sector_scores(code, sector_scores))
            else:
                sector_values.append(_stock_sector_scores("", {}))
        for key in (
            "sector_heat_score", "sector_persistence_score", "sector_mainline_score",
            "sector_resonance_score", "resonance_sectors",
        ):
            today[key] = [item[key] for item in sector_values]

        board_height = []
        seal_time_score = []
        float_mv_fit_score = []
        float_mv_vals = []
        for _, row in today.iterrows():
            code = str(row.get("code") or "")
            pool = pool_by_code.get(code) or {}
            pool_boards = to_float(pool.get("limit_times"), 0) if pool else 0
            boards = pool_boards if pool_boards > 0 else 0
            board_height.append(boards)
            seal_time_score.append(_seal_time_score(pool.get("first_time"), pool.get("open_times")))
            # 流通市值（万元）：优先用全市场覆盖的 daily_basic circ_mv，缺失再回退涨停池 float_mv
            circ_mv = to_float(row.get("circ_mv"), 0)
            fmv = circ_mv if circ_mv > 0 else (to_float(pool.get("float_mv"), 0) if pool else 0.0)
            float_mv_vals.append(fmv)
            float_mv_fit_score.append(_float_mv_fit_score(fmv))
        today["board_height"] = board_height
        today["board_height_score"] = [_board_height_score(b) for b in board_height]
        today["seal_time_score"] = seal_time_score
        today["float_mv"] = float_mv_vals
        today["float_mv_fit_score"] = float_mv_fit_score
        today["board_score"] = [
            safe_weighted_score([
                (row.board_height_score, 0.50),
                (row.seal_time_score, 0.30),
                (row.float_mv_fit_score, 0.20),
            ])
            for row in today.itertuples()
        ]

        today["total_score"] = [
            safe_weighted_score([
                (row.tech_score, 0.25),
                (row.volume_score, 0.12),
                (row.liquidity_score, 0.13),
                (row.sector_resonance_score, 0.30),
                (row.board_score, 0.20),
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
            "sector_heat_score",
            "sector_persistence_score",
            "sector_mainline_score",
            "sector_resonance_score",
            "resonance_sectors",
            "board_score",
            "board_height",
            "board_height_score",
            "seal_time_score",
            "float_mv",
            "float_mv_fit_score",
            "total_score",
            "rank",
            "pct_chg",
            "vol_ratio",
            "amount_ratio",
            "new_high_ratio",
            "limit_pct",
            "limit_progress",
            "limit_progress_score",
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
                    factor_id="stk_limit_progress", raw_value=row["limit_progress"], score=row["limit_progress_score"],
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
                    factor_id="stk_sector_heat_score", raw_value=row["sector_heat_score"],
                    score=row["sector_heat_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_persistence_score", raw_value=row["sector_persistence_score"],
                    score=row["sector_persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_mainline_score", raw_value=row["sector_mainline_score"],
                    score=row["sector_mainline_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_resonance_score", raw_value=row["sector_resonance_score"],
                    score=row["sector_resonance_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_board_height", raw_value=row["board_height"], score=row["board_height_score"],
                    direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_seal_time_quality", raw_value=row["board_height"], score=row["seal_time_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_float_mv_fit", raw_value=row["float_mv"], score=row["float_mv_fit_score"],
                    direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_board_position", raw_value=row["board_height"], score=row["board_score"],
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
