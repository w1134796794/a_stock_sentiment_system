"""概览页数据：健康检查 + 关键产物统计。仅用轻量依赖（标准库 + 配置/快照读取）。"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config.settings import (
    BASE_DIR,
    FACTOR_DB_PATH,
    KB_DB_PATH,
    LLM_CONFIG,
    OUTPUT_DIR,
    SNAPSHOT_DIR,
    TUSHARE_TOKEN,
    WEB_DATA_DIR,
)
from snapshot.reader import SnapshotReader

DRAGON_POOL_PATH = Path(BASE_DIR) / "dragon_pools.json"
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


def _read_dragon_pools() -> Dict[str, Any]:
    try:
        if DRAGON_POOL_PATH.exists():
            return json.loads(DRAGON_POOL_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def health_items() -> List[Dict[str, Any]]:
    """返回若干健康检查项，供概览页渲染（状态 ok / warn）。"""
    reader = SnapshotReader(SNAPSHOT_DIR)
    dates = reader.list_dates()
    latest = reader.latest()

    pools = _read_dragon_pools()
    dragon_n = len(pools.get("dragon_pool", {}) or {})
    weak_n = len(pools.get("weakening_pool", {}) or {})

    token_ok = bool((TUSHARE_TOKEN or "").strip())
    ai_key = (LLM_CONFIG.get("api_key") or "").strip()
    ai_ok = bool(ai_key) and ai_key != "your-api-key-here"

    items: List[Dict[str, Any]] = [
        {
            "title": "Tushare Token",
            "status": _ok(token_ok),
            "value": "已配置" if token_ok else "未配置",
            "detail": "数据更新需要 Tushare 历史数据接口；可在「参数配置」或 .env 中设置 TUSHARE_TOKEN。",
            "badge": "正常" if token_ok else "缺失",
        },
        {
            "title": "AI 解读 / 问答",
            "status": _ok(ai_ok),
            "value": "已配置" if ai_ok else "未配置（降级为离线词法检索）",
            "detail": "配置 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 后启用 AI 每日解读与问答。",
            "badge": "正常" if ai_ok else "可选",
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
            "title": "龙头池",
            "status": _ok(DRAGON_POOL_PATH.exists()),
            "value": (
                f"龙头 {dragon_n} 只 · 走弱 {weak_n} 只"
                if DRAGON_POOL_PATH.exists()
                else "暂无 dragon_pools.json"
            ),
            "detail": f"更新时间：{pools.get('update_time', '—')}",
            "badge": "已就绪" if DRAGON_POOL_PATH.exists() else "缺失",
        },
        {
            "title": "数据目录",
            "status": _ok(Path(WEB_DATA_DIR).exists()),
            "value": str(WEB_DATA_DIR),
            "detail": f"数据产物：{WEB_DATA_DIR} · 知识库：{KB_DB_PATH} · 兼容输出：{OUTPUT_DIR}",
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


def _limit_threshold(code: Any, name: Any = "") -> float:
    code6 = _normalize_stock_code(code)
    label = str(name or "").upper()
    if "ST" in label:
        return 4.8
    if code6.startswith(("300", "301", "688", "689")):
        return 19.5
    if code6.startswith(("8", "4", "920")):
        return 29.5
    return 9.5


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


def _board_counts(df: Any, date: str) -> Dict[str, int]:
    """Count consecutive limit-up days ending at date when historical rows exist."""
    counts: Dict[str, int] = {}
    if df is None or getattr(df, "empty", True):
        return counts
    for code, group in df.groupby("code", sort=False):
        group = group.sort_values("trade_date")
        if str(group.iloc[-1].get("trade_date")) != str(date) or not bool(group.iloc[-1].get("is_limit_up")):
            continue
        n = 0
        for ok in reversed(group["is_limit_up"].tolist()):
            if ok:
                n += 1
            else:
                break
        counts[str(code)] = max(n, 1)
    return counts


def _cohort_stats(rows: Any, board_count: Dict[str, int]) -> Dict[str, Any]:
    if rows is None or getattr(rows, "empty", True):
        return {
            "limit_up_count": None,
            "limit_down_count": None,
            "max_board_height": None,
            "continuous_count": None,
            "broken_rate": None,
        }
    codes = [str(x) for x in rows.get("code", []).tolist()]
    boards = [board_count.get(c, 1) for c in codes if board_count.get(c, 0) > 0]
    return {
        "limit_up_count": int(rows["is_limit_up"].sum()),
        "limit_down_count": int(rows["is_limit_down"].sum()),
        "max_board_height": max(boards) if boards else 0,
        "continuous_count": sum(1 for b in boards if b >= 2),
        "broken_rate": None,
    }


def _promotion_stats(hist: Any, today_board: Dict[str, int], date: str) -> Dict[str, Any]:
    empty = {"overall": None, "rate_1to2": None, "rate_2to3": None, "rate_high": None}
    if hist is None or getattr(hist, "empty", True):
        return empty
    dates = sorted(str(x) for x in hist["trade_date"].dropna().unique().tolist() if str(x) < str(date))
    if not dates:
        return empty
    prev_date = dates[-1]
    prev_hist = hist[hist["trade_date"] <= prev_date].copy()
    prev_board = _board_counts(prev_hist, prev_date)
    if not prev_board:
        return empty

    def rate(prev_level: int | None = None, high: bool = False) -> float | None:
        base = []
        for code, b in prev_board.items():
            if high and b >= 3:
                base.append((code, b))
            elif prev_level is not None and b == prev_level:
                base.append((code, b))
        if not base:
            return None
        promoted = sum(1 for code, b in base if today_board.get(code, 0) >= b + 1)
        return round(promoted / len(base) * 100, 2)

    all_base = list(prev_board.items())
    promoted_all = sum(1 for code, b in all_base if today_board.get(code, 0) >= b + 1)
    return {
        "overall": round(promoted_all / len(all_base) * 100, 2) if all_base else None,
        "rate_1to2": rate(1),
        "rate_2to3": rate(2),
        "rate_high": rate(high=True),
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
                hist = con.execute(
                    """
                    SELECT trade_date, code, ts_code, name, pct_chg, amount_yuan
                    FROM stock_daily_silver
                    WHERE trade_date <= ?
                    ORDER BY code, trade_date
                    """,
                    [str(date)],
                ).fetchdf()
                if hist is not None and not hist.empty:
                    hist["trade_date"] = hist["trade_date"].astype(str)
                    hist["code"] = hist["code"].astype(str)
                    hist["pct_chg"] = hist["pct_chg"].fillna(0).astype(float)
                    hist["amount_yuan"] = hist["amount_yuan"].fillna(0).astype(float)
                    hist["is_limit_up"] = hist.apply(
                        lambda r: float(r["pct_chg"]) >= _limit_threshold(r.get("code"), r.get("name")),
                        axis=1,
                    )
                    hist["is_limit_down"] = hist.apply(
                        lambda r: float(r["pct_chg"]) <= -_limit_threshold(r.get("code"), r.get("name")),
                        axis=1,
                    )
                    today = hist[hist["trade_date"] == str(date)].copy()
                    if not today.empty:
                        today_board = _board_counts(hist, date)
                        total = max(len(today), 1)
                        out.update({
                            "up_count": int((today["pct_chg"] > 0).sum()),
                            "down_count": int((today["pct_chg"] < 0).sum()),
                            "flat_count": int((today["pct_chg"] == 0).sum()),
                            "up_ratio": round(float((today["pct_chg"] > 0).sum()) / total * 100, 1),
                            "limit_up": int(today["is_limit_up"].sum()),
                            "limit_down": int(today["is_limit_down"].sum()),
                            "max_board": max(today_board.values()) if today_board else 0,
                            "amount_total": float(today["amount_yuan"].sum()),
                            "promotion": _promotion_stats(hist, today_board, date),
                        })
                        ranked = today.sort_values("amount_yuan", ascending=True).reset_index(drop=True)
                        n = len(ranked)
                        small_end = int(n * 0.6)
                        mid_end = int(n * 0.9)
                        out["cohorts"] = {
                            "small": _cohort_stats(ranked.iloc[:small_end], today_board),
                            "mid": _cohort_stats(ranked.iloc[small_end:mid_end], today_board),
                            "large": _cohort_stats(ranked.iloc[mid_end:], today_board),
                        }
        cache_overlay = _limitup_cache_overlay(date)
        if cache_overlay:
            out.update(cache_overlay)
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
    width = env.get("width") or {}
    volume = env.get("volume") or {}
    trend = env.get("trend") or {}

    if is_etl:
        trade_date = str(snap.get("meta", {}).get("date") or m.get("date") or "")
        overlay = _etl_market_overlay(trade_date)
        market_row = overlay.get("market_row") or {}
        market_score = _f(env.get("market_score") or market_row.get("market_score") or (m.get("scores") or {}).get("etl_market_score"))
        trend_score = _f(env.get("trend_score") or market_row.get("trend_score"))
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
    }


def overview() -> Dict[str, Any]:
    from desktop.runner import CONTROLLER

    reader = SnapshotReader(SNAPSHOT_DIR)
    pools = _read_dragon_pools()
    return {
        "checks": health_items(),
        "latest": reader.latest(),
        "snapshot_count": len(reader.list_dates()),
        "dragon_count": len(pools.get("dragon_pool", {}) or {}),
        "weak_count": len(pools.get("weakening_pool", {}) or {}),
        "pool_update_time": pools.get("update_time", ""),
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


def dragon_pools() -> Dict[str, Any]:
    """返回龙头池/走弱池的精简表格（供龙头池页面渲染）。"""

    # 枚举名 → 中文展示（dragon_pools.json 里存的是 "DragonType.TREND" / "TREND" 形式）
    _ENUM_ZH = {
        "CONTINUOUS": "连板龙头", "TREND": "趋势龙头", "SPACE": "空间龙头",
        "MONITORING": "观察中", "WEAKENING": "已走弱", "RECOVERING": "转强中", "EXPIRED": "已过期",
    }

    def _clean(v: Any) -> Any:
        if isinstance(v, str) and "." in v and v.split(".")[0] in ("DragonType", "DragonStatus"):
            v = v.split(".", 1)[1]
        if isinstance(v, str):
            return _ENUM_ZH.get(v, v)
        return v

    def _stat_interval(entry_date: Any) -> str:
        """统计区间：与「10日涨幅/涨停数」同口径——截至入池日的最近 10 个交易日。

        返回 ``MM-DD~MM-DD``；交易日历不可用或日期非法时返回空串。
        """
        d = str(entry_date or "")[:8]
        if len(d) != 8 or not d.isdigit():
            return ""
        try:
            from core.utils.date_utils import get_last_n_trade_dates

            dates = get_last_n_trade_dates(10, d)  # 倒序，最新在前
            if not dates:
                return ""
            end, start = dates[0], dates[-1]
            return f"{start[4:6]}-{start[6:8]}~{end[4:6]}-{end[6:8]}"
        except Exception:
            return ""

    pools = _read_dragon_pools()

    def _rows(section: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for code, item in (pools.get(section, {}) or {}).items():
            out.append(
                {
                    "代码": code,
                    "名称": item.get("stock_name", ""),
                    "类型": _clean(item.get("dragon_type", "")),
                    "板块": item.get("sector_name", ""),
                    "涨停数": item.get("limit_up_count", ""),
                    "10日涨幅": (
                        f"{round(float(item.get('total_rise_10d', 0)) * 100, 1)}%"
                        if item.get("total_rise_10d") is not None
                        else ""
                    ),
                    "状态": _clean(item.get("status", "")),
                    "入池日": item.get("entry_date", ""),
                    "统计区间": _stat_interval(item.get("entry_date", "")),
                    "走弱类型": item.get("weakening_type", ""),
                    "走弱日": item.get("weakening_date", ""),
                }
            )
        return out

    return {
        "update_time": pools.get("update_time", ""),
        "dragon_rows": _rows("dragon_pool"),
        "weak_rows": _rows("weakening_pool"),
        "exists": DRAGON_POOL_PATH.exists(),
        "path": str(DRAGON_POOL_PATH),
    }
