"""概览页数据：健康检查 + 关键产物统计。仅用轻量依赖（标准库 + 配置/快照读取）。"""
from __future__ import annotations

import csv
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from config.settings import (
    BASE_DIR,
    FACTOR_DB_PATH,
    OUTPUT_DIR,
    SNAPSHOT_DIR,
    TUSHARE_TOKEN,
    WEB_DATA_DIR,
)
from snapshot.reader import SnapshotReader

LOG_PATH = Path(BASE_DIR) / "logs" / "system.log"
INDEX_LABELS = {
    "000001.SH": "上证",
    "399001.SZ": "深证",
    "399006.SZ": "创业板",
    "000688.SH": "科创50",
    "899050.BJ": "北证",
}


def _ok(passed: bool) -> str:
    return "ok" if passed else "warn"


def health_items() -> List[Dict[str, Any]]:
    """返回若干健康检查项，供概览页渲染（状态 ok / warn）。"""
    reader = SnapshotReader(SNAPSHOT_DIR)
    dates = reader.list_dates()
    latest = reader.latest()

    token_ok = bool((TUSHARE_TOKEN or "").strip())
    items: List[Dict[str, Any]] = [
        {
            "title": "Tushare Token",
            "status": _ok(token_ok),
            "value": "已配置" if token_ok else "未配置",
            "detail": "数据更新需要 Tushare 历史数据接口；可在「参数配置」或 .env 中设置 TUSHARE_TOKEN。",
            "badge": "正常" if token_ok else "缺失",
        },
        {
            "title": "最新数据快照",
            "status": _ok(latest is not None),
            "value": (f"{latest}（共 {len(dates)} 天）" if latest else "暂无快照"),
            "detail": f"快照目录：{SNAPSHOT_DIR}",
            "badge": "已生成" if latest else "缺失",
        },
        {
            "title": "指标仓库",
            "status": _ok(Path(FACTOR_DB_PATH).exists()),
            "value": str(FACTOR_DB_PATH),
            "detail": "行情、指标和筛选结果依赖此 DuckDB 文件。",
            "badge": "已就绪" if Path(FACTOR_DB_PATH).exists() else "缺失",
        },
        {
            "title": "数据目录",
            "status": _ok(Path(WEB_DATA_DIR).exists()),
            "value": str(WEB_DATA_DIR),
            "detail": f"数据产物：{WEB_DATA_DIR} · 兼容输出：{OUTPUT_DIR}",
            "badge": "存在" if Path(WEB_DATA_DIR).exists() else "缺失",
        },
    ]
    return items


def etl_artifacts(date: str | None = None) -> Dict[str, Any]:
    """Return the current data artifact status for the run page and APIs."""
    reader = SnapshotReader(SNAPSHOT_DIR)
    target = str(date or reader.latest() or "")
    quality = Path(WEB_DATA_DIR) / "etl_quality" / f"quality_{target}.json" if target else None
    screening = Path(WEB_DATA_DIR) / "screening" / f"screening_{target}.json" if target else None
    analysis = Path(WEB_DATA_DIR) / "screening" / f"analysis_{target}.json" if target else None
    snapshot = Path(SNAPSHOT_DIR) / f"{target}.json" if target else None

    def _item(label: str, path: Path | None) -> Dict[str, Any]:
        exists = bool(path and path.exists())
        return {
            "label": label,
            "ok": exists,
            "path": str(path) if path else "",
            "updated_at": (
                datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                if exists and path is not None else ""
            ),
        }

    return {
        "date": target,
        "items": [
            _item("DuckDB 指标仓库", Path(FACTOR_DB_PATH)),
            _item("数据质量报告", quality),
            _item("候选池", screening),
            _item("分析摘要", analysis),
            _item("页面快照", snapshot),
        ],
    }


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize_stock_code(code: Any) -> str:
    text = str(code or "").strip().upper()
    if "." in text:
        text = text.split(".")[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:6]


def _position_range(score: float) -> str:
    if score >= 70:
        return "60-80%"
    if score >= 50:
        return "40-60%"
    if score >= 35:
        return "20-40%"
    return "0-20%"


