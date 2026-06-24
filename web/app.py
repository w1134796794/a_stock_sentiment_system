"""
FastAPI 应用 —— 指标体系看板 + 数据浏览（只读）。

启动：
    python run_web.py
或：
    uvicorn web.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter, deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from web.auth_store import (
    SESSION_COOKIE_NAME,
    create_user,
    ensure_auth_db,
    extend_user,
    list_users,
    login as auth_login,
    recent_login_logs,
    reset_password,
    revoke_session,
    revoke_user_sessions,
    update_user_limits,
    update_user_status,
    validate_session,
)

from config.settings import (
    SNAPSHOT_DIR,
    KB_DB_PATH,
    APP_DB_PATH,
    WINRATE_PATH,
    TUSHARE_TOKEN,
    CACHE_DIR,
    WEB_DATA_DIR,
    FACTOR_DB_PATH,
)
from snapshot.reader import SnapshotReader

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
reader = SnapshotReader(SNAPSHOT_DIR)
_REALTIME_QUOTE_SERVICE = None
_REALTIME_SECTOR_SERVICE = None


def _money(value: Any, signed: bool = False) -> str:
    """千分位金额格式化（Jinja 的 % 格式化不支持逗号分组，故用自定义过滤器）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{v:+,.0f}" if signed else f"{v:,.0f}"


templates.env.filters["money"] = _money


