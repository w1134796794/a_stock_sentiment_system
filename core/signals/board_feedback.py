"""Point-in-time veto and rebound exit after a three-limit-up failure."""
from __future__ import annotations

import math
from collections.abc import Mapping

from core.signals.opening import positive_price
from core.utils.price_limit import get_price_limit_pct, limit_up_price, normalize_code

FEEDBACK_VERSION = "three_board_feedback_v1"


def feedback_policy(config=None):
    def get(key, default):
        return config.get(key, default) if isinstance(config, Mapping) else getattr(config, key, default)

    fraction = positive_price(get("three_board_feedback_limit_fraction", .5))
    rebound = positive_price(get("three_board_rebound_exit_gain", .02))
    if fraction is None or fraction > 1 or rebound is None or rebound > 1:
        raise ValueError("三板负反馈阈值必须在(0, 1]内")
    enabled = get("three_board_feedback_enabled", None)
    if enabled is None:
        enabled = get("production_rules", False)
    return {"enabled": bool(enabled),
            "loss_limit_fraction": fraction, "rebound_gain": rebound}


def history_dates(market_date, calendar):
    dates = []
    day = market_date
    for _ in range(6):
        day = calendar.prev(day)
        dates.append(day)
    return list(reversed(dates))


def date_text(value):
    return str(value or "").replace("-", "")[:8]


def assess_feedback(inputs, code, market_date, policy):
    """Only previous completed sessions are eligible; D4 itself is never vetoed."""
    code = normalize_code(code)
    base = {"version": FEEDBACK_VERSION, "market_date": market_date, "code": code,
            "policy": dict(policy), "active": False, "assessed": True,
            "source_basis": (inputs or {}).get("source_basis", "missing_history")}
    if not policy["enabled"]:
        return {**base, "reason": "三板负反馈门禁未启用"}
    inputs = inputs or {}
    dates = inputs.get("dates") or []
    if (inputs.get("market_date") != market_date or not inputs.get("calendar_verified")
            or len(dates) != 6 or dates != sorted(set(dates)) or any(d >= market_date for d in dates)):
        return {**base, "assessed": False, "reason": "缺少三板负反馈历史日线或真实交易日历"}
    records, conflicts = {}, set()
    for row in (inputs.get("stocks") or {}).get(code, []):
        day = date_text(row.get("trade_date"))
        if day not in dates or normalize_code(row.get("code") or code) != code:
            continue
        facts = (positive_price(row.get("close")), positive_price(row.get("pre_close")), str(row.get("name") or ""))
        if day in records and facts != records[day]:
            conflicts.add(day)
        records[day] = facts

    def prices(day):
        values = records.get(day)
        return values if values and day not in conflicts and all(values[:2]) else None

    def sealed(day):
        values = prices(day)
        if not values:
            return None
        close, previous, name = values
        target = limit_up_price(previous, code, name)
        return target is not None and abs(close - target) < .005

    missing = set()
    for age in (1, 2):
        index = len(dates) - age
        failure_date = dates[index]
        board_dates = dates[index - 3:index]
        flags = [sealed(day) for day in board_dates]
        prior_date = dates[index - 4]
        prior_flag = sealed(prior_date)
        if False in flags or prior_flag is True:
            continue
        values = prices(failure_date)
        if not values:
            missing.add(failure_date)
            continue
        close, previous, name = values
        limit = get_price_limit_pct(code, name, previous)
        loss = close / previous - 1
        if limit is None or loss > -limit * policy["loss_limit_fraction"] + 1e-10:
            continue
        if None in flags or prior_flag is None:
            missing.update(day for day in [prior_date, *board_dates] if sealed(day) is None)
            continue
        board_day = 4 + age
        return {**base, "active": True, "board_day": board_day,
                "board_dates": board_dates, "third_board_date": board_dates[-1],
                "feedback_date": failure_date, "feedback_close": close,
                "feedback_pre_close": previous, "feedback_change_pct": round(loss * 100, 4),
                "feedback_threshold_pct": -limit * policy["loss_limit_fraction"] * 100,
                "known_after": failure_date + " 15:00:00",
                "reason": f"三连板后第4日({failure_date})收盘{loss * 100:+.2f}%，第{board_day}个交易日禁止新买入"}
    if missing:
        return {**base, "assessed": False, "missing_dates": sorted(missing),
                "reason": "缺少三板负反馈历史日线，不能确认是否处于禁入期"}
    return {**base, "reason": "未命中三板后第4日强负反馈禁入条件"}


def rebound_exit(context, position, quote, market_date):
    """D4 entries exit on a D5 current-price rebound, never on a later daily high."""
    if (not context.get("active") or not context.get("assessed")
            or context.get("version") != FEEDBACK_VERSION or not (context.get("policy") or {}).get("enabled")
            or context.get("market_date") != market_date
            or context.get("code") != normalize_code(position.get("code"))
            or context.get("board_day") != 5
            or date_text(position.get("entry_date")) != context.get("feedback_date")):
        return {}
    if (normalize_code(quote.get("code")) != context["code"]
            or quote.get("is_stale") is not False or date_text(quote.get("date")) != market_date
            or quote.get("quality_ok") is False or quote.get("is_suspended") or quote.get("suspended")):
        return {}
    opening, current = positive_price(quote.get("open_price")), positive_price(quote.get("last_price"))
    if opening is None or current is None:
        return {}
    gain = context["policy"]["rebound_gain"]
    target = math.ceil(opening * (1 + gain) * 100 - 1e-8) / 100
    if current < target:
        return {}
    return {"target_price": target, "session_open": opening, "current_price": current,
            "reason": f"三板后第4日买入且收盘强负反馈，第5日较真实开盘反弹达到{gain * 100:g}%，优先退出"}