def _trend_state(score: float) -> str:
    if score >= 60:
        return "多头"
    if score <= 45:
        return "空头"
    return "震荡"


def _table_exists(con: Any, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()[0])
    except Exception:
        return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_amount_yuan(value: Any) -> str:
    amount = _safe_float(value)
    if amount <= 0:
        return ""
    if amount >= 1e12:
        return f"{amount / 1e12:.2f}万亿"
    if amount >= 1e8:
        return f"{amount / 1e8:.0f}亿"
    return f"{amount:,.0f}"


def _all_daily_amount_yuan(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = set(reader.fieldnames or [])
            if "amount_yuan" in fields:
                col, multiplier = "amount_yuan", 1.0
            elif "成交额" in fields:
                col, multiplier = "成交额", 1.0
            elif "amount" in fields:
                # Tushare daily.amount is in thousand yuan.
                col, multiplier = "amount", 1000.0
            else:
                return 0.0
            for row in reader:
                total += _safe_float(row.get(col)) * multiplier
    except Exception:
        return 0.0
    return total


@lru_cache(maxsize=64)
def _market_amount_overlay(date: str) -> Dict[str, Any]:
    base = Path(BASE_DIR) / "data" / "cache" / "stock" / "all_daily"
    if not date or not base.exists():
        return {}
    files = sorted(
        (p for p in base.glob("*.csv") if p.stem.isdigit() and p.stem <= str(date)),
        key=lambda p: p.stem,
    )
    current = next((p for p in files if p.stem == str(date)), None)
    if current is None:
        return {}
    amount_today = _all_daily_amount_yuan(current)
    if amount_today <= 0:
        return {}
    prev_files = [p for p in files if p.stem < str(date)]
    prev = prev_files[-1] if prev_files else None
    amount_prev = _all_daily_amount_yuan(prev) if prev else 0.0
    ratio_prev = amount_today / amount_prev if amount_prev > 0 else None
    return {
        "amount_total": amount_today,
        "amount_text": _format_amount_yuan(amount_today),
        "amount_prev_date": prev.stem if prev else "",
        "amount_ratio_prev": ratio_prev,
        "amount_source": str(current),
    }


def _read_limit_pool_count(path: Path, limit_type: str) -> int | None:
    if not path.exists():
        return None
    try:
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = set(reader.fieldnames or [])
            for row in reader:
                if "limit" in fields and str(row.get("limit") or "").strip().upper() != limit_type:
                    continue
                count += 1
        return count
    except Exception:
        return None


def _fetch_and_cache_limit_pool(date: str, limit_type: str) -> int | None:
    token = (TUSHARE_TOKEN or "").strip()
    if not token or token == "your_tushare_token_here":
        return None
    try:
        import tushare as ts  # type: ignore

        pro = ts.pro_api(token)
        try:
            df = pro.limit_list_d(trade_date=str(date), limit_type=limit_type)
        except TypeError:
            df = pro.limit_list_d(trade_date=str(date))
        if df is None or df.empty:
            count = 0
        else:
            if "limit" in df.columns:
                df = df[df["limit"].astype(str).str.upper() == limit_type].copy()
            count = int(len(df))
        folder = Path(BASE_DIR) / "data" / "cache" / "market" / ("limit_up" if limit_type == "U" else "limit_down")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{date}.csv"
        if df is not None:
            df.to_csv(path, index=False)
        return count
    except Exception:
        return None


@lru_cache(maxsize=64)
def _strict_limit_counts(date: str) -> Dict[str, Any]:
    if not date:
        return {}
    base = Path(BASE_DIR) / "data" / "cache" / "market"
    up_path = base / "limit_up" / f"{date}.csv"
    down_path = base / "limit_down" / f"{date}.csv"
    up_count = _read_limit_pool_count(up_path, "U")
    down_count = _read_limit_pool_count(down_path, "D")
    if up_count is None:
        up_count = _fetch_and_cache_limit_pool(date, "U")
    if down_count is None:
        down_count = _fetch_and_cache_limit_pool(date, "D")
    out: Dict[str, Any] = {"limit_source": "tushare_limit_list_d"}
    if up_count is not None:
        out["limit_up"] = int(up_count)
    if down_count is not None:
        out["limit_down"] = int(down_count)
    return out


def _limitup_cache_overlay(date: str) -> Dict[str, Any]:
    path = Path(BASE_DIR) / "data" / "cache" / "summary" / "limit_up_stocks.csv"
    if not date or not path.exists():
        return {}
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("trade_date") or "").strip() != str(date):
                    continue
                rows.append(row)
    except Exception:
        return {}
    if not rows:
        return {}

    def board(row: Dict[str, Any]) -> int:
        return int(_safe_float(row.get("连板数"), 1.0) or 1)

    def market_cap(row: Dict[str, Any]) -> float:
        return _safe_float(row.get("流通市值"))

    def code(row: Dict[str, Any]) -> str:
        return _normalize_stock_code(row.get("代码"))

    def stats(bucket: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not bucket:
            return {
                "limit_up_count": 0,
                "limit_down_count": None,
                "max_board_height": 0,
                "continuous_count": 0,
                "broken_rate": None,
            }
        blasted = sum(1 for r in bucket if _safe_float(r.get("炸板次数")) > 0)
        return {
            "limit_up_count": len(bucket),
            "limit_down_count": None,
            "max_board_height": max(board(r) for r in bucket),
            "continuous_count": sum(1 for r in bucket if board(r) >= 2),
            "broken_rate": round(blasted / len(bucket) * 100, 2),
        }

    small = [r for r in rows if market_cap(r) and market_cap(r) < 1e10]
    mid = [r for r in rows if 1e10 <= market_cap(r) < 5e10]
    large = [r for r in rows if market_cap(r) >= 5e10]
    if not (small or mid or large):
        ranked = sorted(rows, key=market_cap)
        n = len(ranked)
        small = ranked[: int(n * 0.6)]
        mid = ranked[int(n * 0.6): int(n * 0.9)]
        large = ranked[int(n * 0.9):]

    current_board = {code(r): board(r) for r in rows if code(r)}
    promotion = {"overall": None, "rate_1to2": None, "rate_2to3": None, "rate_high": None}
    try:
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                d = str(row.get("trade_date") or "").strip()
                if d and d < str(date):
                    by_date.setdefault(d, []).append(row)
        prev_dates = sorted(by_date)
        if prev_dates:
            prev_rows = by_date[prev_dates[-1]]
            prev_board = {code(r): board(r) for r in prev_rows if code(r)}

            def rate(prev_level: int | None = None, high: bool = False) -> float | None:
                base = [
                    (c, b) for c, b in prev_board.items()
                    if (high and b >= 3) or (prev_level is not None and b == prev_level)
                ]
                if not base:
                    return None
                promoted = sum(1 for c, b in base if current_board.get(c, 0) >= b + 1)
                return round(promoted / len(base) * 100, 2)

            base_all = list(prev_board.items())
            promoted_all = sum(1 for c, b in base_all if current_board.get(c, 0) >= b + 1)
            promotion = {
                "overall": round(promoted_all / len(base_all) * 100, 2) if base_all else None,
                "rate_1to2": rate(1),
                "rate_2to3": rate(2),
                "rate_high": rate(high=True),
            }
    except Exception:
        pass

    blasted_all = sum(1 for r in rows if _safe_float(r.get("炸板次数")) > 0)
    continuous = sum(1 for r in rows if board(r) >= 2)
    return {
        "limit_up": len(rows),
        "max_board": max(board(r) for r in rows),
        "broken_rate": round(blasted_all / len(rows) * 100, 2),
        "continuous_rate": round(continuous / len(rows) * 100, 2),
        "cohorts": {
            "small": stats(small),
            "mid": stats(mid),
            "large": stats(large),
        },
        "promotion": promotion,
    }


def _etl_market_overlay(date: str) -> Dict[str, Any]:
    if not date or not Path(FACTOR_DB_PATH).exists():
        return {}
    try:
        import duckdb  # type: ignore

        out: Dict[str, Any] = {}
        with duckdb.connect(str(FACTOR_DB_PATH), read_only=True) as con:
            if _table_exists(con, "factor_market_wide"):
                market_df = con.execute(
                    """
                    SELECT *
                    FROM factor_market_wide
                    WHERE trade_date = ?
                    LIMIT 1
                    """,
                    [str(date)],
                ).fetchdf()
                if market_df is not None and not market_df.empty:
                    out["market_row"] = market_df.to_dict(orient="records")[0]

            if _table_exists(con, "index_daily_silver"):
                idx_df = con.execute(
                    """
                    SELECT index_code, index_name, close, pct_chg
                    FROM index_daily_silver
                    WHERE trade_date = ?
                    """,
                    [str(date)],
                ).fetchdf()
                if idx_df is not None and not idx_df.empty:
                    order = {code: i for i, code in enumerate(INDEX_LABELS.keys())}
                    rows = []
                    for _, r in idx_df.iterrows():
                        code = str(r.get("index_code") or "")
                        rows.append({
                            "name": INDEX_LABELS.get(code) or str(r.get("index_name") or code),
                            "close": round(_f(r.get("close")), 2),
                            "pct": round(_f(r.get("pct_chg")), 2),
                            "_order": order.get(code, 99),
                        })
                    rows.sort(key=lambda x: x["_order"])
                    for row in rows:
                        row.pop("_order", None)
                    out["indices"] = rows

            if _table_exists(con, "stock_daily_silver"):
                today = con.execute(
                    """
                    SELECT trade_date, code, ts_code, name, pct_chg, amount_yuan
                    FROM stock_daily_silver
                    WHERE trade_date = ?
                    """,
                    [str(date)],
                ).fetchdf()
                if today is not None and not today.empty:
                    today["pct_chg"] = today["pct_chg"].fillna(0).astype(float)
                    today["amount_yuan"] = today["amount_yuan"].fillna(0).astype(float)
                    total = max(len(today), 1)
                    out.update({
                        "up_count": int((today["pct_chg"] > 0).sum()),
                        "down_count": int((today["pct_chg"] < 0).sum()),
                        "flat_count": int((today["pct_chg"] == 0).sum()),
                        "up_ratio": round(float((today["pct_chg"] > 0).sum()) / total * 100, 1),
                        "amount_total": float(today["amount_yuan"].sum()),
                    })
        amount_overlay = _market_amount_overlay(date)
        if amount_overlay:
            out.update(amount_overlay)
        cache_overlay = _limitup_cache_overlay(date)
        if cache_overlay:
            out.update(cache_overlay)
        strict_counts = _strict_limit_counts(date)
        if strict_counts:
            out.update(strict_counts)
        return out
    except Exception:
        return {}


def market_overview(reader: SnapshotReader) -> Dict[str, Any]:
    """从最新快照的 market 块提取大盘速览：指数涨跌 / 涨跌停 / 涨跌家数 / 量能 / 情绪周期 / 综合趋势。"""
    snap = reader.load_latest() or {}
    m = snap.get("market") or {}
    if not m:
        return {"available": False}

    env = m.get("env") or {}
    metrics = m.get("metrics") or {}
    phase = m.get("phase") or {}
    phase_model = m.get("phase_model") or {}
    is_etl = ((snap.get("meta") or {}).get("engine") == "etl") or env.get("engine") == "etl_gold"
    trade_plans = ((snap.get("trade_plans") or {}).get("rows") or [])
    screening_final = (((snap.get("etl") or {}).get("screening") or {}).get("final") or [])
    plan_count = len(trade_plans) or len(screening_final)
    width = env.get("width") or {}
    volume = env.get("volume") or {}
    trend = env.get("trend") or {}

    if is_etl:
        trade_date = str(snap.get("meta", {}).get("date") or m.get("date") or "")
        overlay = _etl_market_overlay(trade_date)
        market_row = overlay.get("market_row") or {}
        market_score = _f(market_row.get("market_score") or env.get("market_score") or (m.get("scores") or {}).get("etl_market_score"))
        trend_score = _f(market_row.get("trend_score") or env.get("trend_score"))
        amount_ratio = overlay.get("amount_ratio_prev")
        if amount_ratio is None:
            amount_ratio = metrics.get("amount_ratio_5d")
        if amount_ratio is None:
            amount_ratio = market_row.get("amount_ratio_5d")
        position = _position_range(market_score)
        up_ratio = overlay.get("up_ratio")
        if up_ratio is None and metrics.get("up_ratio") is not None:
            up_ratio = round(_f(metrics.get("up_ratio")) * 100, 1)
        amount_total = overlay.get("amount_total") or market_row.get("amount_yuan")
        limit_up = overlay.get("limit_up")
        limit_down = overlay.get("limit_down")
        max_board = overlay.get("max_board")
        summary_parts = [
            f"市场分 {market_score:.0f}",
            f"红盘占比 {up_ratio:.1f}%" if up_ratio is not None else "",
            f"涨停/跌停 {limit_up}/{limit_down}" if limit_up is not None and limit_down is not None else "",
            f"成交额 {amount_total / 1e12:.2f}万亿" if amount_total else "",
        ]
        return {
            "available": True,
            "engine": "etl",
            "date": trade_date,
            "index_date": trade_date,
            "index_stale": False,
            "indices": overlay.get("indices") or [],
            "limit_up": limit_up if limit_up is not None else metrics.get("limit_up_count"),
            "limit_down": limit_down if limit_down is not None else metrics.get("limit_down_count"),
            "max_board": max_board if max_board is not None else metrics.get("max_board_height"),
            "broken_rate": overlay.get("broken_rate") if overlay.get("broken_rate") is not None else metrics.get("broken_rate"),
            "continuous_rate": overlay.get("continuous_rate") if overlay.get("continuous_rate") is not None else metrics.get("continuous_rate"),
            "up_count": overlay.get("up_count"),
            "down_count": overlay.get("down_count"),
            "flat_count": overlay.get("flat_count"),
            "up_ratio": up_ratio,
            "vol_ratio": amount_ratio,
            "vol_pct": round((_f(amount_ratio, 1.0) - 1.0) * 100, 1) if amount_ratio is not None else None,
            "vol_word": "放量" if _f(amount_ratio, 1.0) >= 1.05
            else ("缩量" if _f(amount_ratio, 1.0) <= 0.95 else "平量"),
            "amount_total": amount_total,
            "amount_text": overlay.get("amount_text") or _format_amount_yuan(amount_total),
            "amount_prev_date": overlay.get("amount_prev_date") or "",
            "amount_source": overlay.get("amount_source") or "",
            "limit_source": overlay.get("limit_source") or "",
            "cycle_name": m.get("cycle_name"),
            "phase_label": None,
            "phase_progress": None,
            "transition_warning": None,
            "next_cycle": None,
            "position": position,
            "strategy": m.get("strategy"),
            "cohorts": overlay.get("cohorts") or metrics.get("cohorts") or {},
            "promotion": overlay.get("promotion") or metrics.get("promotion") or {},
            "new_phase": phase_model.get("phase"),
            "new_momentum": phase_model.get("momentum"),
            "trunk_clarity": round(_f(phase_model.get("trunk_clarity")) * 100, 0) if phase_model.get("trunk_clarity") is not None else None,
            "composite_score": round(market_score, 0),
            "trend_score": round(trend_score, 0),
            "trend_state": _trend_state(trend_score),
            "risk_level": "低" if market_score >= 70 else ("中" if market_score >= 50 else ("高" if market_score >= 35 else "极高")),
            "suggested_position": position,
            "analysis_summary": "，".join(p for p in summary_parts if p) + "。",
            "cross_judgment": "盘中买入只由实时行情确认；低开候选直接取消。",
            "plan_count": plan_count,
        }

    # —— 各指数涨跌幅 ——
    idx_defs = [("上证", "sh_index"), ("深证", "sz_index"), ("创业板", "cyb_index"),
                ("科创50", "kcb_index"), ("北证", "bj_index")]
    indices: List[Dict[str, Any]] = []
    for label, key in idx_defs:
        d = env.get(key) or {}
        if d:
            indices.append({
                "name": label,
                "close": round(_f(d.get("close")), 2),
                "pct": round(_f(d.get("change_pct")), 2),
            })

    # —— 市场量能：对比昨日缩量 / 放量 ——
    vol_ratio = volume.get("ratio")
    vol_pct = round((_f(vol_ratio) - 1.0) * 100, 1) if vol_ratio is not None else None
    if vol_ratio is None:
        vol_word = ""
    elif _f(vol_ratio) >= 1.05:
        vol_word = "放量"
    elif _f(vol_ratio) <= 0.95:
        vol_word = "缩量"
    else:
        vol_word = "平量"

    # —— 指数 EOD 滞后检测：指数数据实际日期与快照日期不一致时给出提示 ——
    snap_date = env.get("trade_date") or snap.get("date")
    index_date = env.get("index_trade_date") or snap_date
    index_stale = bool(env.get("index_stale")) or (
        index_date is not None and snap_date is not None and str(index_date) != str(snap_date)
    )

    return {
        "available": True,
        "date": snap_date,
        "index_date": index_date,
        "index_stale": index_stale,
        "indices": indices,
        # 涨跌停
        "limit_up": metrics.get("limit_up_count"),
        "limit_down": metrics.get("limit_down_count"),
        "max_board": metrics.get("max_board_height"),
        "broken_rate": metrics.get("broken_rate"),
        "continuous_rate": metrics.get("continuous_rate"),
        # 涨跌家数
        "up_count": width.get("up_count"),
        "down_count": width.get("down_count"),
        "flat_count": width.get("flat_count"),
        "up_ratio": round(_f(width.get("up_ratio")) * 100, 1) if width.get("up_ratio") is not None else None,
        # 量能
        "vol_ratio": vol_ratio,
        "vol_pct": vol_pct,
        "vol_word": vol_word or volume.get("state", ""),
        "amount_total": volume.get("total"),
        # 情绪周期
        "cycle_name": m.get("cycle_name"),
        "phase_label": phase.get("phase_label"),
        "phase_progress": round(_f(phase.get("phase_progress")) * 100, 0) if phase.get("phase_progress") is not None else None,
        "transition_warning": phase.get("transition_warning"),
        "next_cycle": phase.get("next_likely_cycle"),
        "position": m.get("position"),
        "strategy": m.get("strategy"),
        # P1 分群子指标（大/中军/小票）+ 真·晋级率（仅展示）
        "cohorts": metrics.get("cohorts") or {},
        "promotion": metrics.get("promotion") or {},
        # 循环相位模型（情绪周期权威来源）
        "new_phase": phase_model.get("phase"),
        "new_momentum": phase_model.get("momentum"),
        "trunk_clarity": round(_f(phase_model.get("trunk_clarity")) * 100, 0) if phase_model.get("trunk_clarity") is not None else None,
        # 综合趋势
        "trend_state": trend.get("state"),
        "trend_score": round(_f(trend.get("score")), 0) if trend.get("score") is not None else None,
        "composite_score": round(_f(env.get("composite_score")), 0) if env.get("composite_score") is not None else None,
        "risk_level": env.get("risk_level"),
        "suggested_position": env.get("suggested_position"),
        "analysis_summary": env.get("analysis_summary"),
        "cross_judgment": env.get("cross_judgment"),
        "plan_count": plan_count,
    }


def overview() -> Dict[str, Any]:
    from desktop.runner import CONTROLLER

    reader = SnapshotReader(SNAPSHOT_DIR)
    return {
        "checks": health_items(),
        "latest": reader.latest(),
        "snapshot_count": len(reader.list_dates()),
        "market": market_overview(reader),
        "run": {
            "state": CONTROLLER.state,
            "date": CONTROLLER.date,
            "started_at": CONTROLLER.started_at,
            "finished_at": CONTROLLER.finished_at,
            "error": CONTROLLER.error,
        },
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_dir": str(BASE_DIR),
    }


def tail_log(lines: int = 500) -> str:
    """读取 system.log 末尾若干行（从文件尾部读约 512KB，避免读超大文件）。"""
    try:
        if not LOG_PATH.exists():
            return ""
        size = LOG_PATH.stat().st_size
        chunk = min(size, 512 * 1024)
        with open(LOG_PATH, "rb") as f:
            if size > chunk:
                f.seek(size - chunk)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        rows = text.splitlines()
        n = max(1, min(int(lines or 500), 5000))
        return "\n".join(rows[-n:])
    except Exception as exc:  # noqa: BLE001
        return f"[读取日志失败] {exc!r}"
