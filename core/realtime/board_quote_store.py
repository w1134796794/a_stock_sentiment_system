"""Isolated TDX board snapshots. Readers never request or backfill market data."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.trading_session import shanghai_now


def positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def trading_time(stamp):
    at = stamp.strftime("%H:%M:%S")
    return "09:30:00" <= at <= "11:30:00" or "13:00:00" <= at <= "15:00:00"


class BoardQuoteStore:
    def __init__(self, *, backend=None, journal=None, archive_root=None, stale_seconds=20, expected_interval=10):
        from config.settings import WEB_DATA_DIR

        self.backend = backend or get_shared_state_backend()
        self.journal = journal
        self.archive_root = Path(archive_root or Path(WEB_DATA_DIR) / "market_archive")
        self.stale_seconds = stale_seconds
        self.expected_interval = expected_interval

    @staticmethod
    def key(date, code):
        return f"realtime:tdx-board:{date}:{code}"

    def write(self, plan, rows, *, now=None):
        if self.journal is None:
            raise RuntimeError("Board writes require a durable ingestion journal")
        clock = shanghai_now(now)
        date = plan["market_date"]
        if clock.strftime("%Y%m%d") != date or not trading_time(clock):
            return {"count": 0, "rejected_count": len(rows)}
        keys = {code: self.key(date, code) for code in plan["codes"]}
        old = self.backend.get_many_json(list(keys.values()))
        values, accepted, rejected = {}, [], 0
        for raw in rows:
            code = raw.get("code")
            try:
                stamp = shanghai_now(datetime.fromisoformat(raw["received_at"]))
            except (KeyError, TypeError, ValueError):
                rejected += 1
                continue
            if (
                code not in keys
                or raw.get("date") != date
                or stamp.strftime("%Y%m%d") != date
                or not trading_time(stamp)
                or not 0 <= (clock - stamp).total_seconds() <= self.stale_seconds
                or raw.get("source") != "eltdx_board_snapshot"
                or raw.get("synthetic")
                or not positive(raw.get("last_price"))
            ):
                rejected += 1
                continue
            key = keys[code]
            history = values.get(key, old.get(key)) or []
            previous = history[-1] if history else None
            prior_stamp = shanghai_now(datetime.fromisoformat(previous["received_at"])) if previous else None
            if prior_stamp is not None and stamp <= prior_stamp:
                rejected += 1
                continue
            row = {
                **raw,
                "received_at": stamp.isoformat(timespec="milliseconds"),
                "board_plan_id": plan["plan_id"],
                "formal_eligible": False,
                "exchange_timestamp_verified": False,
                "sample_interval_seconds": (stamp - prior_stamp).total_seconds() if prior_stamp else None,
            }
            accepted.append(row)
            values[key] = [
                point
                for point in history
                if 0 <= (stamp - shanghai_now(datetime.fromisoformat(point["received_at"]))).total_seconds() <= 180
            ]
            values[key] = (values[key] + [row])[-200:]
            row["capture_evaluated_at"] = clock.isoformat()
            row["capture_policy"] = {"stale_seconds": self.stale_seconds, "expected_interval": self.expected_interval}
            row["assessment_version"] = "board_shadow_v1"
            row["assessment_at_capture"] = assess_board_points(
                plan,
                {code: values[key]},
                now=clock,
                stale_seconds=self.stale_seconds,
                expected_interval=self.expected_interval,
            )[code]
        if accepted:
            # A failed hot-cache write cannot erase the already committed observations.
            self.journal.append("board_snapshot", date, accepted)
            self.backend.set_many_json(values, 180)
        return {"count": len(accepted), "rejected_count": rejected}

    def read(self, plan, *, now=None, expected_interval=10):
        clock = shanghai_now(now)
        date = plan["market_date"]
        values = self.backend.get_many_json([self.key(date, code) for code in plan["codes"]])
        return assess_board_points(
            plan,
            {code: values.get(self.key(date, code)) or [] for code in plan["codes"]},
            now=clock,
            stale_seconds=self.stale_seconds,
            expected_interval=expected_interval,
        )

    def archived_points(self, date, *, until=None):
        datetime.strptime(date, "%Y%m%d")
        cutoff = shanghai_now(until)
        selected = {}
        for path in (self.archive_root / f"market_{date}.sqlite3", self.archive_root / "ingestion.sqlite3"):
            if not path.exists():
                continue
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
                for event_id, payload in con.execute(
                    "SELECT event_id,payload FROM market_events WHERE kind='board_snapshot'"
                ):
                    row = json.loads(payload)
                    stamp = shanghai_now(datetime.fromisoformat(row["received_at"]))
                    if row.get("date") == date and stamp.strftime("%Y%m%d") == date and stamp <= cutoff:
                        selected[event_id] = row
        return sorted(selected.values(), key=lambda row: row["received_at"])


def assess_board_points(plan, values, *, now, stale_seconds=20, expected_interval=10):
    """Shared pure evaluator for live reads, captured assessments and replay."""
    clock = shanghai_now(now)
    date = plan["market_date"]
    result = {}
    for code in plan["codes"]:
        points = [
            point
            for point in values.get(code) or []
            if point.get("board_plan_id") == plan["plan_id"] and point.get("code") == code
        ]
        valid = []
        for point in points:
            try:
                stamp = shanghai_now(datetime.fromisoformat(point["received_at"]))
            except (KeyError, ValueError, TypeError):
                continue
            if (
                point.get("date") == date
                and stamp.strftime("%Y%m%d") == date
                and point.get("source") == "eltdx_board_snapshot"
                and positive(point.get("last_price"))
                and stamp <= clock
            ):
                valid.append((stamp, point))
        valid.sort(key=lambda item: item[0])
        latest = valid[-1] if valid else None
        age = (clock - latest[0]).total_seconds() if latest else None
        window = [(stamp, row) for stamp, row in valid if (clock - stamp).total_seconds() <= 180]
        gaps = [(right[0] - left[0]).total_seconds() for left, right in zip(window, window[1:], strict=False)]
        price = positive(latest[1].get("last_price")) if latest else None
        opening = positive(latest[1].get("open_price")) if latest else None
        prev = positive(latest[1].get("pre_close")) if latest else None
        fresh = age is not None and clock.strftime("%Y%m%d") == date and age <= stale_seconds
        result[code] = {
            "status": "missing" if latest is None else "fresh_receipt" if fresh else "stale",
            "received_at": latest[1]["received_at"] if latest else "",
            "receipt_age_seconds": age,
            "last_price": price,
            "open_price": opening,
            "pre_close": prev,
            "change_pct": (price / prev - 1) * 100 if price and prev else None,
            "open_gain_pct": (price / opening - 1) * 100 if price and opening else None,
            "hot_sample_count": len(window),
            "max_gap_seconds": max(gaps) if gaps else None,
            "continuity_ok": fresh and len(window) >= 2 and max(gaps) <= expected_interval * 2,
            "observed_window_seconds": (window[-1][0] - window[0][0]).total_seconds() if window else 0,
            "exchange_timestamp_verified": False,
            "formal_eligible": False,
        }
    return result


def aggregate_board_minutes(rows, *, until=None):
    """Sampled OHLC, not exchange minute bars; never fill an unobserved minute."""
    cutoff = shanghai_now(until)
    groups = {}
    for row in sorted(rows, key=lambda item: item["received_at"]):
        stamp = shanghai_now(datetime.fromisoformat(row["received_at"]))
        price = positive(row.get("last_price"))
        if stamp > cutoff or not trading_time(stamp) or not price or row.get("source") != "eltdx_board_snapshot":
            continue
        end = stamp.replace(second=0, microsecond=0)
        if stamp > end:
            end += timedelta(minutes=1)
        if end.strftime("%H:%M:%S") in {"09:30:00", "13:00:00"}:
            continue
        key = row["date"], row["code"], end.isoformat(), row.get("board_plan_id")
        group = groups.setdefault(
            key,
            {
                "date": row["date"],
                "code": row["code"],
                "time": end.strftime("%H:%M:%S"),
                "board_plan_id": row.get("board_plan_id"),
                "open": price,
                "high": price,
                "low": price,
                "sample_count": 0,
                "first_received_at": row["received_at"],
                "closed": end <= cutoff,
                "source": "sampled_board_snapshots",
                "formal_eligible": False,
            },
        )
        group.update(
            high=max(group["high"], price),
            low=min(group["low"], price),
            close=price,
            last_received_at=row["received_at"],
            sample_count=group["sample_count"] + 1,
        )
    return list(groups.values())