def _num2(value: Any) -> Any:
    """数值统一保留两位小数；整数/布尔/非数值（字符串/列表/字典）原样返回。

    用于表格单元格，避免 14.658499999999998 这类浮点尾数直接显示。
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


templates.env.filters["num2"] = _num2


DISPLAY_TEXT_REPLACEMENTS = (
    ("ETL指标筛选", "指标筛选"),
    ("ETL交易计划", "交易计划"),
    ("ETL计划", "交易计划"),
    ("ETL产物", "数据产物"),
    ("运行ETL", "生成数据"),
    ("开始ETL", "开始生成"),
    ("ETL市场分", "市场分"),
    ("实时 Overlay", "实时行情"),
    ("Overlay", "实时行情"),
    ("按 Gold 指标排序", "按指标评分排序"),
    ("Gold板块", "板块热度"),
    ("Gold 指标", "指标"),
    ("Silver 质量报告", "数据质量报告"),
    ("Screening 候选池", "候选池"),
    ("Gold 分析摘要", "分析摘要"),
    ("cancelled/observe", "取消/观察"),
    ("cancelled", "取消"),
    ("observe", "观察"),
)


def _display_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    for old, new in DISPLAY_TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def _sanitize_display(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_display(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_display(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_display(v) for v in obj)
    return _display_text(obj)


templates.env.filters["display_text"] = _display_text


def _screening_context_map(date: Any, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    if not date or not codes or not Path(FACTOR_DB_PATH).exists():
        return {}
    wanted = {_normalize_stock_code(code) for code in codes if _normalize_stock_code(code)}
    if not wanted:
        return {}
    try:
        import duckdb  # type: ignore
    except Exception:
        return {}

    con = duckdb.connect(str(FACTOR_DB_PATH), read_only=True)
    try:
        exists = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'factor_stock_wide'"
        ).fetchone()[0]
        if not exists:
            return {}
        rows = con.execute(
            """
            SELECT code, pct_chg, vol_ratio, amount_ratio, new_high_ratio,
                   liquidity_score, sector_resonance_score
            FROM factor_stock_wide
            WHERE trade_date = ?
            """,
            [str(date)],
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()

    out: Dict[str, Dict[str, Any]] = {}
    columns = [
        "code", "pct_chg", "vol_ratio", "amount_ratio", "new_high_ratio",
        "liquidity_score", "sector_resonance_score",
    ]
    for raw in rows:
        row = dict(zip(columns, raw))
        code = _normalize_stock_code(row.get("code"))
        if code in wanted:
            row.pop("code", None)
            out[code] = row
    return out


def _enrich_screening_display_reasons(snapshot: Dict[str, Any]) -> None:
    """Backfill per-stock screening reasons for old snapshots at display time."""
    try:
        from core.screening.explanations import build_screening_reasons
    except Exception:
        return

    screening = ((snapshot.get("etl") or {}).get("screening") or {})
    final = screening.get("final") or []
    codes = [
        _normalize_stock_code(item.get("code") or item.get("股票代码") or "")
        for item in final
        if isinstance(item, dict)
    ]
    date = ((snapshot.get("meta") or {}).get("date") or snapshot.get("date") or screening.get("trade_date"))
    context_by_code = _screening_context_map(date, codes)
    by_code: Dict[str, List[str]] = {}
    for item in final:
        if not isinstance(item, dict):
            continue
        code = _normalize_stock_code(item.get("code") or item.get("股票代码") or "")
        if not code:
            continue
        base_reasons = item.get("rule_reasons") or item.get("reasons") or []
        reasons = build_screening_reasons(
            metrics=item.get("metrics") or {},
            context=item.get("context") or context_by_code.get(code) or {},
            score=item.get("score") or item.get("综合评分"),
            rank=item.get("rank") or item.get("优先级"),
            base_reasons=base_reasons,
        )
        item["reasons"] = reasons
        by_code[code] = reasons

    if not by_code:
        return

    for row in ((snapshot.get("trade_plans") or {}).get("rows") or []):
        if not isinstance(row, dict):
            continue
        code = _normalize_stock_code(row.get("股票代码") or row.get("code") or "")
        reasons = by_code.get(code)
        if reasons:
            row["筛选理由"] = "；".join(reasons[:5])

    for section in snapshot.get("sections") or []:
        for row in section.get("rows") or []:
            if not isinstance(row, dict):
                continue
            code = _normalize_stock_code(row.get("code") or row.get("股票代码") or row.get("stock_code") or "")
            reasons = by_code.get(code)
            if not reasons:
                continue
            if "reasons" in row:
                row["reasons"] = reasons
            if "筛选理由" in row:
                row["筛选理由"] = "；".join(reasons[:5])


# ----------------------------------------------------------------------
# 表头英文列名 → 中文展示名（只翻译显示，底层数据 key 不变，无需重生成快照）。
# 未收录的列（含已是中文的列）原样返回。
# ----------------------------------------------------------------------
COLUMN_LABELS: Dict[str, str] = {
    # 指标筛选 / 交易计划 / 风控闸门
    "pattern_type": "模式类型",
    "stock_code": "股票代码",
    "stock_name": "股票名称",
    "ts_code": "代码",
    "name": "名称",
    "type": "类型",
    "confidence": "置信度",
    "description": "信号描述",
    "key_metrics": "关键指标",
    "validation_rules": "校验规则",
    "entry_price": "入场价",
    "stop_loss": "止损价",
    "take_profit": "止盈价",
    "position_size": "建议仓位",
    "l2_industry": "二级行业",
    "is_dual_resonance": "双线共振",
    "action": "风控动作",
    "reason_text": "决策原因",
    "original_position_pct": "原始仓位%",
    "final_position_pct": "风控后仓位%",
    # 板块热度（热点概念 / 热点行业 / 持续性）
    "rank": "排名",
    "pct_change": "涨跌幅",
    "limit_up_count": "涨停家数",
    "limit_cons_count": "连板数",
    "sector_code": "板块代码",
    "sector_name": "板块名称",
    "sector_type": "板块类型",
    "member_count": "成分股数",
    "amount": "成交额",
    "avg_amount": "平均成交额",
    "vol": "成交量",
    "composite_score": "综合评分",
    "momentum_score": "动量评分",
    "price_score": "价格评分",
    "amount_score": "成交额评分",
    "amount_ratio_score": "量能评分",
    "persistence_score": "持续性评分",
    "mainline_score": "主线评分",
    "theme_score": "主线主题评分",
    "current_score": "当日主线评分",
    "hot_days_10": "近10日红盘天数",
    "strong_days_10": "近10日强势天数",
    "top50_days_10": "近10日上榜天数",
    "cum_pct_10": "近10日累计涨幅%",
    "avg_pct_5": "5日均涨幅%",
    "limit_score": "涨停评分",
    "amount_rank": "成交额排名",
    "limit_cpt_rank": "涨停概念排名",
    "limit_cpt_score": "涨停概念评分",
    "is_hot": "是否热点",
    "is_hot_concept": "是否热点概念",
    "is_hot_industry": "是否热点行业",
    "_date_list": "上榜日期",
    # 因子原始数据（与导出报表命名一致）
    "tech_D1_n_day_high_low": "D1N日新高低",
    "tech_D2_vol_price_coord": "D2量价配合",
    "tech_D3_seal_strength": "D3封板强度",
    "tech_D4_turnover_health": "D4换手健康",
    "tech_D5_ma_bull_align": "D5均线多头",
    "mf_E1_main_net_ratio": "E1主力净占比",
    "mf_E2_retail_net_ratio": "E2散户净占比",
    "mf_E3_large_buy_ratio": "E3大单买入占比",
    "mf_E4_moneyflow_trend": "E4资金趋势",
    # 候选解释与实时确认相关字段
    "seal_ratio": "封单强度",
    "gap_ratio": "次日高开",
    "gap_pct": "竞价高开",
    "first_board_score": "首板质量分",
    "is_fast": "快速封板",
    "auction_vol_ratio": "竞价量比",
    "flexible_score": "弹性评分",
    "weakening_type": "走弱类型",
    "breakout_type": "突破类型",
    "volume_ratio_excess": "量能超额",
    "break_count": "开板次数",
    "early_seal": "早盘秒封",
    "sector_score": "板块效应",
    "max_boards": "最高连板",
    "days_since_peak": "距高点天数",
    "layer2_clean": "L2干净",
}


def _col_label(col: Any) -> str:
    """列名 → 中文展示名；未收录或已是中文的原样返回。"""
    s = "" if col is None else str(col)
    return COLUMN_LABELS.get(s, s)


templates.env.filters["col_label"] = _col_label


def _sector_type_label(value: Any) -> str:
    text = str(value or "").strip()
    return {
        "N": "概念",
        "I": "行业",
        "R": "地域",
        "S": "特色",
    }.get(text, text)


@lru_cache(maxsize=1)
def _sector_meta_map() -> Dict[str, Dict[str, str]]:
    """Local THS sector code map used to backfill snapshots."""
    mapping: Dict[str, Dict[str, str]] = {}
    base = Path(CACHE_DIR) / "sector" / "ths_index"
    for name in ("index_all.csv", "index_N.csv", "index_I.csv", "adata_concept_ths.csv"):
        path = base / name
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    code = str(
                        row.get("ts_code")
                        or row.get("sector_code")
                        or row.get("index_code")
                        or row.get("concept_code")
                        or row.get("code")
                        or ""
                    ).strip()
                    if not code:
                        continue
                    label = str(row.get("name") or row.get("sector_name") or row.get("index_name") or "").strip()
                    typ = _sector_type_label(row.get("type") or row.get("sector_type") or row.get("板块类型") or "")
                    if name == "adata_concept_ths.csv" and not typ:
                        typ = "概念"
                    for key in _sector_code_keys(code):
                        current = mapping.setdefault(key, {"name": "", "type": ""})
                        if label and not current.get("name"):
                            current["name"] = label
                        if typ and not current.get("type"):
                            current["type"] = typ
        except Exception:
            continue
    return mapping


def _sector_code_keys(code: str) -> List[str]:
    code = str(code or "").strip()
    if not code:
        return []
    keys = [code]
    if "." not in code and code.isdigit():
        keys.append(f"{code}.TI")
    return keys


@lru_cache(maxsize=1)
def _stock_concept_map() -> Dict[str, List[str]]:
    """Stock code -> THS concept names, sourced from local cached concept members."""
    mapping: Dict[str, List[str]] = {}
    base = Path(CACHE_DIR) / "concept" / "members"
    try:
        files = sorted(base.glob("all_*.csv"), key=lambda p: (p.name, p.stat().st_mtime), reverse=True)
    except Exception:
        files = []
    if not files:
        return mapping
    try:
        with files[0].open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = _normalize_stock_code(
                    str(row.get("con_code") or row.get("stock_code") or row.get("ts_code") or "")
                )
                concept = str(row.get("concept_name") or row.get("concept") or row.get("板块名称") or "").strip()
                if not code or not concept:
                    continue
                bucket = mapping.setdefault(code, [])
                if concept not in bucket:
                    bucket.append(concept)
    except Exception:
        return mapping
    return mapping


def _enrich_sector_rows(rows: List[Dict[str, Any]]) -> None:
    mapping = _sector_meta_map()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("sector_code") or row.get("板块代码") or row.get("ts_code") or "").strip()
        meta = mapping.get(code) or {}
        if not str(row.get("sector_name") or row.get("板块名称") or "").strip():
            row["sector_name"] = meta.get("name") or code
        if not str(row.get("sector_type") or row.get("板块类型") or "").strip():
            row["sector_type"] = meta.get("type") or ""


def _allowed_hot_sector(row: Dict[str, Any]) -> bool:
    code = str(row.get("sector_code") or row.get("板块代码") or row.get("ts_code") or "").strip()
    name = str(row.get("sector_name") or row.get("板块名称") or "").strip()
    typ = _sector_type_label(row.get("sector_type") or row.get("板块类型") or "")
    return bool(name and name != code and typ in {"概念", "行业"})


def _score_between(value: Any, low: float, high: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if high <= low:
        return 0.0
    return max(0.0, min(100.0, (v - low) / (high - low) * 100.0))


def _factor_sector_rows(date: str, limit: int = 20) -> List[Dict[str, Any]]:
    fetch_limit = max(int(limit) * 80, 1000)
    try:
        import duckdb  # type: ignore

        if not Path(FACTOR_DB_PATH).exists():
            return []
        with duckdb.connect(str(FACTOR_DB_PATH), read_only=True) as con:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'factor_sector_wide'"
            ).fetchone()[0]
            if not exists:
                return []
            df = con.execute(
                """
                SELECT *
                FROM factor_sector_wide
                WHERE trade_date = ?
                ORDER BY rank ASC, mainline_score DESC
                LIMIT ?
                """,
                [str(date), fetch_limit],
            ).fetchdf()
    except Exception:
        return []
    rows = df.to_dict(orient="records") if df is not None and not df.empty else []
    _enrich_sector_rows(rows)
    named = [row for row in rows if _allowed_hot_sector(row)]
    selected = (named or rows)[: int(limit)]
    for i, row in enumerate(selected, start=1):
        row["rank"] = i
    return selected


def _mainline_theme_rows(date: str, limit: int = 10, lookback: int = 10) -> List[Dict[str, Any]]:
    """主线主题不是当日热点复用：用近N日持续活跃 + 当日确认重新评分。"""
    try:
        import duckdb  # type: ignore

        if not Path(FACTOR_DB_PATH).exists():
            return []
        with duckdb.connect(str(FACTOR_DB_PATH), read_only=True) as con:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'factor_sector_wide'"
            ).fetchone()[0]
            daily_exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'sector_daily_silver'"
            ).fetchone()[0]
            if not exists or not daily_exists:
                return []
            dates = [
                str(x[0]) for x in con.execute(
                    """
                    SELECT DISTINCT trade_date
                    FROM sector_daily_silver
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    [str(date), int(lookback)],
                ).fetchall()
            ]
            if not dates:
                return []
            current_df = con.execute(
                """
                SELECT *
                FROM factor_sector_wide
                WHERE trade_date = ?
                """,
                [str(date)],
            ).fetchdf()
            hist_df = con.execute(
                """
                SELECT trade_date, sector_code, sector_name, sector_type, pct_chg, amount_yuan
                FROM sector_daily_silver
                WHERE trade_date IN (
                    SELECT DISTINCT trade_date
                    FROM sector_daily_silver
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC
                    LIMIT ?
                )
                """,
                [str(date), int(lookback)],
            ).fetchdf()
    except Exception:
        return []

    if current_df is None or current_df.empty or hist_df is None or hist_df.empty:
        return []

    current_rows = current_df.to_dict(orient="records")
    hist_rows = hist_df.to_dict(orient="records")
    _enrich_sector_rows(current_rows)
    _enrich_sector_rows(hist_rows)

    import pandas as pd  # type: ignore

    current = pd.DataFrame([row for row in current_rows if _allowed_hot_sector(row)])
    hist = pd.DataFrame([row for row in hist_rows if _allowed_hot_sector(row)])
    if current.empty or hist.empty:
        return []

    for col in ("pct_chg", "amount_yuan"):
        hist[col] = pd.to_numeric(hist.get(col), errors="coerce").fillna(0.0)
    for col in ("mainline_score", "momentum_score", "amount_score", "amount_ratio_score", "persistence_score"):
        current[col] = pd.to_numeric(current.get(col), errors="coerce").fillna(0.0)

    hist["daily_momentum_score"] = hist["pct_chg"].map(lambda v: _score_between(v, -5.0, 8.0))
    hist["daily_amount_score"] = hist.groupby("trade_date")["amount_yuan"].rank(pct=True).fillna(0.0) * 100.0
    hist["daily_heat_score"] = hist["daily_momentum_score"] * 0.65 + hist["daily_amount_score"] * 0.35
    hist["daily_heat_rank"] = hist.groupby("trade_date")["daily_heat_score"].rank(method="dense", ascending=False)

    rows: List[Dict[str, Any]] = []
    for _, cur in current.iterrows():
        code = str(cur.get("sector_code") or "")
        name = str(cur.get("sector_name") or "")
        h = hist[hist["sector_code"].astype(str) == code].sort_values("trade_date")
        if h.empty:
            continue
        recent = h.tail(int(lookback))
        current_score = float(cur.get("mainline_score") or 0.0)
        persistence_score = float(cur.get("persistence_score") or 0.0)
        amount_ratio_score = float(cur.get("amount_ratio_score") or 0.0)
        hot_days = int((recent["pct_chg"] > 0).sum())
        strong_days = int((recent["pct_chg"] >= 1.5).sum())
        top50_days = int((recent["daily_heat_rank"] <= 50).sum())
        cum_pct = float(recent["pct_chg"].sum())
        avg_pct_5 = float(recent.tail(5)["pct_chg"].mean()) if not recent.tail(5).empty else 0.0

        continuity_score = min(100.0, top50_days / 3.0 * 100.0)
        hot_score = min(100.0, hot_days / 6.0 * 100.0)
        strength_score = min(100.0, strong_days / 3.0 * 100.0)
        trend_score = _score_between(cum_pct, -5.0, 15.0)
        theme_score = (
            current_score * 0.25
            + persistence_score * 0.25
            + continuity_score * 0.20
            + trend_score * 0.15
            + amount_ratio_score * 0.10
            + strength_score * 0.05
        )

        # 主线至少需要“持续证据”或“多日红盘”，不能只因当日排名靠前入选。
        if not (top50_days >= 2 or hot_days >= 5 or persistence_score >= 75.0):
            continue
        if current_score < 45.0:
            continue

        rows.append({
            "板块名称": name,
            "板块代码": code,
            "板块类型": _sector_type_label(cur.get("sector_type") or ""),
            "主线主题评分": round(theme_score, 2),
            "当日主线评分": round(current_score, 2),
            "近10日上榜天数": top50_days,
            "近10日红盘天数": hot_days,
            "近10日强势天数": strong_days,
            "近10日累计涨幅%": round(cum_pct, 2),
            "5日均涨幅%": round(avg_pct_5, 2),
            "量能评分": round(amount_ratio_score, 2),
            "持续性评分": round(persistence_score, 2),
        })

    rows.sort(key=lambda x: (float(x.get("主线主题评分") or 0), int(x.get("近10日上榜天数") or 0)), reverse=True)
    selected = rows[: int(limit)]
    for i, row in enumerate(selected, start=1):
        row["排名"] = i
    return selected


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_board_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split(".")[0]
    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            hh = parts[0].zfill(2)
            mm = parts[1].zfill(2)
            ss = (parts[2] if len(parts) >= 3 else "00").zfill(2)
            return f"{hh}:{mm}:{ss}"
    if not text.isdigit() or int(text or "0") <= 0:
        return ""
    text = text.zfill(6)[-6:]
    return f"{text[:2]}:{text[2:4]}:{text[4:6]}"


