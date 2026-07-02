"""Build backtest-compatible trade plan CSVs from current snapshot artifacts."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from core.screening.enhancements import enhancement_label, enhancement_slug, normalize_enhancements

DEFAULT_MAX_BACKTEST_RANK = 0


def _code6(value: Any) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".")[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits.zfill(6) if digits else ""


def _position(value: Any) -> str:
    text = str(value or "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    max_pct = max(nums) if nums else 0.0
    if "重" in text or max_pct >= 40:
        return "heavy"
    if "中" in text or max_pct >= 20:
        return "medium"
    return "light"


def _score_to_confidence(value: Any) -> float:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        score = 0.0
    if score <= 1:
        return round(max(score, 0.0), 4)
    return round(max(min(score / 100.0, 1.0), 0.0), 4)


def _to_number(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rank_value(row: Dict[str, Any]) -> Optional[int]:
    value = row.get("优先级") if row.get("优先级") not in (None, "") else row.get("rank")
    try:
        if value is None or pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _snapshot_date(path: Path, payload: Dict[str, Any]) -> str:
    date = ((payload.get("meta") or {}).get("date") or payload.get("date") or path.stem)
    return str(date).replace("-", "")[:8]


def _rows_from_snapshot(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = list(((payload.get("trade_plans") or {}).get("rows") or []))
    return [
        row for row in rows
        if "指标筛选" in str(row.get("模式类型") or row.get("pattern_type") or "")
    ]


def _rows_from_screening(
    payload: Dict[str, Any], *, lhb_scenario: str = "", enhancements: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    screening = ((payload.get("etl") or {}).get("screening") or {})
    rows = []
    selected_enhancements = normalize_enhancements(enhancements)
    if enhancements is not None and not lhb_scenario:
        pool = screening.get("candidate_pool") or screening.get("final") or []
        items = []
        for item in pool:
            candidate = dict(item)
            base = _to_number(candidate.get("model_baseline_score"), _to_number(candidate.get("score")))
            parts = dict(candidate.get("enhancements") or {})
            candidate["score"] = round(base + sum(_to_number(parts.get(key)) for key in selected_enhancements), 4)
            items.append(candidate)
        items = sorted(items, key=lambda item: _to_number(item.get("score")), reverse=True)
        top_n = max(1, len(screening.get("final") or []) or 10)
        items = items[:top_n]
        for rank, item in enumerate(items, start=1):
            item["rank"] = rank
    else:
        selected = ((screening.get("scenarios") or {}).get(lhb_scenario) if lhb_scenario else None)
        items = selected if isinstance(selected, list) else (screening.get("final") or [])
    for item in items:
        metrics = dict(item.get("metrics") or {})
        context = dict(item.get("context") or {})
        row = {
            "股票代码": item.get("code") or item.get("ts_code"),
            "股票名称": item.get("name"),
            "模式类型": f"指标筛选/{screening.get('profile') or 'default'}",
            "优先级": item.get("rank"),
            "综合评分": item.get("score"),
            "建议仓位": "中性 20%-30%",
            "入场区间": "弱转强/强势延续/高开加速按分钟确认",
            "竞价条件": "开盘仅用于信号分层，10:00前按一分钟行情确认",
            "风险提示": "未确认或信号出现后无可成交分钟则不买入",
            "共振板块": item.get("resonance_sectors") or "",
            "所属板块": item.get("resonance_sectors") or "",
            "筛选理由": "；".join(str(x) for x in (item.get("reasons") or [])[:5]),
            "惩罚理由": "；".join(str(x) for x in (item.get("penalty_reasons") or [])[:5]),
            "龙虎榜口径": lhb_scenario or "lhb_sector",
            "回测增强组合": enhancement_label(selected_enhancements),
        }
        for factor, value in metrics.items():
            row[f"因子_{factor}"] = value
        for key, value in context.items():
            row[f"原始_{key}"] = value
        rows.append(row)
    return rows


def _attach_market_context(rows: List[Dict[str, Any]], payload: Dict[str, Any]) -> None:
    """Attach point-in-time market context to every plan row."""
    market = (((payload.get("etl") or {}).get("gold_summary") or {}).get("market") or {})
    if not market:
        market = payload.get("market") or {}
    mapping = {
        "原始_mkt_market_score": market.get("market_score"),
        "原始_mkt_width_score": market.get("width_score"),
        "原始_mkt_emotion_score": market.get("emotion_score"),
    }
    for row in rows:
        for key, value in mapping.items():
            if value is not None and row.get(key) in (None, ""):
                row[key] = value


def _to_backtest_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = _code6(row.get("股票代码") or row.get("code") or row.get("ts_code"))
    name = str(row.get("股票名称") or row.get("name") or "").strip()
    if not code or not name:
        return None

    mode = str(row.get("模式类型") or row.get("pattern_type") or "指标筛选/default")
    score = row.get("综合评分") if row.get("综合评分") is not None else row.get("score")
    reason = str(row.get("筛选理由") or row.get("reason") or "")
    entry = str(row.get("入场区间") or "弱转强/强势延续/高开加速按分钟确认")
    condition = str(row.get("竞价条件") or "开盘仅用于信号分层，10:00前按一分钟行情确认")
    cancel = str(row.get("风险提示") or "未确认或无可成交分钟则不买入")
    position = _position(row.get("建议仓位") or row.get("position"))
    factor_metrics = {
        key.replace("因子_", "", 1): _to_number(value)
        for key, value in row.items()
        if str(key).startswith("因子_")
    }
    raw_context = {
        key.replace("原始_", "", 1): _to_number(value)
        for key, value in row.items()
        if str(key).startswith("原始_")
    }

    out = {
        "模式": mode,
        "代码": code,
        "名称": name,
        "动作": "买入",
        "介入时机": entry,
        "目标价": 0.0,
        "止损价": 0.0,
        "止盈价": 0.0,
        "仓位": position,
        "前置条件": condition,
        "取消条件": cancel,
        "置信度": _score_to_confidence(score),
        "理由": reason,
        "加入观察池": True,
        "热点共振": False,
        "共振板块": row.get("共振板块") or row.get("所属板块") or "",
        "综合评分": score or 0,
        "优先级": row.get("优先级") or row.get("rank") or "",
        "所属板块": row.get("所属板块") or row.get("共振板块") or "",
        "惩罚理由": row.get("惩罚理由") or "",
        "因子指标": json.dumps(factor_metrics, ensure_ascii=False, sort_keys=True),
        "原始指标": json.dumps(raw_context, ensure_ascii=False, sort_keys=True),
        "回测增强组合": row.get("回测增强组合") or "基线",
    }
    for factor, value in factor_metrics.items():
        out[f"因子_{factor}"] = value
    for key, value in raw_context.items():
        out[f"原始_{key}"] = value
    return out


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _has_enhancement_data(screening: Dict[str, Any], selected: List[str]) -> bool:
    if not selected:
        return True
    if int(_to_number(screening.get("enhancement_schema_version"), 0)) < 1:
        return False
    pool = screening.get("candidate_pool") or []
    if not isinstance(pool, list):
        return False
    if not pool:
        return True
    return all(
        isinstance(item, dict)
        and isinstance(item.get("enhancements"), dict)
        and all(key in item["enhancements"] for key in selected)
        for item in pool
    )


def build_backtest_plan_dir(
    *,
    snapshot_dir: Path,
    output_dir: Path,
    screening_dir: Optional[Path] = None,
    start_date: str = "",
    end_date: str = "",
    max_rank: int = DEFAULT_MAX_BACKTEST_RANK,
    lhb_scenario: str = "",
    enhancements: Optional[Iterable[str]] = None,
) -> Tuple[Path, int, int]:
    """Create a clean plan directory for BacktestEngine from current artifacts.

    Returns:
        (plan_dir, file_count, row_count)
    """
    suffix = f"_{lhb_scenario}" if lhb_scenario else f"_{enhancement_slug(enhancements)}"
    plan_dir = output_dir / f"backtest_trade_plans{suffix}"
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)

    start = str(start_date or "")
    end = str(end_date or "")
    file_count = 0
    row_count = 0
    missing_enhancement_dates: List[str] = []
    selected_enhancements = normalize_enhancements(enhancements)

    for path in sorted(Path(snapshot_dir).glob("*.json")):
        payload = _load_json(path)
        if not payload:
            continue
        date = _snapshot_date(path, payload)
        if start and date < start:
            continue
        if end and date > end:
            continue

        rows: List[Dict[str, Any]] = []
        screening = {}
        enhancement_source_found = False
        if screening_dir:
            screening_path = Path(screening_dir) / f"screening_{date}.json"
            screening = _load_json(screening_path)
            enhancement_source_found = bool(screening)
            if selected_enhancements and screening and not _has_enhancement_data(screening, selected_enhancements):
                missing_enhancement_dates.append(date)
                continue
            rows = _rows_from_screening(
                {"etl": {"screening": screening}}, lhb_scenario=lhb_scenario,
                enhancements=enhancements,
            )
        if not rows:
            embedded = ((payload.get("etl") or {}).get("screening") or {})
            enhancement_source_found = enhancement_source_found or bool(embedded)
            if selected_enhancements and embedded and not _has_enhancement_data(embedded, selected_enhancements):
                missing_enhancement_dates.append(date)
                continue
            rows = _rows_from_screening(payload, lhb_scenario=lhb_scenario, enhancements=enhancements)
        if not rows:
            if selected_enhancements and not enhancement_source_found:
                missing_enhancement_dates.append(date)
                continue
            rows = _rows_from_snapshot(payload)
        _attach_market_context(rows, payload)
        if max_rank and max_rank > 0:
            rows = [row for row in rows if (_rank_value(row) or 999999) <= max_rank]
        bt_rows = [x for x in (_to_backtest_row(row) for row in rows) if x]

        if not bt_rows and screening_dir:
            screening_path = Path(screening_dir) / f"screening_{date}.json"
            screening = _load_json(screening_path)
            fallback_payload = {"etl": {"screening": screening}}
            rows = _rows_from_screening(
                fallback_payload, lhb_scenario=lhb_scenario, enhancements=enhancements,
            )
            _attach_market_context(rows, payload)
            if max_rank and max_rank > 0:
                rows = [row for row in rows if (_rank_value(row) or 999999) <= max_rank]
            bt_rows = [x for x in (_to_backtest_row(row) for row in rows) if x]

        if not bt_rows:
            continue

        df = pd.DataFrame(bt_rows)
        if "优先级" in df.columns:
            df["_rank"] = pd.to_numeric(df["优先级"], errors="coerce").fillna(999999)
            df = df.sort_values(["_rank", "综合评分"], ascending=[True, False]).drop(columns=["_rank"])
        df.to_csv(plan_dir / f"交易计划_{date}.csv", index=False, encoding="utf-8-sig")
        file_count += 1
        row_count += len(bt_rows)

    if missing_enhancement_dates:
        preview = ", ".join(missing_enhancement_dates[:8])
        suffix_text = "..." if len(missing_enhancement_dates) > 8 else ""
        raise ValueError(
            f"所选增强组合缺少历史因子数据：{preview}{suffix_text}；"
            "请先在‘生成数据’中批量重跑这些交易日"
        )

    return plan_dir, file_count, row_count
