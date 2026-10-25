"""Causal, display-only sector review scores. Never written to strategy factors."""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import defaultdict

VERSION = "sector_review_v2"
WEIGHTS = {"持续活跃": 0.35, "相对强度": 0.30, "量能持续": 0.20, "涨停扩散": 0.15}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def scale(value, low, high):
    return max(0.0, min(100.0, (value - low) / (high - low) * 100)) if value is not None else None


def score_number(value):
    value = number(value)
    return value if value is not None and 0 <= value <= 100 else None


def identity(row):
    code = str(row.get("sector_code") or row.get("板块代码") or "").strip()
    name = str(row.get("sector_name") or row.get("板块名称") or "").strip()
    kind = str(row.get("sector_type") or row.get("板块类型") or "")
    return code, name, kind


def concept(row):
    code, name, kind = identity(row)
    return bool(re.fullmatch(r"8\d{5}(?:\.TI)?", code) and name and kind in {"N", "概念"})


def family(name):
    value = unicodedata.normalize("NFKC", name).replace(" ", "")
    value = re.sub(r"\(A股\)$", "", value)
    return re.sub(r"概念$", "", value)


def compact(rows, score_key, limit):
    """Fold only conservative naming variants; keep all source codes and scores."""
    groups = {}
    for row in sorted(
        rows,
        key=lambda r: (
            not r.get("_review_qualified", True),
            r.get("_review_status") == "数据不足",
            -(number(r.get(score_key)) or 0),
            identity(r)[0],
        ),
    ):
        key = family(identity(row)[1])
        if key in groups:
            groups[key]["_review_aliases"].append(
                {"code": identity(row)[0], "name": identity(row)[1], "score": row.get(score_key)}
            )
        else:
            groups[key] = {**row, "_review_aliases": []}
    selected = list(groups.values())[:limit]
    for rank, row in enumerate(selected, 1):
        row["_review_rank"] = rank
        row["_review_version"] = VERSION
    return selected


def hotspots(rows, limit=20):
    result = []
    for source in rows:
        if not concept(source):
            continue
        row = dict(source)
        parts = {
            "当日动量": (score_number(row.get("momentum_score")), 0.45),
            "量能变化": (score_number(row.get("amount_ratio_score")), 0.25),
            "成交活跃": (score_number(row.get("amount_score")), 0.15),
        }
        if row.get("membership_available") in (True, 1, "1"):
            parts["涨停扩散"] = (score_number(row.get("limit_up_diffusion_score")), 0.15)
        valid = {key: (value, weight) for key, (value, weight) in parts.items() if value is not None}
        weight = sum(pair[1] for pair in valid.values())
        row["_review_score"] = round(sum(value * w for value, w in valid.values()) / weight, 2) if weight else None
        row["_review_parts"] = [
            {"name": key, "score": round(value, 2), "weight": w} for key, (value, w) in valid.items()
        ]
        row["_review_score_coverage"] = round(weight * 100)
        row["_review_status"] = "当日热点" if weight else "数据不足"
        result.append(row)
    return compact(result, "_review_score", limit)


