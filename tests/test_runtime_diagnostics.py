from __future__ import annotations

import json
import sqlite3

import pytest

from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
from scripts.diagnose_intraday_signal import runtime_evidence


def report(*records, replay_confirmed=False, missing=True):
    return {
        "stocks": [{"code": "603533", "status": "observe",
                    "first_confirm_time": "09:38:00" if replay_confirmed else ""}],
        "failures": [{"code": "603533", "evidence": "sector_minutes"}] if missing else [],
        "recorded_runtime": {"signal_ledger": {"rows": list(records)}},
        "live_runtime_verified": False, "account_modified": False, "notification_sent": False,
    }


def record(*, status="confirmed", profile="production", events=()):
    return {"code": "603533", "status": status,
            "evidence": {"profile": profile, "minute_closed_at": "20261015 09:38:00"},
            "delivery_events": list(events)}


def event(kind="notification", **payload):
    return {"kind": kind, "occurred_at": "2026-10-15T09:39:01",
            "payload_json": json.dumps({"code": "603533", "side": "buy", **payload})}


def test_missing_replay_cannot_erase_recorded_delivery_or_make_it_live():
    value = report(record(events=[event(sent=1)]))
    original = json.loads(json.dumps(value["recorded_runtime"]))
    attach_runtime_diagnostics(value)
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "sent"
    assert runtime["replay_discrepancy"] == "replay_missing_evidence"
    assert runtime["confirm_times"] == ["20261015 09:38:00"]
    assert value["runtime_summary"]["sent_stocks"] == 1
    assert value["recorded_runtime"] == original
    assert value["stocks"][0]["status"] == "observe"
    assert not value["live_runtime_verified"] and not value["notification_sent"] and not value["account_modified"]


def test_missing_session_open_does_not_erase_legacy_decision_pool_delivery():
    value = report(record(profile="decision_pool", events=[event(sent=1)]))
    value["stocks"][0].update(status="missing_evidence", session_open=None, transitions=[])
    value["failures"][0]["evidence"] = "session_open"
    attach_runtime_diagnostics(value)
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "sent" and runtime["confirmed"]
    assert not runtime["production_confirmed"]
    assert runtime["replay_discrepancy"] == "replay_missing_evidence"
    assert value["runtime_summary"]["sent_stocks"] == 1
    assert value["stocks"][0]["status"] == "missing_evidence"
    assert not value["notification_sent"] and not value["account_modified"]


def test_observation_profile_confirmation_is_not_production_or_delivery():
    value = attach_runtime_diagnostics(report(record(profile="leader_pool")))
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "observation_confirmed"
    assert runtime["confirmed"] and not runtime["production_confirmed"] and not runtime["notification_sent"]


def test_confirmed_without_delivery_means_unknown_not_failed():
    value = attach_runtime_diagnostics(report(record()))
    assert value["stocks"][0]["runtime_diagnostic"]["state"] == "confirmed_no_delivery_record"


def test_delivery_attempt_reason_and_deduplication_are_visible():
    attempt = event("notification_attempt", sent=0, status="blocked", skip_reason="collector_stale")
    row = record(events=[attempt, attempt])
    value = attach_runtime_diagnostics(report(row, row))
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "attempt_not_sent"
    assert runtime["notification_attempts"] == [{"occurred_at": attempt["occurred_at"], "status": "blocked",
                                                "sent": False, "reason": "collector_stale"}]


@pytest.mark.parametrize("bad", [event(sent=0), event(sent="not a number"), event(sent=1, side="sell"),
                                  event(sent=1, code="600391"), event("other", sent=1),
                                  {"kind": "notification", "payload_json": "[1]"},
                                  {"kind": "notification", "payload_json": "invalid"}])
def test_other_events_and_malformed_records_cannot_prove_buy_delivery(bad):
    value = attach_runtime_diagnostics(report(record(events=[bad])))
    assert not value["stocks"][0]["runtime_diagnostic"]["notification_sent"]


def test_no_runtime_record_is_not_proof_of_no_signal():
    value = attach_runtime_diagnostics(report(replay_confirmed=True, missing=False))
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "no_runtime_evidence"
    assert runtime["replay_discrepancy"] == "replay_confirmed_without_runtime_evidence"


def test_cancelled_latest_state_does_not_erase_earlier_delivery():
    value = attach_runtime_diagnostics(report(record(status="cancelled", events=[event(sent=1)]), missing=False))
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "sent" and not runtime["confirmed"]
    assert runtime["replay_discrepancy"] == "replay_result_differs"


def test_later_confirmation_is_not_a_discrepancy_before_replay_cutoff():
    value = report(record(events=[event(sent=1)]))
    value.update(market_date="20261015", until="09:37:00")
    attach_runtime_diagnostics(value)
    runtime = value["stocks"][0]["runtime_diagnostic"]
    assert runtime["state"] == "sent"  # Recorded full-day delivery remains visible.
    assert runtime["replay_discrepancy"] == ""


def test_runtime_only_cli_does_not_read_or_fetch_price_data(tmp_path, monkeypatch):
    import scripts.diagnose_intraday_signal as diagnostics
    output = tmp_path / "diagnostic.json"
    monkeypatch.setattr(diagnostics.sys, "argv", ["diagnose", "--date", "20261015", "--codes", "603533",
                                                "--runtime-only", "--output", str(output)])
    monkeypatch.setattr(diagnostics.pd, "read_csv", lambda *a, **kw: pytest.fail("must not load market data"))
    monkeypatch.setattr(diagnostics, "price_diagnostic", lambda *a: pytest.fail("must not assess price signals"))
    monkeypatch.setattr(diagnostics, "runtime_evidence", lambda *a: {"rows": []})
    diagnostics.main()
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["classification"] == "recorded_runtime_diagnostic"
    assert value["stocks"][0]["runtime_diagnostic"]["state"] == "no_runtime_evidence"


def test_old_saved_reports_get_read_only_runtime_summary(tmp_path):
    from core.realtime.day_replay_jobs import DayReplayJobs
    jobs = DayReplayJobs(tmp_path)
    path = jobs.path("a" * 32, "report.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report(record(events=[event(sent=1)]))), encoding="utf-8")
    before = path.read_bytes()
    assert jobs.get("a" * 32, report=True)["runtime_summary"]["sent_stocks"] == 1
    assert path.read_bytes() == before


def test_sqlite_diagnostic_keeps_actual_status_profile_and_delivery(tmp_path):
    path = tmp_path / "events.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE signal_evidence(signal_id,market_date,code,status,evidence_json)")
        con.execute("CREATE TABLE trading_events(signal_id,kind,occurred_at,payload_json)")
        evidence = {"status": "confirmed", "profile": "production", "minute_closed_at": "20261015 09:38:00"}
        con.execute("INSERT INTO signal_evidence VALUES(?,?,?,?,?)", ("a", "20261015", "603533", "confirmed", json.dumps(evidence)))
        sent = event(sent=1)
        con.execute("INSERT INTO trading_events VALUES(?,?,?,?)", ("a", sent["kind"], sent["occurred_at"], sent["payload_json"]))
    before = path.read_bytes()
    raw = runtime_evidence(path, "signal_evidence", "20261015", ["603533"])
    value = report()
    value["recorded_runtime"]["signal_ledger"] = raw
    attach_runtime_diagnostics(value)
    assert raw["rows"][0]["evidence"]["profile"] == "production"
    assert value["runtime_summary"]["sent_stocks"] == 1
    assert path.read_bytes() == before
    assert not runtime_evidence(path, "signal_evidence", "20261014", ["603533"])["rows"]