def _build_limitup_section_from_cache(date: str) -> Optional[Dict[str, Any]]:
    path = Path(CACHE_DIR) / "summary" / "limit_up_stocks.csv"
    if not path.exists():
        return None
    concept_map = _stock_concept_map()
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for raw in csv.DictReader(f):
                if str(raw.get("trade_date") or "").strip() != str(date):
                    continue
                ts_code = str(raw.get("代码") or "").strip()
                code6 = _normalize_stock_code(ts_code)
                concepts = concept_map.get(code6) or []
                concept_label = " / ".join(concepts[:2])
                amount_yuan = _to_float(raw.get("成交额"))
                rows.append({
                    "行业": str(raw.get("所属行业") or ""),
                    "概念": concept_label,
                    "股票代码": ts_code or code6,
                    "股票名称": str(raw.get("名称") or ""),
                    "连板数": int(_to_float(raw.get("连板数"), 1.0) or 1),
                    "涨幅%": round(_to_float(raw.get("涨跌幅")), 2),
                    "最新价": round(_to_float(raw.get("最新价")), 2),
                    "成交额(亿)": round(amount_yuan / 1e8, 2) if amount_yuan else None,
                    "首次涨停时间": _format_board_time(raw.get("首次封板时间")),
                    "炸板次数": int(_to_float(raw.get("炸板次数"))),
                    "_concepts": concepts[:5],
                })
    except Exception:
        return None
    if not rows:
        return None
    rows.sort(
        key=lambda x: (
            int(x.get("连板数") or 0),
            -int(str(x.get("首次涨停时间") or "99:99:99").replace(":", "") or 999999),
            float(x.get("涨幅%") or 0),
            float(x.get("成交额(亿)") or 0),
        ),
        reverse=True,
    )
    return {
        "name": "涨停梯队",
        "kind": "table",
        "columns": ["行业", "概念", "股票代码", "股票名称", "连板数", "涨幅%", "最新价", "成交额(亿)", "首次涨停时间", "炸板次数"],
        "rows": rows,
        "summary": "包含连板数、封板时间、炸板次数和行业归属。",
    }


def _build_limitup_section(date: str) -> Optional[Dict[str, Any]]:
    cached = _build_limitup_section_from_cache(date)
    if cached:
        return cached

    try:
        import duckdb  # type: ignore

        if not Path(FACTOR_DB_PATH).exists():
            return None
        with duckdb.connect(str(FACTOR_DB_PATH), read_only=True) as con:
            limit_exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'limit_up_pool_silver'"
            ).fetchone()[0]
            if not limit_exists:
                return None
            stock_exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'stock_daily_silver'"
            ).fetchone()[0]
            if stock_exists:
                sql = """
                SELECT l.trade_date, l.code, l.ts_code, l.name, l.pct_chg,
                       l.first_time, l.open_times, l.limit_times, l.fd_amount, l.float_mv,
                       s.close, s.amount_yuan
                FROM limit_up_pool_silver l
                LEFT JOIN stock_daily_silver s
                  ON s.trade_date = l.trade_date AND s.code = l.code
                WHERE l.trade_date = ?
                ORDER BY l.limit_times DESC, l.first_time ASC, l.fd_amount DESC
                """
            else:
                sql = """
                SELECT trade_date, code, ts_code, name, pct_chg,
                       first_time, open_times, limit_times, fd_amount, float_mv,
                       0.0 AS close, 0.0 AS amount_yuan
                FROM limit_up_pool_silver
                WHERE trade_date = ?
                ORDER BY limit_times DESC, first_time ASC, fd_amount DESC
                """
            df = con.execute(
                sql,
                [str(date)],
            ).fetchdf()
    except Exception:
        return None

    if df is None or df.empty:
        return None

    concept_map = _stock_concept_map()
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        amount_yuan = float(r.get("amount_yuan") or 0)
        code = str(r.get("code") or "")
        concepts = concept_map.get(_normalize_stock_code(code)) or []
        concept_label = " / ".join(concepts[:2])
        rows.append({
            "行业": concept_label,
            "概念": concept_label,
            "股票代码": code,
            "股票名称": str(r.get("name") or ""),
            "连板数": int(_to_float(r.get("limit_times"), 1.0) or 1),
            "涨幅%": round(float(r.get("pct_chg") or 0), 2),
            "最新价": round(float(r.get("close") or 0), 2),
            "成交额(亿)": round(amount_yuan / 1e8, 2) if amount_yuan else None,
            "首次涨停时间": _format_board_time(r.get("first_time")),
            "炸板次数": int(_to_float(r.get("open_times"))),
            "_concepts": concepts[:5],
        })
    rows.sort(key=lambda x: (x.get("连板数") or 0, x.get("涨幅%") or 0, x.get("成交额(亿)") or 0), reverse=True)
    return {
        "name": "涨停梯队",
        "kind": "table",
        "columns": ["行业", "概念", "股票代码", "股票名称", "连板数", "涨幅%", "最新价", "成交额(亿)", "首次涨停时间", "炸板次数"],
        "rows": rows,
        "summary": "由 limit_up_pool_silver 官方涨停池生成，并用本地 THS 概念成员表补充概念归属。",
    }


