"""Optional, causal board-index scoring. This module never requests market data."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic

from core.realtime.board_quote_store import trading_time
from core.realtime.trading_session import shanghai_now

VERSION = "mapped_board_trend_score_v1"


def number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError):
        return None


def assess_trend(names, inputs, *, now, weight=5.0):
    """Equal-weight mapped boards; never treat missing breadth as confirmation."""
    clock = shanghai_now(now)
    plan = inputs.get("plan") or {}
    result = {
        "version": VERSION,
        "role": "score_only",
        "entry_veto": False,
        "assessed": False,
        "score_adjustment": 0.0,
        "weight": weight,
        "sector_names": sorted(names),
        "boards": [],
        "unmapped_names": [],
        "reason": "缺少经核验的板块映射或连续板块自身行情",
        "source": "tdx_board_index_mapped",
    }
    if not plan or plan.get("market_date") != clock.strftime("%Y%m%d"):
        return result
    selected = {}
    for name in names:
        mapping = next((row for row in plan.get("rows", []) if row.get("ths_name") == name), {})
        code = mapping.get("score_code") if mapping.get("score_eligible") else None
        if code and code in plan.get("codes", []):
            selected[code] = mapping
        else:
            result["unmapped_names"].append(name)
    for code, mapping in sorted(selected.items()):
        points = []
        for row in inputs.get("points", []):
            try:
                stamp = shanghai_now(datetime.fromisoformat(row["received_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if (
                row.get("code") == code
                and row.get("board_plan_id") == plan.get("plan_id")
                and row.get("date") == plan["market_date"]
                and stamp.strftime("%Y%m%d") == plan["market_date"]
                and trading_time(stamp)
                and row.get("source") == "eltdx_board_snapshot"
                and not row.get("synthetic")
                and 0 <= (clock - stamp).total_seconds() <= 220
            ):
                points.append((stamp, row))
        points.sort(key=lambda item: item[0])
        detail = {
            "code": code,
            "ths_name": mapping["ths_name"],
            "mapping_basis": mapping.get("score_mapping_basis"),
            "assessed": False,
            "reason": "板块快照缺失、过期或三分钟窗口不连续",
        }
        result["boards"].append(detail)
        if not points or (clock - points[-1][0]).total_seconds() > 20:
            continue
        latest_at, latest = points[-1]
        target = latest_at - timedelta(minutes=3)
        earlier = [item for item in points if 0 <= (target - item[0]).total_seconds() <= 20]
        if not earlier:
            continue
        anchor_at, anchor = earlier[-1]
        window = [item for item in points if anchor_at <= item[0] <= latest_at]
        opening, previous, price = (number(latest.get(k)) for k in ("open_price", "pre_close", "last_price"))
        before = number(anchor.get("last_price"))
        if any(value is None or value <= 0 for value in (opening, previous, price, before)):
            continue
        if any((b[0] - a[0]).total_seconds() > 20 for a, b in zip(window, window[1:], strict=False)):
            continue
        if any(
            number(row.get("open_price")) != opening
            or number(row.get("pre_close")) != previous
            or (stamp.hour < 12) != (latest_at.hour < 12)
            for stamp, row in window
        ):
            continue
        changes = [(price / previous - 1) * 100, (price / opening - 1) * 100, (price / before - 1) * 100]
        signs = [(1 if value > 1e-9 else -1 if value < -1e-9 else 0) for value in changes]
        detail.update(
            assessed=True,
            reason="真实板块自身涨幅、相对开盘与三分钟方向评分",
            observed_at=latest_at.isoformat(),
            baseline_at=anchor_at.isoformat(),
            change_pct=changes[0],
            open_gain_pct=changes[1],
            three_minute_change_pct=changes[2],
            score_adjustment=round(sum(signs) / 3 * weight, 4),
        )
    valid = [row for row in result["boards"] if row["assessed"]]
    if valid:
        result.update(
            assessed=True,
            reason="板块自身趋势辅助评分，不参与买点一票否决",
            score_adjustment=round(sum(row["score_adjustment"] for row in valid) / len(selected), 4),
            data_coverage=len(valid) / len(selected),
            trend_state=all(row["open_gain_pct"] >= 0 and row["three_minute_change_pct"] > 0 for row in valid),
        )
    return result


def trend_adjustment(row):
    factor = row.get("sector_trend") or (row.get("sector_detail") or {}).get("trend_factor") or {}
    value = number(factor.get("score_adjustment"))
    return (
        max(-20, min(20, value))
        if factor.get("version") == VERSION and factor.get("assessed") and value is not None
        else 0.0
    )


def make_trend_checker(names, inputs, *, date, weight=5.0, clock=None):
    detail = {
        "sector_confirmation_version": VERSION,
        "sector_confirmation_source": "tdx_board_index_mapped",
        "sector_required": False,
        "data_completeness": 0.0,
        "assessed": False,
    }
    from core.realtime.collection_plan import digest

    plan = inputs.get("plan") or {}
    if plan and (
        plan.get("market_date") != date
        or plan.get("plan_id") != digest({k: v for k, v in plan.items() if k != "plan_id"})
    ):
        inputs = {}
    # Build the archive timestamp index once, not once for every replay minute.
    from bisect import bisect_left, bisect_right

    indexed = []
    for row in inputs.get("points") or []:
        try:
            indexed.append((shanghai_now(datetime.fromisoformat(row["received_at"])), row))
        except (KeyError, TypeError, ValueError):
            continue
    indexed.sort(key=lambda item: item[0])
    stamps = [item[0] for item in indexed]

    def check(at_time):
        at = shanghai_now(datetime.strptime(date + str(at_time), "%Y%m%d%H:%M:%S"))
        if clock is not None:
            at = min(at, shanghai_now(clock()))
        try:
            window = indexed[bisect_left(stamps, at - timedelta(seconds=220)) : bisect_right(stamps, at)]
            factor = assess_trend(
                names, {"plan": inputs.get("plan"), "points": [row for _, row in window]}, now=at, weight=weight
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            factor = assess_trend(names, {}, now=at, weight=weight)
            factor["reason"] = "板块趋势证据格式无效，未评估"
        detail.update(
            trend_factor=factor,
            assessed=factor["assessed"],
            reason=factor["reason"],
            data_completeness=factor.get("data_coverage", 0.0),
        )
        return factor.get("trend_state") if factor["assessed"] else None

    return check, detail


def archived_trend_inputs(root, date):
    from pathlib import Path

    from core.infrastructure.atomic_json import atomic_read_json
    from core.realtime.board_plan import BoardCollectionPlan
    from core.realtime.board_quote_store import BoardQuoteStore
    from core.realtime.collection_plan import digest

    try:
        parent_path = Path(root) / "collection_plans" / f"{date}.json"
        if not parent_path.exists():
            return {"plan": None, "points": []}
        parent = atomic_read_json(parent_path)
        if parent.get("market_date") != date or parent.get("plan_id") != digest(
            {k: v for k, v in parent.items() if k != "plan_id"}
        ):
            raise ValueError("Invalid parent collection plan")
        plan = BoardCollectionPlan(root).load(date, parent_id=parent["plan_id"])
        if not plan:
            return {"plan": None, "points": []}
        points = BoardQuoteStore(backend=object(), archive_root=Path(root) / "market_archive").archived_points(
            date, until=datetime.strptime(date + "15:01:00", "%Y%m%d%H:%M:%S")
        )
        return {"plan": plan, "points": points}
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        return {"plan": None, "points": [], "reason": type(exc).__name__}


class BoardTrendReader:
    """Read prepared mappings and hot board quotes only; no provider dependency."""

    def __init__(self, root=None, backend=None):
        from core.infrastructure.shared_state import get_shared_state_backend

        self.root, self.backend, self._cache = root, backend or get_shared_state_backend(), None

    def checker(self, names, date, weight=5.0, *, view=None):
        from core.realtime.board_plan import BoardCollectionPlan
        from core.realtime.board_quote_store import BoardQuoteStore
        from core.realtime.collection_plan import DailyCollectionPlan

        if view is not None:
            root = self.root or view.reader.root.parent
            parent = DailyCollectionPlan(root, self.backend).load(date)
            plan = BoardCollectionPlan(root).load(date, parent_id=parent["plan_id"]) if parent else None
            detail = {"assessed": False, "sector_required": False, "data_completeness": 0.0}

            def persistent_check(at_time):
                at = min(view.asof, shanghai_now(datetime.strptime(date+str(at_time), "%Y%m%d%H:%M:%S")))
                points = view.events("board_snapshot", plan["codes"], start=at-timedelta(seconds=220), end=at) if plan else []
                checker, observed = make_trend_checker(names, {"plan": plan, "points": points},
                    date=date, weight=weight, clock=lambda: view.asof)
                state = checker(at_time)
                detail.update(observed)
                detail["persistent_view"] = {"asof": view.asof.isoformat(), "watermark": view.watermark}
                return state

            return persistent_check, detail

        if self._cache is None or self._cache[0] != date or monotonic() - self._cache[1] > 1:
            parent = DailyCollectionPlan(self.root, self.backend).load(date)
            plan = BoardCollectionPlan(self.root).load(date, parent_id=parent["plan_id"]) if parent else None
            values = self.backend.get_many_json([BoardQuoteStore.key(date, c) for c in plan["codes"]]) if plan else {}
            inputs = {
                "plan": plan,
                "points": [row for rows in values.values() if isinstance(rows, list) for row in rows],
            }
            self._cache = (date, monotonic(), inputs)
        return make_trend_checker(names, self._cache[2], date=date, weight=weight, clock=shanghai_now)
