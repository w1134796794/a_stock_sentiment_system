"""Stock-level batch factor job."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config.settings import CACHE_DIR
from core.factors.behavior_cycle import BEHAVIOR_STATES, stock_behavior_cycle
from core.factors.jobs.first_board_factors import (
    FIRST_BOARD_FACTOR_COLUMNS,
    build_first_board_stock_metrics,
    load_membership_map,
)
from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    now_iso,
    percentile_score,
    read_recent_trade_dates,
    read_table,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
)
from core.factors.jobs.stock_advanced import (
    crowding_metrics,
    late_seal_safety,
    relative_sector_strength,
    reseal_resilience,
    seal_quality,
    sector_rotation_metrics,
)
from core.factors.sector_taxonomy import is_trade_theme_sector
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
    """成交额确认分：温和放量最优，避免 0.8-1.5 全部封顶。"""
    v = to_float(value, 0.0)
    if v <= 0:
        return 0.0
    if v < 0.4:
        return max(0.0, v / 0.4 * 15.0)
    if v < 0.8:
        return 15.0 + (v - 0.4) / 0.4 * 60.0
    if v < 1.15:
        return 75.0 + (v - 0.8) / 0.35 * 25.0
    if v <= 1.5:
        return 100.0 - (v - 1.15) / 0.35 * 15.0
    if v <= 2.2:
        return 85.0 - (v - 1.5) / 0.7 * 40.0
    if v <= 3.0:
        return 45.0 - (v - 2.2) / 0.8 * 30.0
    return max(0.0, 15.0 - (v - 3.0) * 5.0)


def _new_high_position_score(value: float) -> float:
    """阶段位置分：接近或温和突破新高最优，过度乖离不再持续封顶。"""
    v = to_float(value, 0.0)
    if v <= 0:
        return 0.0
    if v < 0.85:
        return max(0.0, v / 0.85 * 20.0)
    if v < 0.95:
        return 20.0 + (v - 0.85) / 0.10 * 50.0
    if v < 1.02:
        return 70.0 + (v - 0.95) / 0.07 * 30.0
    if v <= 1.10:
        return 100.0 - (v - 1.02) / 0.08 * 20.0
    if v <= 1.20:
        return 80.0 - (v - 1.10) / 0.10 * 25.0
    return max(20.0, 55.0 - (v - 1.20) * 100.0)


def _stock_sector_scores(
    code: str,
    sector_scores: dict,
    cache_dir: Path = CACHE_DIR,
    memberships: list[dict[str, str]] | None = None,
) -> dict:
    """Map cached stock memberships to point-in-time sector factor scores."""
    neutral = {
        "sector_heat_score": 50.0,
        "sector_persistence_score": 50.0,
        "sector_mainline_score": 50.0,
        "sector_resonance_score": 50.0,
        "sector_flow_score": 50.0,
        "resonance_sectors": "",
        "primary_sector_code": "",
        "primary_sector_name": "",
        "sector_behavior_dominant_state": "",
        "sector_behavior_dominant_label": "",
        "matched_sector_codes": "",
        "matched_sector_names": "",
    }
    for state in BEHAVIOR_STATES:
        neutral[f"sector_behavior_{state}_score"] = 50.0
    if not sector_scores:
        return neutral
    if memberships is None:
        code6 = str(code or "").split(".")[0].zfill(6)
        membership_dir = Path(cache_dir) / "sector" / "stock_sectors"
        files = list(membership_dir.glob(f"{code6}.*.csv"))
        if not files:
            return neutral
        try:
            membership_rows = pd.read_csv(files[0]).to_dict("records")
        except Exception:
            return neutral
    else:
        membership_rows = memberships

    matched = []
    for row in membership_rows:
        sector_type = str(row.get("type") or "").strip().upper()
        if sector_type not in {"N", "I", "概念", "行业"}:
            continue
        sector_code = str(row.get("code") or row.get("ts_code") or "").split(".")[0]
        values = sector_scores.get(sector_code)
        if not values:
            continue
        momentum = to_float(values.get("momentum_score"), 50.0)
        amount = to_float(values.get("amount_score"), 50.0)
        amount_ratio = to_float(values.get("amount_ratio_score"), 50.0)
        sector_name = str(values.get("sector_name") or row.get("name") or sector_code)
        if not is_trade_theme_sector(sector_name, sector_type):
            continue
        matched.append({
            "code": sector_code,
            "name": sector_name,
            "type": sector_type,
            "heat": safe_weighted_score([(momentum, 0.55), (amount, 0.25), (amount_ratio, 0.20)]),
            "persistence": to_float(values.get("persistence_score"), 50.0),
            "mainline": to_float(values.get("mainline_score"), 50.0),
            "flow": to_float(values.get("sector_flow_score"), 50.0),
            "behavior": {
                state: to_float(values.get(f"behavior_{state}_score"), 50.0)
                for state in BEHAVIOR_STATES
            },
            "behavior_state": str(values.get("behavior_dominant_state") or ""),
            "behavior_label": str(values.get("behavior_dominant_label") or ""),
        })
    if not matched:
        return neutral

    matched.sort(key=lambda item: (item["mainline"], item["heat"]), reverse=True)
    leaders = matched[:3]
    heat = sum(item["heat"] for item in leaders) / len(leaders)
    persistence = sum(item["persistence"] for item in leaders) / len(leaders)
    mainline = sum(item["mainline"] for item in leaders) / len(leaders)
    sector_flow = sum(item["flow"] for item in leaders) / len(leaders)
    concept_best = max((item["mainline"] for item in matched if item["type"] in {"N", "概念"}), default=0.0)
    industry_best = max((item["mainline"] for item in matched if item["type"] in {"I", "行业"}), default=0.0)
    dual_resonance = min(concept_best, industry_best)
    resonance = safe_weighted_score([
        (heat, 0.30), (persistence, 0.25), (mainline, 0.35), (dual_resonance, 0.10),
    ])
    result = {
        "sector_heat_score": round(heat, 4),
        "sector_persistence_score": round(persistence, 4),
        "sector_mainline_score": round(mainline, 4),
        "sector_resonance_score": round(resonance, 4),
        "sector_flow_score": round(sector_flow, 4),
        "resonance_sectors": ",".join(item["name"] for item in leaders),
        "primary_sector_code": str(leaders[0].get("code") or ""),
        "primary_sector_name": str(leaders[0].get("name") or ""),
        "sector_behavior_dominant_state": str(leaders[0].get("behavior_state") or ""),
        "sector_behavior_dominant_label": str(leaders[0].get("behavior_label") or ""),
        "matched_sector_codes": ",".join(item["code"] for item in matched),
        "matched_sector_names": ",".join(item["name"] for item in matched),
    }
    for state in BEHAVIOR_STATES:
        result[f"sector_behavior_{state}_score"] = round(
            sum(item["behavior"][state] for item in leaders) / len(leaders), 4,
        )
    return result


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
                "amount_yuan", "open", "high", "close", "pre_close", "circ_mv",
            ),
        )
        if stock.empty:
            result.ok = False
            result.add_message("stock_daily_silver 为空，无法计算个股指标")
            return result

        stock["trade_date"] = stock["trade_date"].astype(str)
        for col in ("pct_chg", "vol_hand", "amount_yuan", "open", "high", "close", "pre_close"):
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
        today["new_high_score"] = today["new_high_ratio"].map(_new_high_position_score)
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
        membership_map = load_membership_map(list(today["code"].astype(str)))
        sector_values = []
        for _, row in today.iterrows():
            code = str(row.get("code") or "")
            memberships = membership_map.get(code.zfill(6), [])
            if memberships:
                sector_values.append(
                    _stock_sector_scores(code, sector_scores, memberships=memberships)
                )
            else:
                sector_values.append(_stock_sector_scores("", {}))
        for key in (
            "sector_heat_score", "sector_persistence_score", "sector_mainline_score",
            "sector_resonance_score", "resonance_sectors",
            "sector_flow_score", "primary_sector_code", "primary_sector_name",
            "sector_behavior_dominant_state", "sector_behavior_dominant_label",
            "matched_sector_codes", "matched_sector_names",
            *[f"sector_behavior_{state}_score" for state in BEHAVIOR_STATES],
        ):
            today[key] = [item[key] for item in sector_values]
        today["sector_mapping_available"] = (
            today["primary_sector_code"].fillna("").astype(str).str.len() > 0
        ).astype(int)

        sector_history = read_recent_trade_dates(
            con,
            "factor_sector_wide",
            trade_date,
            days=6,
            columns=(
                "trade_date", "sector_code", "momentum_score", "mainline_score",
            ),
        )
        rotation_by_sector = sector_rotation_metrics(sector_history)
        rotation_values = [
            rotation_by_sector.get(str(code).split(".")[0], {})
            for code in today["primary_sector_code"]
        ]
        today["sector_rotation_momentum_score"] = [
            item.get("score", 50.0) for item in rotation_values
        ]
        today["sector_rotation_age"] = [item.get("age", 0.0) for item in rotation_values]
        today["sector_rotation_acceleration"] = [
            item.get("acceleration", 0.0) for item in rotation_values
        ]

        limit_history = read_recent_trade_dates(
            con,
            "limit_up_pool_silver",
            trade_date,
            days=5,
            columns=(
                "trade_date", "code", "limit_times", "first_time", "last_time", "open_times",
            ),
        )
        crowding_by_code = crowding_metrics(limit_history)
        crowding_values = [crowding_by_code.get(str(code), {}) for code in today["code"].astype(str)]
        today["crowding_decay_5d_score"] = [item.get("score", 50.0) for item in crowding_values]
        today["limit_appearances_5d"] = [item.get("appearances", 0.0) for item in crowding_values]

        relative = relative_sector_strength(today)
        today["relative_strength_sector_raw"] = relative["relative_strength_sector_raw"]
        today["relative_strength_sector_score"] = relative["relative_strength_sector_score"]

        lhb_frame = read_table(
            con, "factor_lhb_stock_wide", where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)]
        )
        lhb_source_available = not lhb_frame.empty
        lhb_by_code = (
            lhb_frame.assign(code=lhb_frame["code"].astype(str).str.split(".").str[0].str.zfill(6))
            .drop_duplicates("code", keep="last").set_index("code").to_dict("index")
            if not lhb_frame.empty else {}
        )
        lhb_defaults = {
            "lhb_present": 0.0,
            "lhb_net_buy_score": 50.0,
            "institution_net_buy_score": 50.0,
            "institution_consensus_score": 50.0,
            "repeat_persistence_score": 50.0,
            "sector_lhb_resonance_score": 50.0,
            "crowding_penalty_score": 0.0,
            "lhb_composite_score": 50.0,
            "lhb_net_buy_ratio": 0.0,
            "institution_net_buy_ratio": 0.0,
            "appearance_days_5d": 0.0,
            "signal_date": "",
            "effective_date": "",
        }
        lhb_values = [lhb_by_code.get(str(code), lhb_defaults) for code in today["code"].astype(str)]
        for key, default in lhb_defaults.items():
            today[key] = [item.get(key, default) for item in lhb_values]
        today["lhb_source_available"] = int(lhb_source_available)

        signal_frame = read_table(
            con, "factor_signal_stock_wide",
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        signal_columns = [
            "code", "capital_flow_consensus_score", "capital_flow_persistence_score",
            "capital_flow_adjustment", "attention_score", "attention_crowding_penalty",
            "attention_adjustment", "leader_quality_score", "leader_adjustment",
            "margin_score", "margin_adjustment", "event_risk_score", "risk_adjustment",
            "flow_source_count", "attention_source_count", "kpl_present",
            "signal_date", "effective_date",
        ]
        if not signal_frame.empty:
            signal_frame = signal_frame[[col for col in signal_columns if col in signal_frame.columns]].copy()
            signal_frame["code"] = signal_frame["code"].astype(str).str.zfill(6)
            signal_frame = signal_frame.rename(columns={
                "signal_date": "short_signal_date", "effective_date": "short_effective_date",
            })
            today = today.merge(signal_frame.drop_duplicates("code", keep="last"), on="code", how="left")
        optional_source_tables = {
            "capital_flow_source_available": "stock_capital_flow_silver",
            "attention_source_available": "stock_attention_silver",
            "leader_source_available": "stock_leader_signal_silver",
            "margin_source_available": "stock_margin_silver",
            "event_source_available": "stock_event_silver",
        }
        for availability_column, table_name in optional_source_tables.items():
            source = read_table(
                con, table_name,
                where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
            )
            today[availability_column] = int(not source.empty)
            result.record_source(
                table_name, available=not source.empty, rows=len(source),
                freshness_date=str(trade_date), required=False,
            )
            if source.empty:
                result.disable_enhancement(availability_column.removesuffix("_source_available"))
        result.record_source(
            "factor_lhb_stock_wide", available=lhb_source_available,
            rows=len(lhb_frame), freshness_date=str(trade_date), required=False,
        )
        result.record_source(
            "sector_membership", available=bool(today["sector_mapping_available"].any()),
            rows=int(today["sector_mapping_available"].sum()),
            freshness_date=str(trade_date), required=True,
        )
        signal_defaults = {
            "capital_flow_consensus_score": 50.0, "capital_flow_persistence_score": 50.0,
            "capital_flow_adjustment": 0.0, "attention_score": 50.0,
            "attention_crowding_penalty": 0.0, "attention_adjustment": 0.0,
            "leader_quality_score": 50.0, "leader_adjustment": 0.0,
            "margin_score": 50.0, "margin_adjustment": 0.0,
            "event_risk_score": 0.0, "risk_adjustment": 0.0,
            "flow_source_count": 0.0, "attention_source_count": 0.0, "kpl_present": 0.0,
        }
        for column, default in signal_defaults.items():
            if column not in today.columns:
                today[column] = default
            else:
                today[column] = pd.to_numeric(today[column], errors="coerce").fillna(default)
        today["short_signal_date"] = today.get("short_signal_date", "")
        today["short_effective_date"] = today.get("short_effective_date", "")
        today["capital_flow_adjustment"] = (
            today["capital_flow_adjustment"]
            + (pd.to_numeric(today["sector_flow_score"], errors="coerce").fillna(50.0) - 50.0) / 50.0 * 1.5
        ).clip(-5.5, 5.5)

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
            ]) if row.board_height > 0 else 50.0
            for row in today.itertuples()
        ]
        first_board_metrics = build_first_board_stock_metrics(
            con,
            str(trade_date),
            today,
            pool_by_code,
            sector_scores,
            membership_map,
        )
        first_board_columns = (
            *FIRST_BOARD_FACTOR_COLUMNS,
            "first_board_factor_available",
            "first_board_primary_sector_code",
            "first_board_primary_sector_name",
            "first_board_sector_top20",
        )
        for column in first_board_columns:
            today[column] = [
                first_board_metrics.get(str(code).zfill(6), {}).get(column, 0.0)
                for code in today["code"].astype(str)
            ]
        # A genuine first board can outrank a plain second-board position only
        # when sector launch evidence is strong. Other board heights retain the
        # existing board score unchanged.
        first_board_mask = (
            pd.to_numeric(today["board_height"], errors="coerce").eq(1)
            & pd.to_numeric(
                today["first_board_factor_available"], errors="coerce",
            ).eq(1)
        )
        today.loc[first_board_mask, "board_score"] = (
            pd.to_numeric(today.loc[first_board_mask, "board_score"], errors="coerce").fillna(50.0) * 0.35
            + pd.to_numeric(
                today.loc[first_board_mask, "first_board_resonance_score"], errors="coerce",
            ).fillna(0.0) * 0.65
        ).clip(0.0, 100.0)
        seal_values = [
            seal_quality(pool_by_code.get(str(row.get("code") or "")) or {}, row.get("amount_yuan"))
            for _, row in today.iterrows()
        ]
        today["intraday_seal_quality_score"] = [value[0] for value in seal_values]
        today["sealed_order_amount_ratio"] = [value[1] for value in seal_values]
        prior_first_times: dict[str, list[str]] = {}
        if not limit_history.empty:
            prior_rows = limit_history[limit_history["trade_date"].astype(str) < str(trade_date)]
            for code, rows in prior_rows.groupby(prior_rows["code"].astype(str).str.zfill(6)):
                prior_first_times[str(code)] = list(rows["first_time"])
        reseal_scores = []
        late_seal_scores = []
        late_seal_delays = []
        behavior_rows = []
        for _, row in today.iterrows():
            code = str(row.get("code") or "")
            pool = pool_by_code.get(code) or {}
            reseal_score = reseal_resilience(pool)
            late_score, late_delay = late_seal_safety(pool, prior_first_times.get(code, []))
            reseal_scores.append(reseal_score)
            late_seal_scores.append(late_score)
            late_seal_delays.append(late_delay)
            pre_close = to_float(row.get("pre_close"), 0.0)
            open_gap = (
                (to_float(row.get("open"), pre_close) / pre_close - 1.0) * 100.0
                if pre_close > 0 else 0.0
            )
            behavior_rows.append(stock_behavior_cycle(
                open_gap_pct=open_gap,
                close_pct_chg=row.get("pct_chg"),
                amount_ratio=row.get("amount_ratio"),
                amount_ratio_score=row.get("amount_ratio_score"),
                relative_strength_score=row.get("relative_strength_sector_score"),
                seal_quality_score=row.get("intraday_seal_quality_score"),
                reseal_resilience_score=reseal_score,
                crowding_safety_score=row.get("crowding_decay_5d_score"),
                late_seal_safety_score=late_score,
                board_score=row.get("board_score"),
                sector_states={
                    state: row.get(f"sector_behavior_{state}_score", 50.0)
                    for state in BEHAVIOR_STATES
                },
            ))
        today["reseal_resilience_score"] = reseal_scores
        today["late_seal_safety_score"] = late_seal_scores
        today["late_seal_delay_minutes"] = late_seal_delays
        for state in BEHAVIOR_STATES:
            today[f"behavior_{state}_score"] = [item["scores"][state] for item in behavior_rows]
            today[f"behavior_{state}_probability"] = [
                item["probabilities"][state] for item in behavior_rows
            ]
        today["behavior_repair_quality_score"] = [
            item["atomic"]["repair_quality"] for item in behavior_rows
        ]
        today["behavior_divergence_resilience_score"] = [
            item["atomic"]["divergence_resilience"] for item in behavior_rows
        ]
        today["behavior_dominant_state"] = [item["dominant_state"] for item in behavior_rows]
        today["behavior_dominant_label"] = [item["dominant_label"] for item in behavior_rows]
        today["behavior_dominant_probability"] = [item["dominant_probability"] for item in behavior_rows]
        today["behavior_data_completeness"] = [item["data_completeness"] for item in behavior_rows]

        total_score = pd.Series([
            safe_weighted_score([
                (row.tech_score, 0.25),
                (row.volume_score, 0.12),
                (row.liquidity_score, 0.13),
                (row.sector_resonance_score, 0.30),
                (row.board_score, 0.20),
            ])
            for row in today.itertuples()
        ], index=today.index, dtype=float)
        signal_total_adjustment = today[[
            "capital_flow_adjustment", "attention_adjustment", "leader_adjustment",
            "margin_adjustment", "risk_adjustment",
        ]].sum(axis=1).clip(-10.0, 10.0)
        score_columns = pd.DataFrame({
            "total_score": total_score,
            "signal_total_adjustment": signal_total_adjustment,
            "enhanced_total_score": (total_score + signal_total_adjustment).clip(0, 100),
            "rank": total_score.rank(method="dense", ascending=False).astype(int),
        }, index=today.index)
        today = pd.concat([today, score_columns], axis=1).copy()

        # Keep neutral defaults during internal calculations, then persist
        # unavailable optional evidence as NULL so downstream code can
        # distinguish "not fetched" from a genuinely neutral observation.
        optional_columns = {
            "lhb_source_available": [
                "lhb_net_buy_score", "institution_net_buy_score",
                "institution_consensus_score", "repeat_persistence_score",
                "sector_lhb_resonance_score", "lhb_composite_score",
                "crowding_penalty_score",
            ],
            "capital_flow_source_available": [
                "capital_flow_consensus_score", "capital_flow_persistence_score",
            ],
            "attention_source_available": ["attention_score", "attention_crowding_penalty"],
            "leader_source_available": ["leader_quality_score"],
            "margin_source_available": ["margin_score"],
            "event_source_available": ["event_risk_score"],
        }
        for availability_column, columns in optional_columns.items():
            if not bool(pd.to_numeric(today[availability_column], errors="coerce").fillna(0).max()):
                for column in columns:
                    today[column] = pd.NA

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
            "sector_flow_score",
            "resonance_sectors",
            "primary_sector_code",
            "primary_sector_name",
            "sector_mapping_available",
            "sector_behavior_dominant_state",
            "sector_behavior_dominant_label",
            "sector_behavior_attention_score",
            "sector_behavior_acceleration_score",
            "sector_behavior_divergence_score",
            "sector_behavior_repair_score",
            "sector_behavior_decay_score",
            "sector_rotation_momentum_score",
            "sector_rotation_age",
            "sector_rotation_acceleration",
            "crowding_decay_5d_score",
            "limit_appearances_5d",
            "relative_strength_sector_raw",
            "relative_strength_sector_score",
            "board_score",
            "board_height",
            "board_height_score",
            "seal_time_score",
            "intraday_seal_quality_score",
            "sealed_order_amount_ratio",
            "reseal_resilience_score",
            "late_seal_safety_score",
            "late_seal_delay_minutes",
            "behavior_attention_score",
            "behavior_acceleration_score",
            "behavior_divergence_score",
            "behavior_repair_score",
            "behavior_decay_score",
            "behavior_attention_probability",
            "behavior_acceleration_probability",
            "behavior_divergence_probability",
            "behavior_repair_probability",
            "behavior_decay_probability",
            "behavior_repair_quality_score",
            "behavior_divergence_resilience_score",
            "behavior_dominant_state",
            "behavior_dominant_label",
            "behavior_dominant_probability",
            "behavior_data_completeness",
            "float_mv",
            "float_mv_fit_score",
            "first_board_sector_sync_score",
            "first_board_leadership_score",
            "first_board_sector_pioneer_score",
            "first_board_breadth_score",
            "first_board_amount_surge_score",
            "first_board_new_theme_score",
            "first_board_resonance_score",
            "first_board_factor_available",
            "first_board_primary_sector_code",
            "first_board_primary_sector_name",
            "first_board_sector_top20",
            "lhb_present",
            "lhb_source_available",
            "lhb_net_buy_score",
            "institution_net_buy_score",
            "institution_consensus_score",
            "repeat_persistence_score",
            "sector_lhb_resonance_score",
            "crowding_penalty_score",
            "lhb_composite_score",
            "lhb_net_buy_ratio",
            "institution_net_buy_ratio",
            "appearance_days_5d",
            "signal_date",
            "effective_date",
            "short_signal_date",
            "short_effective_date",
            "capital_flow_consensus_score",
            "capital_flow_persistence_score",
            "capital_flow_adjustment",
            "attention_score",
            "attention_crowding_penalty",
            "attention_adjustment",
            "leader_quality_score",
            "leader_adjustment",
            "margin_score",
            "margin_adjustment",
            "event_risk_score",
            "risk_adjustment",
            "flow_source_count",
            "attention_source_count",
            "kpl_present",
            "capital_flow_source_available",
            "attention_source_available",
            "leader_source_available",
            "margin_source_available",
            "event_source_available",
            "signal_total_adjustment",
            "enhanced_total_score",
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
                    factor_id="stk_sector_heat_score", raw_value=(
                        row["sector_heat_score"] if row["sector_mapping_available"] else None
                    ),
                    score=row["sector_heat_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_persistence_score", raw_value=(
                        row["sector_persistence_score"] if row["sector_mapping_available"] else None
                    ),
                    score=row["sector_persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_mainline_score", raw_value=(
                        row["sector_mainline_score"] if row["sector_mapping_available"] else None
                    ),
                    score=row["sector_mainline_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_resonance_score", raw_value=(
                        row["sector_resonance_score"] if row["sector_mapping_available"] else None
                    ),
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
                    factor_id="stk_intraday_seal_quality", raw_value=row["sealed_order_amount_ratio"],
                    score=row["intraday_seal_quality_score"], direction="higher_better",
                ),
                *[
                    make_long_record(
                        trade_date=trade_date,
                        entity_type="stock",
                        entity_id=entity_id,
                        factor_id=f"stk_{column}",
                        raw_value=(row[column] if row["first_board_factor_available"] else None),
                        score=row[column],
                        direction="higher_better",
                    )
                    for column in FIRST_BOARD_FACTOR_COLUMNS
                ],
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_sector_rotation_momentum", raw_value=row["sector_rotation_age"],
                    score=row["sector_rotation_momentum_score"], direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_crowding_decay_5d", raw_value=row["limit_appearances_5d"],
                    score=row["crowding_decay_5d_score"], direction="target_range",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_relative_strength_sector", raw_value=row["relative_strength_sector_raw"],
                    score=row["relative_strength_sector_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_net_buy_score", raw_value=(
                        row["lhb_net_buy_ratio"] if row["lhb_source_available"] else None
                    ),
                    score=row["lhb_net_buy_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_institution_score", raw_value=(
                        row["institution_net_buy_ratio"] if row["lhb_source_available"] else None
                    ),
                    score=row["institution_net_buy_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_institution_consensus", raw_value=(
                        row["institution_consensus_score"] if row["lhb_source_available"] else None
                    ),
                    score=row["institution_consensus_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_repeat_persistence", raw_value=(
                        row["appearance_days_5d"] if row["lhb_source_available"] else None
                    ),
                    score=row["repeat_persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_sector_resonance", raw_value=(
                        row["sector_lhb_resonance_score"] if row["lhb_source_available"] else None
                    ),
                    score=row["sector_lhb_resonance_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_composite_score", raw_value=(
                        row["lhb_composite_score"] if row["lhb_source_available"] else None
                    ),
                    score=row["lhb_composite_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_lhb_crowding_risk", raw_value=(
                        row["crowding_penalty_score"] if row["lhb_source_available"] else None
                    ),
                    score=100.0 - to_float(row["crowding_penalty_score"]), direction="lower_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_capital_flow_consensus", raw_value=(
                        row["capital_flow_adjustment"] if row["capital_flow_source_available"] else None
                    ),
                    score=row["capital_flow_consensus_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_capital_flow_persistence", raw_value=(
                        row["capital_flow_persistence_score"] if row["capital_flow_source_available"] else None
                    ),
                    score=row["capital_flow_persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_attention_consensus", raw_value=(
                        row["attention_source_count"] if row["attention_source_available"] else None
                    ),
                    score=row["attention_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_attention_crowding_risk", raw_value=(
                        row["attention_crowding_penalty"] if row["attention_source_available"] else None
                    ),
                    score=100.0 - to_float(row["attention_crowding_penalty"]) * 10.0,
                    direction="lower_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_kpl_leader_quality", raw_value=(
                        row["kpl_present"] if row["leader_source_available"] else None
                    ),
                    score=row["leader_quality_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_margin_acceleration", raw_value=(
                        row["margin_adjustment"] if row["margin_source_available"] else None
                    ),
                    score=row["margin_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_block_trade_risk", raw_value=(
                        row["event_risk_score"] if row["event_source_available"] else None
                    ),
                    score=100.0 - to_float(row["event_risk_score"]), direction="lower_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id="stk_total_score", raw_value=row["total_score"], score=row["total_score"],
                    rank_value=row["rank"], direction="higher_better",
                ),
            ])
            for state in BEHAVIOR_STATES:
                records.append(make_long_record(
                    trade_date=trade_date, entity_type="stock", entity_id=entity_id,
                    factor_id=f"stk_behavior_{state}",
                    raw_value=row[f"behavior_{state}_probability"],
                    score=row[f"behavior_{state}_score"],
                    direction="lower_better" if state == "decay" else "higher_better",
                ))
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