def _build_concept_echelon_section(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows or []:
        concepts = row.get("_concepts") or []
        if not concepts and row.get("概念"):
            concepts = [x.strip() for x in str(row.get("概念")).split("/") if x.strip()]
        for concept in list(concepts)[:3]:
            groups.setdefault(str(concept), []).append(row)
    if not groups:
        return None

    concept_rows: List[Dict[str, Any]] = []
    for concept, stocks in groups.items():
        if not stocks:
            continue
        boards = [int(s.get("连板数") or 1) for s in stocks]
        counter = Counter(boards)
        leaders = sorted(
            stocks,
            key=lambda x: (int(x.get("连板数") or 1), float(x.get("涨幅%") or 0), float(x.get("成交额(亿)") or 0)),
            reverse=True,
        )
        lead = leaders[0]
        dist = [f"{level}板{counter[level]}" for level in sorted(counter.keys(), reverse=True)]
        representatives = "、".join(str(s.get("股票名称") or "") for s in leaders[:5] if s.get("股票名称"))
        concept_rows.append({
            "概念名称": concept,
            "涨停总数": len(stocks),
            "最高连板": max(boards) if boards else 0,
            "梯队分布": ", ".join(dist),
            "龙头股": f"{lead.get('股票名称') or ''} {int(lead.get('连板数') or 1)}板".strip(),
            "代表个股": representatives,
        })
    if not concept_rows:
        return None
    filtered = [r for r in concept_rows if int(r.get("涨停总数") or 0) >= 2]
    concept_rows = filtered or concept_rows
    concept_rows.sort(key=lambda x: (int(x.get("最高连板") or 0), int(x.get("涨停总数") or 0), str(x.get("概念名称") or "")), reverse=True)
    return {
        "name": "概念连板梯队",
        "kind": "table",
        "columns": ["概念名称", "涨停总数", "最高连板", "梯队分布", "龙头股", "代表个股"],
        "rows": concept_rows[:80],
        "summary": "按涨停股所属概念聚合，展示概念内最高连板、涨停总数和梯队分布。",
    }


def _prepare_sections(sections: List[Dict[str, Any]], date: str) -> List[Dict[str, Any]]:
    prepared = [dict(s) for s in sections or []]
    for section in prepared:
        if section.get("name") == "ETL指标筛选":
            section["name"] = "指标筛选"

    hotspot_rows = _factor_sector_rows(date, limit=20)
    mainline_rows = _mainline_theme_rows(date, limit=20)
    if hotspot_rows or mainline_rows:
        sector_columns = [
            "sector_name", "sector_code", "sector_type",
            "mainline_score", "rank", "momentum_score", "amount_score",
            "amount_ratio_score", "persistence_score", "trade_date", "computed_at",
        ]
        mainline_columns = [
            "板块名称", "板块代码", "板块类型", "主线主题评分", "排名",
            "当日主线评分", "近10日上榜天数", "近10日红盘天数", "近10日强势天数",
            "近10日累计涨幅%", "5日均涨幅%", "量能评分", "持续性评分",
        ]
        found_hot = False
        found_mainline = False
        for section in prepared:
            if section.get("name") == "热点概念" and hotspot_rows:
                section["columns"] = [c for c in sector_columns if c in hotspot_rows[0]]
                section["rows"] = hotspot_rows[:10]
                section["summary"] = "当日概念/行业板块强度排名；仅保留概念和行业板块。"
                found_hot = True
            elif section.get("name") == "主线主题" and mainline_rows:
                section["columns"] = [c for c in mainline_columns if c in mainline_rows[0]]
                section["rows"] = mainline_rows[:10]
                section["summary"] = "近10日持续活跃 + 当日确认重新计算；不是当日热点的简单复用。"
                found_mainline = True
        if hotspot_rows and not found_hot:
            prepared.insert(0, {
                "name": "热点概念",
                "kind": "table",
                "columns": [c for c in sector_columns if c in hotspot_rows[0]],
                "rows": hotspot_rows[:10],
                "summary": "当日概念/行业板块强度排名；仅保留概念和行业板块。",
            })
        if mainline_rows and not found_mainline:
            insert_at = 1 if hotspot_rows else 0
            prepared.insert(insert_at, {
                "name": "主线主题",
                "kind": "table",
                "columns": [c for c in mainline_columns if c in mainline_rows[0]],
                "rows": mainline_rows[:10],
                "summary": "近10日持续活跃 + 当日确认重新计算；不是当日热点的简单复用。",
            })
    else:
        for section in prepared:
            if section.get("name") in ("热点概念", "热点行业", "主线主题"):
                _enrich_sector_rows(section.get("rows") or [])

    limit_section = _build_limitup_section(date)
    if limit_section and limit_section.get("rows"):
        replaced = False
        for i, section in enumerate(prepared):
            if section.get("name") == "涨停梯队":
                if not section.get("rows"):
                    prepared[i] = limit_section
                replaced = True
                break
        if not replaced:
            insert_at = len(prepared)
            for i, section in enumerate(prepared):
                if section.get("name") in ("主线主题", "热点行业", "热点概念"):
                    insert_at = i + 1
            prepared.insert(insert_at, limit_section)
        concept_section = _build_concept_echelon_section(limit_section.get("rows") or [])
        if concept_section and concept_section.get("rows"):
            concept_replaced = False
            for i, section in enumerate(prepared):
                if section.get("name") == "概念连板梯队":
                    prepared[i] = concept_section
                    concept_replaced = True
                    break
            if not concept_replaced:
                insert_at = len(prepared)
                for i, section in enumerate(prepared):
                    if section.get("name") == "涨停梯队":
                        insert_at = i + 1
                        break
                prepared.insert(insert_at, concept_section)
    return prepared


def _prepare_snapshot(snapshot: Optional[Dict[str, Any]], date: str) -> Optional[Dict[str, Any]]:
    if snapshot is None:
        return None
    prepared = dict(snapshot)
    prepared["sections"] = _prepare_sections((snapshot or {}).get("sections", []), date)
    _enrich_screening_display_reasons(prepared)
    return _sanitize_display(prepared)


_DATES_CACHE: Dict[str, Any] = {"expires_at": 0.0, "dates": []}


def _snapshot_stat(date: Any) -> Optional[tuple[int, int]]:
    text = str(date or "").strip()
    if not text:
        return None
    path = SNAPSHOT_DIR / f"{text}.json"
    try:
        stat = path.stat()
    except OSError:
        return None
    return int(stat.st_mtime_ns), int(stat.st_size)


@lru_cache(maxsize=96)
def _load_snapshot_cached(date: str, mtime_ns: int, size: int) -> Optional[Dict[str, Any]]:
    path = SNAPSHOT_DIR / f"{date}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@lru_cache(maxsize=96)
def _load_prepared_snapshot_cached(date: str, mtime_ns: int, size: int) -> Optional[Dict[str, Any]]:
    raw = _load_snapshot_cached(date, mtime_ns, size)
    return _prepare_snapshot(raw, date)


def _load_snapshot(date: Any) -> Optional[Dict[str, Any]]:
    text = str(date or "").strip()
    stat = _snapshot_stat(text)
    if stat is None:
        return None
    return _load_snapshot_cached(text, stat[0], stat[1])


def _load_prepared_snapshot(date: Any) -> Optional[Dict[str, Any]]:
    text = str(date or "").strip()
    stat = _snapshot_stat(text)
    if stat is None:
        return None
    return _load_prepared_snapshot_cached(text, stat[0], stat[1])


def _list_dates() -> List[str]:
    now = time.time()
    cached = _DATES_CACHE.get("dates") or []
    if cached and now < float(_DATES_CACHE.get("expires_at") or 0):
        return list(cached)
    dates = reader.list_dates()
    _DATES_CACHE["dates"] = dates
    _DATES_CACHE["expires_at"] = now + 5.0
    return list(dates)


def _latest_date() -> Optional[str]:
    pointer = SNAPSHOT_DIR / "latest.txt"
    try:
        latest = pointer.read_text(encoding="utf-8").strip()
        if latest and _snapshot_stat(latest):
            return latest
    except OSError:
        pass
    dates = _list_dates()
    return dates[0] if dates else None


def _clear_data_caches() -> None:
    _load_snapshot_cached.cache_clear()
    _load_prepared_snapshot_cached.cache_clear()
    _DATES_CACHE["expires_at"] = 0.0
    _DATES_CACHE["dates"] = []

# ----------------------------------------------------------------------
# 数据浏览分类：优先展示指标筛选和板块强度结果。
# signals=True 仅作为旧快照兼容读取，不再是默认主路径。
# 注：候选池、板块热度与涨停数据分别由数据浏览页承载。
# ----------------------------------------------------------------------
DATA_CATEGORIES: List[Dict[str, Any]] = [
    {"key": "strategy", "label": "指标筛选", "signals": True, "names": ["ETL指标筛选", "指标筛选", "交易计划"]},
    {"key": "sector", "label": "板块热度", "signals": False,
     "names": ["热点概念", "热点行业", "概念持续性", "行业持续性", "主线主题"]},
    {"key": "limitup", "label": "涨停数据", "signals": False,
     "names": ["涨停梯队", "概念连板梯队"]},
    {"key": "capital", "label": "龙虎榜资金", "signals": False,
     "names": ["龙虎榜", "资金流向", "筹码结构"]},
]
_CATEGORY_BY_KEY = {c["key"]: c for c in DATA_CATEGORIES}

app = FastAPI(title="A股情绪系统 · 指标看板", docs_url="/api/docs")

_static_dir = BASE / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ----------------------------------------------------------------------
# 登录、订阅与权限
# ----------------------------------------------------------------------
PUBLIC_PATHS = {"/login", "/logout", "/expired", "/favicon.ico"}
ADMIN_PAGE_PREFIXES = (
    "/admin",
    "/run",
    "/config",
    "/factors",
    "/logs",
    "/ask",
    "/api/docs",
    "/openapi.json",
)
ADMIN_GET_API_PREFIXES = (
    "/api/admin",
    "/api/run",
    "/api/logs",
    "/api/config",
    "/api/factors",
    "/api/backtest/run/status",
)
_RATE_BUCKETS: Dict[str, deque] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:500]