def mainlines(current_rows, history_rows, *, as_of, expected_dates, limit=20):
    dates = sorted({str(date) for date in expected_dates if str(date) <= as_of})[-10:]
    if not dates or dates[-1] != as_of:
        return []
    eligible = {identity(row)[0]: dict(row) for row in current_rows if concept(row)}
    # Conflicting duplicates are unusable; never choose a convenient revision.
    observations = defaultdict(list)
    for row in history_rows:
        code = str(row.get("sector_code") or "")
        date = str(row.get("trade_date") or "")
        pct = number(row.get("pct_chg"))
        if code in eligible and date in dates and pct is not None and pct > -100:
            observations[(date, code)].append(row)
    days = defaultdict(dict)
    for (date, code), values in observations.items():
        signatures = {(number(row.get("pct_chg")), number(row.get("amount_yuan"))) for row in values}
        if len(signatures) == 1:
            days[date][code] = values[0]
    # Relative ranking needs a representative peer set, not a few surviving rows.
    minimum_peers = max(2, math.ceil(len(eligible) * 0.60))
    days = defaultdict(dict, {date: rows for date, rows in days.items() if len(rows) >= minimum_peers})
    medians = {
        date: statistics.median(number(row["pct_chg"]) for row in rows.values()) for date, rows in days.items() if rows
    }
    top_codes = {}
    for date, rows in days.items():
        ranked = sorted(rows, key=lambda code: (-number(rows[code]["pct_chg"]), code))
        top_codes[date] = set(ranked[: max(1, math.ceil(len(ranked) * 0.20))])
    result = []
    for code, row in eligible.items():
        history = [(date, days[date][code]) for date in dates if code in days[date]]
        n = len(history)
        if not n or history[-1][0] != as_of:
            continue
        coverage = n / len(dates)
        hot_days = sum(code in top_codes.get(date, set()) for date, _ in history)
        positive_relative = sum(number(item["pct_chg"]) > medians[date] for date, item in history)
        continuity = 100 * (0.6 * hot_days / n + 0.4 * positive_relative / n)
        growth = math.prod(1 + number(item["pct_chg"]) / 100 for _, item in history)
        benchmark = math.prod(1 + medians[date] / 100 for date, _ in history)
        excess = (growth / benchmark - 1) * 100
        amounts = [number(item.get("amount_yuan")) for _, item in history[-6:]]
        ratio = None
        if len(amounts) == 6 and all(value is not None and value > 0 for value in amounts):
            ratio = statistics.median(amounts[-3:]) / statistics.median(amounts[:3])
        parts = {"持续活跃": continuity, "相对强度": scale(excess, -5, 10), "量能持续": scale(ratio, 0.7, 1.6)}
        parts["涨停扩散"] = (
            score_number(row.get("limit_up_diffusion_score"))
            if row.get("membership_available") in (True, 1, "1")
            else None
        )
        available = sum(WEIGHTS[key] for key, value in parts.items() if value is not None)
        score = (
            sum(value * WEIGHTS[key] for key, value in parts.items() if value is not None) / available
            if available
            else None
        )
        complete = n >= 6 and coverage >= 0.8 and available >= 0.8
        qualified = complete and score >= 65 and continuity >= 50 and excess > 0
        row.update(
            {
                "板块名称": identity(row)[1],
                "板块代码": code,
                "板块类型": "概念",
                "主线主题评分": round(score, 2) if score is not None else None,
                "_review_score": round(score, 2) if score is not None else None,
                "_review_status": "持续主线" if qualified else "轮动观察" if complete else "数据不足",
                "_review_qualified": qualified,
                "_review_days": n,
                "_review_expected_days": len(dates),
                "_review_history_coverage": round(coverage * 100),
                "_review_score_coverage": round(available * 100),
                "_review_excess_pct": round(excess, 2),
                "_review_amount_ratio": round(ratio, 3) if ratio is not None else None,
                "近10日上榜天数": hot_days,
                "近10日红盘天数": sum(number(item["pct_chg"]) > 0 for _, item in history),
                "近10日累计涨幅%": round((growth - 1) * 100, 2),
                "_review_parts": [
                    {"name": key, "score": round(value, 2) if value is not None else None, "weight": WEIGHTS[key]}
                    for key, value in parts.items()
                ],
            }
        )
        result.append(row)
    # Evidence-qualified rows precede rotation candidates, regardless of raw score.
    collapsed = compact(result, "_review_score", len(result))
    collapsed.sort(
        key=lambda row: (
            not row["_review_qualified"],
            row["_review_status"] == "数据不足",
            -(row["_review_score"] or 0),
        )
    )
    for rank, row in enumerate(collapsed, 1):
        row["_review_rank"] = rank
    return collapsed[:limit]
