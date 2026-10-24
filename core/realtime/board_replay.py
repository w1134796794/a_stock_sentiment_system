"""Read-only, as-of replay of captured board observations, never a buy signal."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from core.infrastructure.atomic_json import atomic_read_json
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.board_plan import BoardCollectionPlan
from core.realtime.board_quote_store import (
    BoardQuoteStore,
    aggregate_board_minutes,
    assess_board_points,
    positive,
    trading_time,
)
from core.realtime.collection_plan import digest
from core.realtime.trading_session import shanghai_now

VERSION = "board_shadow_v1"


def cutoff_for(date, until="15:01:00"):
    if len(until) == 5:
        until += ":00"
    cutoff = shanghai_now(datetime.strptime(date + until, "%Y%m%d%H:%M:%S"))
    if len(date) != 8 or not date.isdigit() or not "09:30:00" <= until <= "15:01:00":
        raise ValueError("Expected YYYYMMDD and a time between 09:30 and 15:01")
    return cutoff


def load_board_inputs(root, date, *, until="15:01:00"):
    """Only existing files are opened; no backend, provider or journal is created."""
    root = Path(root)
    cutoff = cutoff_for(date, until)
    payload = {
        "market_date": date,
        "until": cutoff.strftime("%H:%M:%S"),
        "synthetic": False,
        "parent": None,
        "plan": None,
        "points": [],
        "load_failures": [],
    }
    path = root / "collection_plans" / f"{date}.json"
    try:
        if not path.exists():
            return payload
        parent = atomic_read_json(path)
        body = {key: value for key, value in parent.items() if key != "plan_id"}
        frozen_at = shanghai_now(datetime.fromisoformat(parent["frozen_at"]))
        if (
            parent["market_date"] != date
            or digest(body) != parent["plan_id"]
            or frozen_at.strftime("%Y%m%d") != date
            or frozen_at.strftime("%H:%M:%S") >= "09:25:00"
        ):
            raise ValueError("Collection plan is not valid frozen preopen evidence")
        payload["parent"] = parent
        plan = BoardCollectionPlan(root).load(date, parent_id=parent["plan_id"])
        if not plan:
            return payload
        captured = shanghai_now(datetime.fromisoformat(plan["captured_at"]))
        if captured.strftime("%Y%m%d") != date or captured.strftime("%H:%M:%S") >= "09:25:00":
            raise ValueError("Board plan is not valid frozen preopen evidence")
        payload["plan"] = plan
        payload["points"] = BoardQuoteStore(
            backend=MemoryStateBackend(), archive_root=root / "market_archive"
        ).archived_points(date, until=cutoff)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        payload["load_failures"].append({"evidence": "board_archive_or_plan", "reason": str(exc)})
    return payload


def replay_board_inputs(inputs):
    date = inputs["market_date"]
    cutoff = cutoff_for(date, inputs.get("until", "15:01:00"))
    parent, plan = inputs.get("parent"), inputs.get("plan")
    files = [Path(__file__), Path(__file__).with_name("board_quote_store.py")]
    versions = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    base = {
        "classification": "board_shadow_diagnostic",
        "market_date": date,
        "until": cutoff.strftime("%H:%M:%S"),
        "synthetic": bool(inputs.get("synthetic")),
        "input_hash": digest(inputs),
        "code_hash": digest(versions),
        "assessment_version": VERSION,
        "formal_eligible": False,
        "formal_signal": False,
        "notification_sent": False,
        "account_modified": False,
        "live_runtime_verified": False,
        "remote_fetch_performed": False,
        "complete": False,
        "failures": list(inputs.get("load_failures") or []),
        "rows": [],
        "timeline": [],
        "minutes": [],
        "reconciliation": {"matched": 0, "different": 0, "missing_live_assessment": 0, "version_mismatch": 0},
        "limitations": [
            "TDX board shadow only; formal THS confirmation is unchanged",
            "Missing observations and capture assessments are not reconstructed",
            "Receipt freshness is not verified exchange timestamp freshness",
            "Historical constituent width is not synthesized from current quotes",
        ],
    }
    if not parent or not plan:
        state = "missing_collection_plan" if not parent else "missing_board_plan"
        base["failures"].append({"evidence": state, "reason": state})
        return {**base, "status": state}
    if plan["market_date"] != date or parent["market_date"] != date or plan["collection_plan_id"] != parent["plan_id"]:
        base["failures"].append({"evidence": "plan_identity_mismatch", "reason": "Frozen plan identities differ"})
        return {**base, "status": "invalid_evidence"}
    histories, seen, events = {code: [] for code in plan["codes"]}, set(), []
    invalid = Counter()
    for row in inputs.get("points") or []:
        try:
            received = datetime.fromisoformat(row["received_at"])
            evaluated = datetime.fromisoformat(row.get("capture_evaluated_at") or row["received_at"])
            if received.tzinfo is None or evaluated.tzinfo is None:
                raise ValueError("Timezone missing")
            received, evaluated = shanghai_now(received), shanghai_now(evaluated)
        except (KeyError, ValueError, TypeError):
            invalid["invalid_timestamp"] += 1
            continue
        if received > cutoff or evaluated > cutoff:
            continue
        if (
            row.get("code") not in histories
            or row.get("board_plan_id") != plan["plan_id"]
            or row.get("date") != date
            or received.strftime("%Y%m%d") != date
            or evaluated.strftime("%Y%m%d") != date
            or evaluated < received
            or row.get("source") != "eltdx_board_snapshot"
            or not trading_time(received)
            or not positive(row.get("last_price"))
            or row.get("synthetic")
            and not base["synthetic"]
        ):
            invalid["invalid_identity_or_quote"] += 1
            continue
        identity = row["code"], received
        if identity in seen:
            continue
        seen.add(identity)
        events.append((evaluated, received, row))
    events.sort(key=lambda event: (event[0], event[1], event[2]["code"]))
    policies = {}
    accepted = []
    for evaluated, received, row in events:
        code = row["code"]
        previous = histories[code]
        if previous and received <= shanghai_now(datetime.fromisoformat(previous[-1]["received_at"])):
            invalid["regressed_receipt"] += 1
            continue
        policy = row.get("capture_policy") or {"stale_seconds": 20, "expected_interval": 10}
        try:
            stale, interval = float(policy["stale_seconds"]), float(policy["expected_interval"])
            if not positive(stale) or not positive(interval):
                raise ValueError("Invalid policy")
        except (KeyError, TypeError, ValueError):
            invalid["invalid_capture_policy"] += 1
            continue
        histories[code] = [
            point
            for point in previous
            if (received - shanghai_now(datetime.fromisoformat(point["received_at"]))).total_seconds() <= 180
        ]
        histories[code] = (histories[code] + [row])[-200:]
        policies[code] = {"stale_seconds": stale, "expected_interval": interval}
        replayed = assess_board_points(plan, histories, now=evaluated, **policies[code])[code]
        original = row.get("assessment_at_capture")
        changes = {}
        if original is None:
            state = "missing_live_assessment"
        elif row.get("assessment_version") != VERSION:
            state = "version_mismatch"
        else:
            changes = {
                key: {"captured": original.get(key), "replayed": value}
                for key, value in replayed.items()
                if original.get(key) != value
            }
            state = "different" if changes else "matched"
        base["reconciliation"][state] += 1
        base["timeline"].append(
            {
                "code": code,
                "received_at": row["received_at"],
                "evaluated_at": evaluated.isoformat(),
                "replayed": replayed,
                "captured": original,
                "comparison_state": state,
                "differences": changes,
            }
        )
        accepted.append(row)
    for code in plan["codes"]:
        assessed = assess_board_points(plan, histories, now=cutoff, **policies.get(code, {}))[code]
        base["rows"].append({"code": code, **assessed})
        if assessed["status"] in {"missing", "stale"}:
            base["failures"].append(
                {
                    "code": code,
                    "evidence": f"board_snapshot_{assessed['status']}",
                    "reason": "No fresh captured board observation at requested cutoff",
                }
            )
    for category, count in invalid.items():
        base["failures"].append({"evidence": category, "count": count, "reason": "Rejected archive observations"})
    base["minutes"] = aggregate_board_minutes(accepted, until=cutoff)
    base["reconciliation"]["complete"] = bool(accepted) and base["reconciliation"]["matched"] == len(accepted)
    base.update(
        status="shadow_only" if accepted else "missing_board_snapshots",
        observed_points=len(accepted),
        mappings=plan.get("rows") or [],
        board_plan_id=plan["plan_id"],
        complete=bool(accepted) and not base["failures"],
    )
    return base


def attach_board_replay(report, root):
    inputs = load_board_inputs(root, report["market_date"], until=report["until"])
    report["board_shadow"] = replay_board_inputs(inputs)
    if "replay_inputs" in report:
        report["replay_inputs"]["board_shadow_inputs"] = inputs
    return report