def _safe_next(raw: Optional[str]) -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/") or path == "/openapi.json"


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _requires_admin(path: str, method: str) -> bool:
    if _matches_prefix(path, ADMIN_PAGE_PREFIXES):
        return True
    if _is_api_path(path):
        if method.upper() != "GET":
            return True
        return _matches_prefix(path, ADMIN_GET_API_PREFIXES)
    return False


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(target, safe='')}", status_code=303)


def _rate_limit_exceeded(request: Request, user: Optional[Dict[str, Any]]) -> bool:
    if request.url.path.startswith("/static/"):
        return False
    try:
        base_limit = int(os.getenv("APP_RATE_LIMIT_PER_MINUTE", "240"))
    except ValueError:
        base_limit = 240
    if user and user.get("role") == "admin":
        base_limit = max(base_limit, 600)
    if not user and request.url.path == "/login":
        base_limit = min(base_limit, 40)
    key = f"user:{user.get('id')}" if user else f"ip:{_client_ip(request)}"
    now = time.time()
    window = 60.0
    bucket = _RATE_BUCKETS.setdefault(key, deque())
    while bucket and bucket[0] <= now - window:
        bucket.popleft()
    if len(bucket) >= base_limit:
        return True
    bucket.append(now)
    return False


def _rate_limited_response(request: Request) -> HTMLResponse | JSONResponse:
    if _is_api_path(request.url.path):
        return JSONResponse({"error": "rate_limited", "message": "请求过于频繁，请稍后再试"}, status_code=429)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><body style="background:#020617;color:#e2e8f0;font-family:sans-serif;padding:40px">请求过于频繁，请稍后再试。</body>',
        status_code=429,
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    ensure_auth_db()
    path = request.url.path
    user, auth_error = validate_session(request.cookies.get(SESSION_COOKIE_NAME))
    request.state.user = user
    request.state.is_admin = bool(user and user.get("role") == "admin")
    request.state.is_viewer = bool(user and user.get("role") == "viewer")

    if _rate_limit_exceeded(request, user):
        return _rate_limited_response(request)

    if _is_public_path(path):
        return await call_next(request)

    if not user:
        if auth_error in {"session_revoked", "session_expired", "user_disabled"}:
            response = (
                JSONResponse({"error": auth_error, "message": "登录状态已失效"}, status_code=401)
                if _is_api_path(path)
                else _login_redirect(request)
            )
            response.delete_cookie(SESSION_COOKIE_NAME)
            return response
        if _is_api_path(path):
            return JSONResponse({"error": "not_authenticated", "message": "请先登录"}, status_code=401)
        return _login_redirect(request)

    if auth_error == "subscription_expired":
        if _is_api_path(path):
            return JSONResponse({"error": "subscription_expired", "message": "服务已到期"}, status_code=403)
        return RedirectResponse(url="/expired", status_code=303)

    if user.get("role") != "admin" and _requires_admin(path, request.method):
        if _is_api_path(path):
            return JSONResponse(
                {"error": "readonly_forbidden", "message": "只读账号不能执行任务或修改配置"},
                status_code=403,
            )
        return templates.TemplateResponse(
            request,
            "permission_denied.html",
            {"user": user},
            status_code=403,
        )

    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: Optional[str] = None) -> Any:
    if request.state.user:
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": ""},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Any:
    raw = (await request.body()).decode("utf-8")
    data = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    next_url = _safe_next(data.get("next"))

    ok, message, token, user, max_age = auth_login(
        username=username,
        password=password,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    if not ok:
        template = "expired.html" if user and user.get("is_expired") else "login.html"
        return templates.TemplateResponse(
            request,
            template,
            {"next": next_url, "error": message, "user": user},
            status_code=401,
        )

    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token or "",
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=os.getenv("APP_COOKIE_SECURE", "0") == "1",
    )
    return response


@app.get("/logout")
def logout(request: Request) -> Any:
    revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/expired", response_class=HTMLResponse)
def expired_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "expired.html", {"user": request.state.user})


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Any:
    """概览页：健康检查 + 关键产物统计（管理工具首页）。"""
    from desktop.status import overview

    return templates.TemplateResponse(request, "overview.html", {"ov": overview()})


@app.get("/report", response_class=HTMLResponse)
def report_index() -> Any:
    latest = _latest_date()
    return RedirectResponse(url=f"/report/{latest}" if latest else "/")


@app.get("/dragon", response_class=HTMLResponse)
def dragon_page(request: Request, date: Optional[str] = None) -> Any:
    """因子体系龙头池：从近几日筛选结果派生，不使用旧策略池。"""
    latest = date or _latest_date()
    return templates.TemplateResponse(
        request,
        "dragon.html",
        {"date": latest, "dates": _list_dates()},
    )


@app.get("/intraday", response_class=HTMLResponse)
def intraday_page(request: Request, date: Optional[str] = None) -> Any:
    """盘中转强：因子龙头池叠加实时行情确认。"""
    latest = date or _latest_date()
    return templates.TemplateResponse(
        request,
        "intraday.html",
        {"date": latest, "dates": _list_dates()},
    )


@app.get("/realtime", response_class=HTMLResponse)
def realtime_page(request: Request) -> Any:
    """实时行情面板：个股批量行情、板块行情、行情源健康。"""
    latest = _latest_date()
    snapshot = _load_snapshot(latest) if latest else None
    return templates.TemplateResponse(
        request,
        "realtime.html",
        {
            "date": latest,
            "default_codes": _default_realtime_codes(snapshot),
        },
    )


def _default_realtime_codes(snapshot: Optional[Dict], limit: int = 8) -> List[str]:
    """从最新交易计划里提取默认监控代码。"""
    codes: List[str] = []
    if snapshot:
        for row in (snapshot.get("trade_plans", {}) or {}).get("rows", []) or []:
            code = row.get("股票代码") or row.get("stock_code") or row.get("代码") or ""
            code = _normalize_stock_code(code)
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= limit:
                break
    return codes or ["000001", "600000", "300750", "002594"]


@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request) -> Any:
    """生成数据页：一键执行指标主流程并实时查看日志。"""
    from desktop.runner import CONTROLLER
    from desktop.status import etl_artifacts

    return templates.TemplateResponse(
        request,
        "run.html",
        {"run": CONTROLLER.status(0), "artifacts": etl_artifacts()},
    )


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "logs.html", {})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> Any:
    from desktop.status import overview

    return templates.TemplateResponse(request, "about.html", {"ov": overview()})


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request) -> Any:
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": list_users(), "login_logs": recent_login_logs(80)},
    )


@app.get("/api/admin/users")
def api_admin_users() -> Any:
    return JSONResponse({"users": list_users(), "login_logs": recent_login_logs(80)})


