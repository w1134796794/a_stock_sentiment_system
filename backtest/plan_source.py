"""Build backtest-compatible trade plan CSVs from current snapshot artifacts."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


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


def _snapshot_date(path: Path, payload: Dict[str, Any]) -> str:
    date = ((payload.get("meta") or {}).get("date") or payload.get("date") or path.stem)
    return str(date).replace("-", "")[:8]


def _rows_from_snapshot(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = list(((payload.get("trade_plans") or {}).get("rows") or []))
    return [
        row for row in rows
        if "指标筛选" in str(row.get("模式类型") or row.get("pattern_type") or "")
    ]


def _rows_from_screening(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    screening = ((payload.get("etl") or {}).get("screening") or {})
    rows = []
    for item in screening.get("final") or []:
        rows.append({
            "股票代码": item.get("code") or item.get("ts_code"),
            "股票名称": item.get("name"),
            "模式类型": f"指标筛选/{screening.get('profile') or 'default'}",
            "优先级": item.get("rank"),
            "综合评分": item.get("score"),
            "建议仓位": "中性 20%-30%",
            "入场区间": "竞价高开且实时确认后",
            "竞价条件": "高开才买入，低开直接放弃",
            "风险提示": "实时行情为取消/观察时不主动买入",
            "筛选理由": "；".join(str(x) for x in (item.get("reasons") or [])[:5]),
        })
    return rows


def _to_backtest_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = _code6(row.get("股票代码") or row.get("code") or row.get("ts_code"))
    name = str(row.get("股票名称") or row.get("name") or "").strip()
    if not code or not name:
        return None

    mode = str(row.get("模式类型") or row.get("pattern_type") or "指标筛选/default")
    score = row.get("综合评分") if row.get("综合评分") is not None else row.get("score")
    reason = str(row.get("筛选理由") or row.get("reason") or "")
    entry = str(row.get("入场区间") or "竞价高开且实时确认后")
    condition = str(row.get("竞价条件") or "高开才买入，低开直接放弃")
    cancel = str(row.get("风险提示") or "低开/平开直接放弃")
    position = _position(row.get("建议仓位") or row.get("position"))

    return {
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
        "共振板块": "",
        "综合评分": score or 0,
        "优先级": row.get("优先级") or row.get("rank") or "",
        "所属板块": "",
    }


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_backtest_plan_dir(
    *,
    snapshot_dir: Path,
    output_dir: Path,
    screening_dir: Optional[Path] = None,
    start_date: str = "",
    end_date: str = "",
) -> Tuple[Path, int, int]:
    """Create a clean plan directory for BacktestEngine from current artifacts.

    Returns:
        (plan_dir, file_count, row_count)
    """
    plan_dir = output_dir / "backtest_trade_plans"
    if plan_dir.exists():
        shutil.rmtree(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)

    start = str(start_date or "")
    end = str(end_date or "")
    file_count = 0
    row_count = 0

    for path in sorted(Path(snapshot_dir).glob("*.json")):
        payload = _load_json(path)
        if not payload:
            continue
        date = _snapshot_date(path, payload)
        if start and date < start:
            continue
        if end and date > end:
            continue

        rows = _rows_from_screening(payload) or _rows_from_snapshot(payload)
        bt_rows = [x for x in (_to_backtest_row(row) for row in rows) if x]

        if not bt_rows and screening_dir:
            screening_path = Path(screening_dir) / f"screening_{date}.json"
            screening = _load_json(screening_path)
            fallback_payload = {"etl": {"screening": screening}}
            bt_rows = [x for x in (_to_backtest_row(row) for row in _rows_from_screening(fallback_payload)) if x]

        if not bt_rows:
            continue

        df = pd.DataFrame(bt_rows)
        df.to_csv(plan_dir / f"交易计划_{date}.csv", index=False, encoding="utf-8-sig")
        file_count += 1
        row_count += len(bt_rows)

    return plan_dir, file_count, row_count
