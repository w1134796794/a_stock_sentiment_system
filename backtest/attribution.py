"""Attribution reports for factor-driven backtests."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd

from core.screening.explanations import FACTOR_LABELS


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_metrics(value: Any) -> Dict[str, float]:
    if isinstance(value, dict):
        return {str(k): _to_float(v) for k, v in value.items()}
    text = str(value or "").strip()
    if not text or text.lower() in ("nan", "none"):
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): _to_float(v) for k, v in data.items()}


def _rate(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float(series.mean())


def _sum(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float(series.sum())


def _mean(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    return float(series.mean())


def _closed_trade_rows(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for t in result.get("trade_history") or []:
        action = str(_attr(t, "action", "") or "")
        if not action.upper().startswith("SELL"):
            continue
        metrics = _parse_metrics(_attr(t, "factor_metrics_json", ""))
        rows.append({
            "exit_date": _attr(t, "date", ""),
            "entry_date": _attr(t, "entry_date", ""),
            "stock_code": _attr(t, "stock_code", ""),
            "stock_name": _attr(t, "stock_name", ""),
            "pattern_type": _attr(t, "pattern_type", ""),
            "action": action,
            "exit_reason": _attr(t, "exit_reason", ""),
            "shares": _attr(t, "shares", 0),
            "pnl": _to_float(_attr(t, "pnl", 0.0)),
            "pnl_pct": _to_float(_attr(t, "pnl_pct", 0.0)),
            "holding_days": _attr(t, "holding_days", 0),
            "stop_loss_triggered": bool(_attr(t, "stop_loss_triggered", False)),
            "take_profit_triggered": bool(_attr(t, "take_profit_triggered", False)),
            "plan_rank": int(_to_float(_attr(t, "plan_rank", 0), 0)),
            "plan_score": _to_float(_attr(t, "plan_score", 0.0)),
            "plan_reason": _attr(t, "plan_reason", ""),
            "factor_metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            **{f"factor_{k}": v for k, v in metrics.items()},
        })
    return rows


def build_attribution_frames(result: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    detail = pd.DataFrame(_closed_trade_rows(result))
    if detail.empty:
        return {
            "attribution": detail,
            "factor_feedback": pd.DataFrame(),
            "rank_feedback": pd.DataFrame(),
        }

    factor_records: List[Dict[str, Any]] = []
    for _, row in detail.iterrows():
        metrics = _parse_metrics(row.get("factor_metrics_json"))
        for factor_id, score in metrics.items():
            factor_records.append({
                "factor_id": factor_id,
                "factor_name": FACTOR_LABELS.get(factor_id, factor_id),
                "score": score,
                "is_weak": score < 45,
                "pnl": row["pnl"],
                "pnl_pct": row["pnl_pct"],
                "win": row["pnl"] > 0,
                "stop_loss": bool(row["stop_loss_triggered"]),
                "take_profit": bool(row["take_profit_triggered"]),
            })

    factor_feedback = pd.DataFrame()
    if factor_records:
        raw = pd.DataFrame(factor_records)
        grouped = raw.groupby(["factor_id", "factor_name"], dropna=False)
        rows = []
        for (factor_id, factor_name), g in grouped:
            weak = g[g["score"] < 45]
            strong = g[g["score"] >= 75]
            mid = g[(g["score"] >= 45) & (g["score"] < 75)]
            rows.append({
                "factor_id": factor_id,
                "factor_name": factor_name,
                "sample_count": int(len(g)),
                "win_rate": _rate(g["win"]),
                "total_pnl": _sum(g["pnl"]),
                "avg_pnl_pct": _mean(g["pnl_pct"]),
                "stop_loss_rate": _rate(g["stop_loss"]),
                "take_profit_rate": _rate(g["take_profit"]),
                "avg_score": _mean(g["score"]),
                "strong_count": int(len(strong)),
                "strong_total_pnl": _sum(strong["pnl"]),
                "strong_avg_pnl_pct": _mean(strong["pnl_pct"]),
                "strong_win_rate": _rate(strong["win"]),
                "strong_stop_loss_rate": _rate(strong["stop_loss"]),
                "mid_count": int(len(mid)),
                "mid_total_pnl": _sum(mid["pnl"]),
                "weak_count": int(len(weak)),
                "weak_total_pnl": _sum(weak["pnl"]),
                "weak_avg_pnl_pct": _mean(weak["pnl_pct"]),
                "weak_win_rate": _rate(weak["win"]),
                "weak_stop_loss_rate": _rate(weak["stop_loss"]),
                "weak_stop_loss_count": int(weak["stop_loss"].sum()) if not weak.empty else 0,
            })
        factor_feedback = pd.DataFrame(rows)
        factor_feedback["strong_minus_weak_pnl"] = (
            factor_feedback["strong_total_pnl"] - factor_feedback["weak_total_pnl"]
        )
        factor_feedback["weak_stop_loss_excess"] = (
            factor_feedback["weak_stop_loss_rate"] - factor_feedback["stop_loss_rate"]
        )
        factor_feedback["feedback"] = factor_feedback.apply(_feedback_label, axis=1)
        factor_feedback = factor_feedback.sort_values(
            ["weak_total_pnl", "weak_stop_loss_rate", "strong_total_pnl"],
            ascending=[True, False, False],
        ).reset_index(drop=True)

    rank_feedback = pd.DataFrame()
    if "plan_rank" in detail.columns:
        rows = []
        for rank, g in detail.groupby("plan_rank"):
            if not rank:
                continue
            rows.append({
                "plan_rank": int(rank),
                "sample_count": int(len(g)),
                "win_rate": float((g["pnl"] > 0).mean()),
                "total_pnl": float(g["pnl"].sum()),
                "avg_pnl_pct": float(g["pnl_pct"].mean()),
                "stop_loss_rate": float(g["stop_loss_triggered"].mean()),
                "take_profit_rate": float(g["take_profit_triggered"].mean()),
                "avg_score": float(g["plan_score"].mean()),
            })
        rank_feedback = pd.DataFrame(rows).sort_values("plan_rank").reset_index(drop=True)

    return {
        "attribution": detail,
        "factor_feedback": factor_feedback,
        "rank_feedback": rank_feedback,
    }


def _feedback_label(row: pd.Series) -> str:
    weak_count = row.get("weak_count", 0)
    strong_count = row.get("strong_count", 0)
    weak_pnl = row.get("weak_total_pnl", 0)
    strong_pnl = row.get("strong_total_pnl", 0)
    weak_stop_excess = row.get("weak_stop_loss_excess", 0)
    weak_stop_rate = row.get("weak_stop_loss_rate", 0)
    strong_win_rate = row.get("strong_win_rate", 0)

    if weak_count >= 3 and weak_pnl < 0 and (weak_stop_rate >= 0.45 or weak_stop_excess >= 0.15):
        return "弱项导致止损"
    if strong_count >= 3 and strong_pnl > 0 and strong_win_rate >= 0.55:
        return "强项正贡献"
    if strong_count >= 3 and strong_pnl < 0 and row.get("strong_stop_loss_rate", 0) >= row.get("stop_loss_rate", 0):
        return "高分失效"
    if weak_count >= 3 and weak_pnl > 0 and weak_stop_excess <= 0:
        return "弱项暂未验证为风险"
    if weak_count >= 3 and weak_pnl < 0:
        return "弱项负贡献"
    return "中性观察"