@app.post("/api/admin/users")
def api_admin_create_user(payload: dict = Body(default={})) -> Any:
    p = payload or {}
    try:
        user = create_user(
            username=p.get("username"),
            password=p.get("password"),
            role=p.get("role") or "viewer",
            display_name=p.get("display_name") or "",
            expire_at=p.get("expire_at") or None,
            days=int(p.get("days") or 30),
            max_sessions=int(p.get("max_sessions") or 1),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/admin/users/{user_id}/extend")
def api_admin_extend_user(user_id: int, payload: dict = Body(default={})) -> Any:
    p = payload or {}
    try:
        user = extend_user(user_id, days=p.get("days"), expire_at=p.get("expire_at") or None)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/admin/users/{user_id}/status")
def api_admin_status_user(request: Request, user_id: int, payload: dict = Body(default={})) -> Any:
    p = payload or {}
    status = "active" if p.get("status") == "active" else "disabled"
    if request.state.user and int(request.state.user.get("id") or 0) == int(user_id) and status == "disabled":
        return JSONResponse({"ok": False, "error": "不能禁用当前登录账号"}, status_code=400)
    try:
        user = update_user_status(user_id, status)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/admin/users/{user_id}/reset-password")
def api_admin_reset_password(user_id: int, payload: dict = Body(default={})) -> Any:
    password = (payload or {}).get("password") or ""
    try:
        user = reset_password(user_id, password)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/admin/users/{user_id}/revoke-sessions")
def api_admin_revoke_sessions(user_id: int) -> Any:
    revoke_user_sessions(user_id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/users/{user_id}/limits")
def api_admin_user_limits(user_id: int, payload: dict = Body(default={})) -> Any:
    p = payload or {}
    try:
        user = update_user_limits(
            user_id,
            max_sessions=int(p.get("max_sessions") or 1),
            display_name=p.get("display_name") or "",
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "user": user})


# ----------------------------------------------------------------------
# 管理工具 JSON API（运行 / 日志 / 概览）
# ----------------------------------------------------------------------
@app.post("/api/run")
def api_run(payload: dict = Body(default={})) -> Any:
    from desktop.runner import CONTROLLER

    _clear_data_caches()
    data = payload or {}
    mode = (data.get("mode") or "single").strip().lower()
    if mode == "batch":
        ok, msg = CONTROLLER.start_batch(data.get("start_date"), data.get("end_date"))
    else:
        mode = "single"
        ok, msg = CONTROLLER.start(data.get("date"))
    return JSONResponse({"started": ok, "message": msg, "mode": mode})


@app.get("/api/run/status")
def api_run_status(since: int = 0) -> Any:
    from desktop.runner import CONTROLLER
    from desktop.status import etl_artifacts

    status = CONTROLLER.status(since)
    status["artifacts"] = etl_artifacts(status.get("date"))
    return JSONResponse(status)


@app.get("/api/logs")
def api_logs(lines: int = 500) -> Any:
    from desktop.status import tail_log

    return JSONResponse({"text": tail_log(lines)})


@app.get("/api/overview")
def api_overview() -> Any:
    from desktop.status import overview

    return JSONResponse(overview())


@app.get("/api/etl/artifacts")
def api_etl_artifacts(date: Optional[str] = None) -> Any:
    from desktop.status import etl_artifacts

    return JSONResponse(etl_artifacts(date))


@app.get("/report/{date}", response_class=HTMLResponse)
def report(request: Request, date: str) -> Any:
    snapshot = _load_prepared_snapshot(date)
    dates = _list_dates()
    if snapshot is None:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"snapshot": None, "date": date, "dates": dates},
            status_code=404,
        )
    market = snapshot.get("market", {}) or {}
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "snapshot": snapshot,
            "date": date,
            "dates": dates,
            "plans": snapshot.get("trade_plans", {}).get("rows", []),
            "market": market,
            "risk_gate": snapshot.get("risk_gate"),
            "sections": snapshot.get("sections", []),
            "winrate": _load_winrate(),
            "cur_cycle": market.get("cycle_name") or "",
        },
    )


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock_detail_page(request: Request, code: str, date: Optional[str] = None) -> Any:
    """个股详情：分时、日K、竞价摘要、信号与风控上下文。"""
    latest = date or _latest_date()
    snapshot = _load_snapshot(latest) if latest else None
    context = _find_stock_context(snapshot, code)
    return templates.TemplateResponse(
        request,
        "stock_detail.html",
        {
            "code": _normalize_stock_code(code),
            "date": latest,
            "dates": _list_dates(),
            "stock_name": context.get("name") or "",
            "context": context,
        },
    )


@app.get("/api/stock/{code}/chart")
def api_stock_chart(code: str, date: Optional[str] = None, daily_count: int = 120) -> Any:
    """Chart payload for stock detail page."""
    from core.data.data_manager_main import DataManager
    from core.utils.stock_code_utils import StockCodeUtils

    trade_date = date or _latest_date()
    if not trade_date:
        return JSONResponse({"error": "no snapshot date"}, status_code=404)

    ts_code = StockCodeUtils.standardize_code(code, add_suffix=True)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    intraday_df = dm.get_stock_tick(ts_code, trade_date)
    daily_df = dm.get_kline(ts_code, period="day", count=max(20, min(int(daily_count or 120), 300)))
    auction = dm.get_auction_data(ts_code, trade_date)

    snapshot = _load_snapshot(trade_date)
    context = _find_stock_context(snapshot, ts_code)

    return JSONResponse({
        "code": StockCodeUtils.standardize_code(ts_code, add_suffix=False),
        "ts_code": ts_code,
        "name": context.get("name") or "",
        "date": trade_date,
        "intraday": _df_to_intraday_line(intraday_df, trade_date),
        "daily": _df_to_daily_candles(daily_df),
        "auction": auction or {},
        "plans": context.get("plans") or [],
        "signals": context.get("signals") or [],
        "risk": context.get("risk") or [],
    })


def _split_codes(codes: Optional[str]) -> List[str]:
    if not codes:
        return []
    return [c.strip() for c in str(codes).replace("，", ",").split(",") if c.strip()]


def _get_realtime_quote_service(stale_after_seconds: int = 90):
    global _REALTIME_QUOTE_SERVICE
    if _REALTIME_QUOTE_SERVICE is None:
        from core.realtime.quote_service import RealtimeQuoteService

        _REALTIME_QUOTE_SERVICE = RealtimeQuoteService(stale_after_seconds=stale_after_seconds)
    else:
        _REALTIME_QUOTE_SERVICE.stale_after_seconds = max(float(stale_after_seconds), 1.0)
    return _REALTIME_QUOTE_SERVICE


def _get_realtime_sector_service():
    global _REALTIME_SECTOR_SERVICE
    if _REALTIME_SECTOR_SERVICE is None:
        from core.realtime.sector_service import RealtimeSectorService

        _REALTIME_SECTOR_SERVICE = RealtimeSectorService()
    return _REALTIME_SECTOR_SERVICE


def _add_stock_name(mapping: Dict[str, str], code: Any, name: Any) -> None:
    try:
        code6 = _normalize_stock_code(str(code or ""))
    except Exception:
        code6 = ""
    label = str(name or "").strip()
    if code6 and label and not mapping.get(code6):
        mapping[code6] = label


def _stock_name_map(snapshot: Optional[Dict], date: Optional[str] = None) -> Dict[str, str]:
    """Build a best-effort stock name map from local snapshot artifacts."""
    mapping: Dict[str, str] = {}

    def scan_row(row: Dict[str, Any]) -> None:
        _add_stock_name(
            mapping,
            row.get("股票代码") or row.get("stock_code") or row.get("code") or row.get("代码") or row.get("ts_code"),
            row.get("股票名称") or row.get("stock_name") or row.get("name") or row.get("名称"),
        )

    for row in ((snapshot or {}).get("trade_plans") or {}).get("rows", []) or []:
        if isinstance(row, dict):
            scan_row(row)
    for section in (snapshot or {}).get("sections", []) or []:
        for row in section.get("rows") or []:
            if isinstance(row, dict):
                scan_row(row)
    for row in (((snapshot or {}).get("etl") or {}).get("screening") or {}).get("final", []) or []:
        if isinstance(row, dict):
            scan_row(row)

    target = str(date or ((snapshot or {}).get("meta") or {}).get("date") or "")
    if target:
        screening_path = Path(WEB_DATA_DIR) / "screening" / f"screening_{target}.json"
        try:
            data = json.loads(screening_path.read_text(encoding="utf-8"))
            for row in data.get("final") or []:
                if isinstance(row, dict):
                    scan_row(row)
        except Exception:
            pass

        limit_up_path = Path(CACHE_DIR) / "market" / "limit_up" / f"{target}.csv"
        try:
            with limit_up_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    scan_row(row)
        except Exception:
            pass

    return mapping


def _enrich_stock_names(rows: List[Dict[str, Any]], snapshot: Optional[Dict], date: Optional[str]) -> None:
    mapping = _stock_name_map(snapshot, date)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _normalize_stock_code(row.get("code") or row.get("stock_code") or row.get("股票代码") or "")
        if not code:
            continue
        if not str(row.get("name") or row.get("股票名称") or "").strip():
            row["name"] = mapping.get(code) or code
        row.setdefault("display_name", row.get("name") or mapping.get(code) or code)


