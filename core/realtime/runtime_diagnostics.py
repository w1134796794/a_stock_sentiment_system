"""Read-only reconciliation of recorded live events and minute-rule replay."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, time
from zoneinfo import ZoneInfo


def _sent(value):
    try:
        return int(value or 0) > 0
    except (ValueError, TypeError):
        return False


def _in_replay_window(stamp, report):
    if not report.get("until"):
        return True
    try:
        value = datetime.fromisoformat(str(stamp))
        if value.tzinfo:
            value = value.astimezone(ZoneInfo("Asia/Shanghai"))
        if report.get("market_date") and value.strftime("%Y%m%d") != report["market_date"]:
            return False
        observed = value.time()
    except ValueError:
        try:
            observed = time.fromisoformat(str(stamp))
        except ValueError:
            return False
    return observed <= time.fromisoformat(report["until"])


def attach_runtime_diagnostics(report):
    """Recorded delivery is evidence, not permission to trade or replay a push."""
    grouped = {}
    recorded = report.get("recorded_runtime") or {}
    for source in recorded.values():
        for row in source.get("rows") or []:
            code = str(row.get("stock_code") or row.get("code") or "")
            if code:
                grouped.setdefault(code, []).append(row)
    counts = Counter()
    for stock in report.get("stocks") or []:
        records = grouped.get(stock["code"], [])
        profiles, times, vetoes, attempts = set(), set(), set(), {}
        observations = []
        confirmed = production_confirmed = delivered = delivered_in_window = False
        for row in records:
            evidence = row.get("evidence") or {}
            if row.get("evidence_kind") == "runtime_observation":
                observations.append({"observed_at": row.get("observed_at") or "",
                                     "status": evidence.get("status") or "",
                                     "reason": evidence.get("trigger_reason") or evidence.get("reason") or "",
                                     "data_status": evidence.get("data_status") or "",
                                     "quote_received_at": evidence.get("quote_received_at") or "",
                                     "quote_stale_seconds": evidence.get("quote_stale_seconds")})
            profile = str(evidence.get("profile") or row.get("profile") or "")
            profiles.add(profile or "unknown")
            status = evidence.get("status") or evidence.get("confirm_status") or row.get("status")
            if status == "confirmed":
                confirmed = True
                production_confirmed |= profile == "production"
                stamp = evidence.get("confirm_time") or evidence.get("minute_closed_at") or row.get("first_confirmed_at")
                if stamp:
                    times.add(str(stamp))
            reason = evidence.get("health_gate_reason") or evidence.get("veto_reason")
            if reason:
                vetoes.add(str(reason))
            for event in row.get("delivery_events") or []:
                try:
                    payload = json.loads(event.get("payload_json") or "{}")
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict) or payload.get("side", "buy") != "buy":
                    continue
                if event.get("kind") not in {"notification", "notification_attempt"}:
                    continue
                if payload.get("code") and str(payload["code"]) != stock["code"]:
                    continue
                delivered |= _sent(payload.get("sent"))
                delivered_in_window |= _sent(payload.get("sent")) and _in_replay_window(event.get("occurred_at"), report)
                if event["kind"] == "notification_attempt":
                    key = (str(event.get("occurred_at") or ""), str(event.get("payload_json") or ""))
                    attempts[key] = {"occurred_at": key[0], "status": payload.get("status") or "",
                                     "sent": _sent(payload.get("sent")),
                                     "reason": payload.get("skip_reason") or ""}
        ordered = sorted(attempts.values(), key=lambda event: event["occurred_at"])
        state = ("sent" if delivered else "attempt_not_sent" if ordered else
                 "confirmed_no_delivery_record" if production_confirmed else
                 "observation_confirmed" if confirmed else "veto_recorded" if vetoes else
                 "recorded_without_confirmation" if records else "no_runtime_evidence")
        discrepancy = ""
        recorded_in_window = delivered_in_window or any(_in_replay_window(stamp, report) for stamp in times)
        if recorded_in_window and "first_confirm_time" in stock and not stock.get("first_confirm_time"):
            missing = stock.get("status") == "missing_evidence" or any(
                failure.get("code") == stock["code"] for failure in report.get("failures") or [])
            discrepancy = "replay_missing_evidence" if missing else "replay_result_differs"
        stock["runtime_diagnostic"] = {
            "state": state, "record_count": len(records), "profiles": sorted(profiles),
            "confirmed": confirmed, "production_confirmed": production_confirmed,
            "confirm_times": sorted(times), "notification_sent": delivered,
            "notification_attempts": ordered, "veto_reasons": sorted(vetoes),
            "replay_discrepancy": discrepancy,
            "last_observation": max(observations, key=lambda item: item["observed_at"]) if observations else None,
        }
        counts[state] += 1
    report["runtime_summary"] = {
        "states": dict(counts), "record_count": sum(len(rows) for rows in grouped.values()),
        "confirmed_stocks": sum(bool(s["runtime_diagnostic"]["confirmed"]) for s in report.get("stocks") or []),
        "sent_stocks": counts["sent"],
        "scope": "selected_stocks_all_day_recorded_events",
    }
    return report