@app.get("/api/realtime/quote/{code}")
def api_realtime_quote(code: str, include_raw: bool = False) -> Any:
    service = _get_realtime_quote_service()
    quote = service.get_quote(code, include_raw=include_raw)
    if not quote:
        return JSONResponse({"ok": False, "message": "未获取到实时行情", "quote": {}}, status_code=502)
    latest = _latest_date()
    _enrich_stock_names([quote], _load_snapshot(latest) if latest else None, latest)
    return JSONResponse({"ok": True, "quote": quote})


@app.get("/api/realtime/quotes")
def api_realtime_quotes(
    codes: Optional[str] = None,
    include_raw: bool = False,
    stale_after_seconds: int = 90,
) -> Any:
    code_list = _split_codes(codes)
    if not code_list:
        return JSONResponse({"ok": False, "message": "codes 参数不能为空", "quotes": []}, status_code=400)
    service = _get_realtime_quote_service(stale_after_seconds=stale_after_seconds)
    payload = service.get_quotes(code_list, include_raw=include_raw)
    latest = _latest_date()
    _enrich_stock_names(payload.get("quotes") or [], _load_snapshot(latest) if latest else None, latest)
    return JSONResponse(payload)


@app.get("/api/realtime/sectors")
def api_realtime_sectors(
    codes: Optional[str] = None,
    source: str = "east",
    limit: int = 20,
    include_raw: bool = False,
) -> Any:
    service = _get_realtime_sector_service()
    return JSONResponse(
        service.get_sector_quotes(
            _split_codes(codes) or None,
            source=source,
            limit=max(1, min(int(limit or 20), 100)),
            include_raw=include_raw,
        )
    )


@app.get("/api/realtime/market")
def api_realtime_market(
    codes: Optional[str] = None,
    limit: int = 100,
    include_raw: bool = False,
) -> Any:
    service = _get_realtime_sector_service()
    return JSONResponse(
        service.get_market_quotes(
            _split_codes(codes) or None,
            limit=max(1, min(int(limit or 100), 500)),
            include_raw=include_raw,
        )
    )


@app.get("/api/realtime/health")
def api_realtime_health(probe: bool = False) -> Any:
    return JSONResponse({
        "ok": True,
        "quotes": _get_realtime_quote_service().health(probe=probe),
        "sectors": _get_realtime_sector_service().health(probe=probe),
    })


@app.get("/api/realtime/overlay")
def api_realtime_overlay(
    date: Optional[str] = None,
    profile: str = "",
    limit: int = 20,
    persist: bool = False,
    stale_after_seconds: int = 90,
) -> Any:
    from core.realtime.overlay_service import RealtimeOverlayService

    trade_date = date or _latest_date()
    service = RealtimeOverlayService(
        quote_service=_get_realtime_quote_service(stale_after_seconds=stale_after_seconds),
    )
    payload = service.build_overlay(
        trade_date,
        profile=profile,
        limit=max(1, min(int(limit or 20), 100)),
        persist=bool(persist),
    )
    snapshot = _load_snapshot(trade_date) if trade_date else None
    _enrich_stock_names(payload.get("rows") or [], snapshot, trade_date)
    return JSONResponse(payload)


@app.get("/api/leader-pool")
def api_leader_pool(
    date: Optional[str] = None,
    lookback: int = 5,
    limit: int = 30,
) -> Any:
    from core.realtime.leader_pool_service import LeaderPoolService

    trade_date = date or _latest_date()
    payload = LeaderPoolService().build_pool(
        trade_date,
        lookback=max(1, min(int(lookback or 5), 20)),
        limit=max(1, min(int(limit or 30), 100)),
    )
    snapshot = _load_snapshot(trade_date) if trade_date else None
    _enrich_stock_names(payload.get("rows") or [], snapshot, trade_date)
    return JSONResponse(payload)


@app.get("/api/intraday-strength")
def api_intraday_strength(
    date: Optional[str] = None,
    lookback: int = 5,
    limit: int = 30,
    stale_after_seconds: int = 90,
) -> Any:
    from core.realtime.leader_pool_service import IntradayStrengthService

    trade_date = date or _latest_date()
    service = IntradayStrengthService(
        quote_service=_get_realtime_quote_service(stale_after_seconds=stale_after_seconds),
    )
    payload = service.build(
        trade_date,
        lookback=max(1, min(int(lookback or 5), 20)),
        limit=max(1, min(int(limit or 30), 100)),
    )
    snapshot = _load_snapshot(trade_date) if trade_date else None
    _enrich_stock_names(payload.get("rows") or [], snapshot, trade_date)
    return JSONResponse(payload)


@app.get("/api/etl/screening/{date}")
def api_etl_screening(date: str) -> Any:
    from core.screening.screening_engine import ScreeningEngine

    result = ScreeningEngine().run(date, persist=False)
    return JSONResponse(result.to_dict())


@app.get("/api/etl/analysis/{date}")
def api_etl_analysis(date: str) -> Any:
    from core.screening.gold_analysis import build_gold_analysis_summary

    return JSONResponse(build_gold_analysis_summary(date))


def _load_winrate() -> Optional[Dict]:
    try:
        if WINRATE_PATH.exists():
            return json.loads(WINRATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _normalize_stock_code(code: str) -> str:
    from core.utils.stock_code_utils import StockCodeUtils

    return StockCodeUtils.standardize_code(code, add_suffix=False)


def _find_stock_context(snapshot: Optional[Dict], code: str) -> Dict[str, Any]:
    """Find plan/signal/risk rows for the stock from a snapshot."""
    if not snapshot:
        return {"name": "", "plans": [], "signals": [], "risk": []}
    pure = _normalize_stock_code(code)
    plans = []
    signals = []
    risk = []
    name = ""

    for row in (snapshot.get("trade_plans", {}) or {}).get("rows", []) or []:
        row_code = _normalize_stock_code(row.get("股票代码") or row.get("stock_code") or "")
        if row_code == pure:
            plans.append(row)
            name = name or row.get("股票名称") or row.get("stock_name") or ""

    for section in snapshot.get("sections", []) or []:
        rows = section.get("rows") or []
        for row in rows:
            row_code = _normalize_stock_code(
                row.get("股票代码") or row.get("stock_code") or row.get("代码") or ""
            )
            if row_code != pure:
                continue
            if section.get("kind") == "signals":
                item = dict(row)
                item["_section"] = section.get("name")
                signals.append(item)
            if section.get("name") == "风控闸门":
                risk.append(row)
            name = name or row.get("股票名称") or row.get("stock_name") or row.get("名称") or ""

    return {"name": name, "plans": plans, "signals": signals, "risk": risk}


def _date_to_ts(date_str: Any, time_str: Any = "15:00:00") -> int:
    from datetime import datetime

    date_s = str(date_str or "").replace("-", "")
    if len(date_s) != 8:
        return 0
    time_s = str(time_str or "15:00:00")
    if len(time_s) == 5:
        time_s += ":00"
    try:
        dt = datetime.strptime(f"{date_s} {time_s[:8]}", "%Y%m%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return 0


def _df_to_daily_candles(df) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        date = r.get("trade_date")
        ts = _date_to_ts(date, r.get("time") or "15:00:00")
        if not ts:
            continue
        rows.append({
            "time": ts,
            "open": float(r.get("open") or 0),
            "high": float(r.get("high") or 0),
            "low": float(r.get("low") or 0),
            "close": float(r.get("close") or 0),
        })
    return [r for r in rows if r["open"] or r["close"]]


def _df_to_intraday_line(df, trade_date: str) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        ts = _date_to_ts(r.get("date") or r.get("trade_date") or trade_date, r.get("time") or "09:30:00")
        value = r.get("close")
        if not ts or value is None:
            continue
        rows.append({"time": ts, "value": float(value)})
    return rows


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> Any:
    """参数配置页：把全系统可调参数开放到网页编辑。"""
    from config.config_registry import build_registry

    return templates.TemplateResponse(
        request,
        "config.html",
        {"registry": build_registry(), "dates": _list_dates()},
    )


@app.get("/api/config")
def api_config_get() -> Any:
    from config.config_registry import build_registry

    return JSONResponse(build_registry())


@app.post("/api/config")
def api_config_save(payload: dict = Body(...)) -> Any:
    """保存一批参数改动。body: {updates: [{scope, path, value}, ...]}"""
    from config.config_registry import apply_updates

    updates = (payload or {}).get("updates", [])
    if not isinstance(updates, list):
        return JSONResponse({"error": "updates 必须是数组"}, status_code=400)
    result = apply_updates(updates)
    return JSONResponse(result)


@app.post("/api/config/reset")
def api_config_reset(payload: dict = Body(default={})) -> Any:
    """重置参数。body: {scope?, path?}；都不传则清空全部覆盖。"""
    from config.config_registry import reset

    scope = (payload or {}).get("scope")
    path = (payload or {}).get("path")
    return JSONResponse(reset(scope=scope, path=path))


# ----------------------------------------------------------------------
# 指标因子页面（因子开关 / 情绪方案 / 各策略打分模式）
# 写入复用 /api/config（apply_updates），本页仅提供友好读取 + 编排。
# ----------------------------------------------------------------------
def _latest_active_profile() -> str:
    """从最新快照 meta 读取实际生效的因子 profile（仅展示）。"""
    try:
        latest = _latest_date()
        snap = _load_snapshot(latest) if latest else None
        meta = (snap or {}).get("meta", {}) or {}
        return str(meta.get("factor_profile") or "")
    except Exception:  # noqa: BLE001
        return ""


@app.get("/factors", response_class=HTMLResponse)
def factors_page(request: Request) -> Any:
    """指标因子：因子启用开关 + 情绪周期方案 + 各策略打分模式。"""
    from web.factor_panel import build_factor_state

    state = build_factor_state(active_profile=_latest_active_profile())
    return templates.TemplateResponse(request, "factors.html", {"state": state})


@app.get("/api/factors/state")
def api_factors_state() -> Any:
    from web.factor_panel import build_factor_state

    return JSONResponse(build_factor_state(active_profile=_latest_active_profile()))


@app.get("/data/{cat}", response_class=HTMLResponse)
def data_index(cat: str) -> Any:
    """数据浏览分类入口：跳转到最新交易日。"""
    if cat not in _CATEGORY_BY_KEY:
        return RedirectResponse(url="/")
    latest = _latest_date()
    return RedirectResponse(url=f"/data/{cat}/{latest}" if latest else "/")


@app.get("/data/{cat}/{date}", response_class=HTMLResponse)
def data_browse(request: Request, cat: str, date: str) -> Any:
    """某分类下的 section 浏览页（复用 /report/{date}/section/{idx} 片段）。"""
    category = _CATEGORY_BY_KEY.get(cat)
    if category is None:
        return HTMLResponse('<div class="p-6 text-slate-400">无此分类</div>', status_code=404)
    snapshot = _load_prepared_snapshot(date)
    return templates.TemplateResponse(
        request,
        "data_browse.html",
        {
            "category": category,
            "cat": cat,
            "date": date,
            "dates": _list_dates(),
            "sections": (snapshot or {}).get("sections", []),
            "snapshot": snapshot,
        },
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_index(request: Request) -> Any:
    """问 AI 入口：默认以最新交易日为上下文。"""
    latest = _latest_date()
    if latest:
        return RedirectResponse(url=f"/ask/{latest}")
    return templates.TemplateResponse(request, "ask.html", {"date": None, "dates": []})


@app.get("/ask/{date}", response_class=HTMLResponse)
def ask_page(request: Request, date: str) -> Any:
    return templates.TemplateResponse(
        request, "ask.html", {"date": date, "dates": _list_dates()}
    )


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request, run: Optional[str] = None) -> Any:
    """模拟交易结果：汇总指标 + 净值曲线 + 逐笔交易 + 模式表现。"""
    from desktop.backtest import backtest_overview

    return templates.TemplateResponse(
        request, "backtest.html", {"bt": backtest_overview(run)}
    )


@app.get("/drawdown", response_class=HTMLResponse)
def drawdown_page(request: Request, run: Optional[str] = None) -> Any:
    """回撤分析：水下回撤曲线 + 最大回撤 + 回撤区间 + 最差交易。"""
    from desktop.backtest import drawdown_overview

    return templates.TemplateResponse(
        request, "drawdown.html", {"dd": drawdown_overview(run)}
    )


@app.get("/api/backtest/runs")
def api_backtest_runs() -> Any:
    from desktop.backtest import runs_meta

    return JSONResponse({"runs": runs_meta()})


@app.post("/api/backtest/run")
def api_backtest_run(payload: dict = Body(default={})) -> Any:
    """启动一次回测（重新生成净值/交易/回撤）。

    body:
      - 区间重算: {mode:'range', start_date?, end_date?, initial_capital?, risk_control?}
      - 单日接力: {mode:'daily', trade_date, initial_capital?, risk_control?, reset_state?}
    risk_control 缺省时回退到全局 RiskConfig.enabled。
    """
    from desktop.runner import BACKTEST_CONTROLLER

    p = payload or {}
    ok, msg = BACKTEST_CONTROLLER.start(
        p.get("start_date"), p.get("end_date"), p.get("initial_capital"),
        risk_control=p.get("risk_control"),
        mode=p.get("mode") or "range",
        trade_date=p.get("trade_date"),
        reset_state=p.get("reset_state"))
    return JSONResponse({"started": ok, "message": msg})


@app.get("/api/backtest/run/status")
def api_backtest_run_status(since: int = 0) -> Any:
    from desktop.runner import BACKTEST_CONTROLLER

    return JSONResponse(BACKTEST_CONTROLLER.status(since))


@app.get("/report/{date}/section/{idx}", response_class=HTMLResponse)
def section_fragment(request: Request, date: str, idx: int, view: str = "table") -> Any:
    """HTMX 片段：返回某个 section 的 HTML。

    view=detail → 折叠主从卡片（紧凑列表 + 点击展开全字段，避免横向滚动），
    用于字段很多的指标筛选 / 板块热度；其余分类默认 view=table 宽表。
    """
    snapshot = _load_prepared_snapshot(date)
    sections: List[Dict] = (snapshot or {}).get("sections", [])
    if not snapshot or idx < 0 or idx >= len(sections):
        return HTMLResponse('<div class="p-6 text-slate-400">无此数据</div>', status_code=404)
    section = sections[idx]
    return templates.TemplateResponse(
        request,
        "partials/section.html",
        {"section": section, "view": view},
    )


# ----------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------
@app.get("/api/dates")
def api_dates() -> Any:
    return JSONResponse({"dates": _list_dates(), "latest": _latest_date()})


@app.get("/api/winrate")
def api_winrate() -> Any:
    data = _load_winrate()
    if data is None:
        return JSONResponse({"error": "not built", "hint": "运行 python scripts/build_winrate.py"},
                            status_code=404)
    return JSONResponse(data)


@app.get("/api/snapshot/{date}")
def api_snapshot(date: str) -> Any:
    snapshot = _load_prepared_snapshot(date)
    if snapshot is None:
        return JSONResponse({"error": "not found", "date": date}, status_code=404)
    return JSONResponse(snapshot)


# ----------------------------------------------------------------------
# P3：AI 每日解读 + 问答
# ----------------------------------------------------------------------
@app.get("/report/{date}/brief", response_class=HTMLResponse)
def brief_fragment(request: Request, date: str) -> Any:
    """HTMX 片段：当日 AI 解读（缓存优先；未配置 key 时返回提示）。"""
    from kb.brief import generate_brief

    snapshot = _load_snapshot(date)
    if snapshot is None:
        return HTMLResponse('<div class="text-slate-500 text-sm">无快照</div>')
    result = generate_brief(snapshot, KB_DB_PATH)
    return templates.TemplateResponse(request, "partials/brief.html", {"brief": result})


@app.get("/api/daily-brief/{date}")
def api_daily_brief(date: str, force: bool = False) -> Any:
    from kb.brief import generate_brief

    snapshot = _load_snapshot(date)
    if snapshot is None:
        return JSONResponse({"error": "not found", "date": date}, status_code=404)
    return JSONResponse(generate_brief(snapshot, KB_DB_PATH, force=force))


@app.post("/api/chat")
def api_chat(payload: dict = Body(...)) -> Any:
    """RAG 问答，SSE 流式返回。body: {question, date?}"""
    from kb.embeddings import get_embedder
    from kb.llm_client import get_llm_client
    from kb.qa import build_chat_messages

    question = (payload or {}).get("question", "").strip()
    date = (payload or {}).get("date")
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    messages, _debug = build_chat_messages(
        question, KB_DB_PATH, APP_DB_PATH, embedder=get_embedder(), date=date)
    client = get_llm_client()

    def event_stream():
        for piece in client.chat_stream(messages):
            yield f"data: {json.dumps({'delta': piece}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
