import json
import sqlite3
from copy import deepcopy
from datetime import datetime

import pytest

from core.realtime.collection_plan import digest
from core.realtime.day_replay import replay_request, replay_saved_inputs
from core.realtime.snapshot_replay import load_snapshot_inputs, replay_snapshot_inputs

DATE = "20261022"


def stamp(time):
    return f"2026-10-22T{time}+08:00"


def event(kind, code, receipt, **values):
    payload = {"code": code, "received_at": stamp(receipt), **values}
    return {
        "event_id": digest([kind, code, receipt, values]),
        "kind": kind,
        "code": code,
        "received_at": stamp(receipt),
        "payload": payload,
    }


def sample(until="09:36:03", sector_mode="breadth_required"):
    from risk.risk_config import RiskConfig

    row = {
        "code": "000001",
        "name": "Test",
        "sector_names": ["test"],
        "allowed_entry_modes": ["weak_to_strong"],
        "strategy_execution": {"allowed_entry_modes": ["weak_to_strong"]},
    }
    bundle = {
        "market_date": DATE,
        "candidate_date": "20261021",
        "bundle_id": "synthetic",
        "inputs": {
            "risk_config": RiskConfig(entry_sector_mode=sector_mode).to_dict(),
            "candidates": [row],
            "sector_codes": {"test": "881001"},
            "sector_members": {"test": ["000001", "000002", "000003"]},
            "daily_rows": [{"code": "000001", "close": 10, "amount_yuan": 100000000}],
            "minute_amount_profile": {"profiles": {"high": {"09:30:00": {"fraction": 0.2, "samples": 200}}}},
        },
    }
    plan = {
        "market_date": DATE,
        "candidate_date": "20261021",
        "bundle_id": bundle["bundle_id"],
        "frozen_at": stamp("08:55:00"),
        "inputs_hash": digest(bundle),
        "rows": [{"code": "000001", "sources": ["decision_pool", "leader_pool"]}],
    }
    plan["plan_id"] = digest(plan)
    bars = [
        dict(time=f"09:{m}:00", open=9.9, high=10, low=9.8, close=9.95, volume=10000, amount=99500)
        for m in range(31, 36)
    ]
    events = [event("minutes", "000001", "09:36:00", bars=bars)]
    for code in ("000002", "000003"):
        points = [
            dict(
                code=code,
                time="09:36:00",
                price=10.1,
                pre_close=10,
                open_price=10,
                received_at=stamp("09:35:59"),
                source="eltdx_batch",
            )
        ]
        events.append(
            event("sector_minutes", code, "09:36:00", points=points, date=DATE, source="collector_closed_quote_minutes")
        )
    for index, time in enumerate(("09:36:00", "09:36:03")):
        events.append(
            event(
                "snapshot",
                "000001",
                time,
                date=DATE,
                time=time,
                source="eltdx_batch",
                last_price=10.05 + index * 0.01,
                ask1=10.06 + index * 0.01,
                open_price=9.9,
                pre_close=10,
                low_price=9.8,
                vol_hand=20000 + index * 10,
                amount_yuan=20000000 + index * 10200,
                delta_volume=10,
                quality_ok=True,
            )
        )
    return {
        "synthetic": True,
        "replay_mode": "archived_snapshot",
        "request": {
            "market_date": DATE,
            "until": until,
            "profile": "leader_pool",
            "lookback": 10,
            "codes": [],
            "replay_mode": "archived_snapshot",
            "fetch_missing": False,
        },
        "plan": plan,
        "bundle": bundle,
        "events": events,
        "load_failures": [],
    }


def test_real_rules_confirm_on_second_snapshot_without_waiting_for_fill():
    report = replay_snapshot_inputs(sample())
    stock = report["stocks"][0]
    assert stock["first_confirm_time"] == "09:36:03", stock
    assert stock["last_signal"]["trigger_source"] == "eltdx_snapshot"
    assert stock["last_signal"]["entry_price"] is None
    assert stock["last_signal"]["condition_checks"]["latest"]["checks"]["sector"] is True
    assert not report["notification_sent"] and not report["account_modified"]
    assert not report["live_runtime_verified"] and report["synthetic"]


def test_future_snapshots_and_late_minutes_never_leak_into_earlier_prefix():
    values = sample("09:36:01")
    assert not replay_snapshot_inputs(values)["stocks"][0]["ever_confirmed"]
    values = sample()
    values["events"][0]["received_at"] = stamp("09:37:00")
    values["events"][0]["payload"]["received_at"] = stamp("09:37:00")
    report = replay_snapshot_inputs(values)
    stock = report["stocks"][0]
    assert not stock["ever_confirmed"]
    assert stock["last_signal"]["data_status"] == "snapshot_structure_pending"


def test_end_status_rechecks_age_without_forgetting_earlier_confirmation():
    stock = replay_snapshot_inputs(sample("09:40:00"))["stocks"][0]
    assert stock["ever_confirmed"] and stock["first_confirm_time"] == "09:36:03"
    assert stock["closing_status"] == "observe"
    assert stock["last_signal"]["data_status"] == "quote_stale"
    assert stock["transitions"][-1]["evaluation_boundary"] == "cutoff_freshness_check"


def test_missing_sector_publication_is_not_rebuilt_from_prices_or_current_cache():
    values = sample()
    values["events"] = [e for e in values["events"] if e["kind"] != "sector_minutes"]
    report = replay_snapshot_inputs(values)
    assert not report["stocks"][0]["ever_confirmed"]
    assert any(f["evidence"] == "missing_published_sector_minutes" for f in report["failures"])


def test_late_sector_publication_is_only_visible_after_its_receipt():
    values = sample()
    for e in values["events"]:
        if e["kind"] == "sector_minutes":
            e["received_at"] = e["payload"]["received_at"] = stamp("09:36:04")
    assert not replay_snapshot_inputs(values)["stocks"][0]["ever_confirmed"]
    values["request"]["until"] = "09:36:04"
    stock = replay_snapshot_inputs(values)["stocks"][0]
    assert stock["first_evaluated_at"] == "09:36:04" and stock["ever_confirmed"]


def test_later_publication_of_older_sector_point_cannot_replace_newer_observation():
    values = sample()
    for code in ("000002", "000003"):
        point = {
            "code": code,
            "time": "09:36:00",
            "price": 9,
            "pre_close": 10,
            "open_price": 10,
            "received_at": stamp("09:35:58"),
            "source": "eltdx_batch",
        }
        values["events"].append(
            event(
                "sector_minutes", code, "09:36:02", points=[point], source="collector_closed_quote_minutes", date=DATE
            )
        )
    assert replay_snapshot_inputs(values)["stocks"][0]["ever_confirmed"]


@pytest.mark.parametrize("change", ["plan", "bundle", "naive", "date", "identity", "source"])
def test_invalid_identity_time_and_plan_are_rejected(change):
    values = sample()
    if change == "plan":
        values["plan"]["plan_id"] = "wrong"
    elif change == "bundle":
        values["bundle"]["inputs"]["risk_config"]["unexpected"] = True
    else:
        raw = values["events"][-1]["payload"]
        if change == "naive":
            raw["received_at"] = "2026-10-22T09:36:03"
        elif change == "date":
            raw["date"] = "20261021"
        elif change == "identity":
            raw["code"] = "000002"
        else:
            raw["source"] = "historical_backfill"
    report = replay_snapshot_inputs(values)
    assert not report["confirmed_stocks"] and report["failures"]


def test_strategy_mode_restrictions_remain_in_force():
    values = sample()
    row = values["bundle"]["inputs"]["candidates"][0]
    row["allowed_entry_modes"] = row["strategy_execution"]["allowed_entry_modes"] = ["acceleration_only"]
    values["plan"]["inputs_hash"] = digest(values["bundle"])
    values["plan"]["plan_id"] = digest({k: v for k, v in values["plan"].items() if k != "plan_id"})
    assert not replay_snapshot_inputs(values)["stocks"][0]["ever_confirmed"]


def test_gap_is_reported_and_not_interpolated():
    values = sample("09:36:12")
    e = values["events"][-1]
    e["received_at"] = e["payload"]["received_at"] = stamp("09:36:12")
    e["payload"]["time"] = "09:36:12"
    report = replay_snapshot_inputs(values)
    stock = report["stocks"][0]
    assert stock["snapshot_count"] == 2 and len(stock["gaps"]) == 1
    assert not stock["ever_confirmed"]
    assert stock["last_signal"]["condition_checks"]["fallback_reason"] == "snapshot_gap"


def test_no_network_shared_backend_or_live_writes(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Replay must not initialise live state or providers")

    monkeypatch.setattr("core.infrastructure.shared_state.get_shared_state_backend", forbidden)
    monkeypatch.setattr("core.operations.preopen_evidence.PreopenEvidenceStore.load", forbidden)
    monkeypatch.setattr("core.realtime.market_journal.MarketJournal.__init__", forbidden)
    monkeypatch.setattr("core.data.providers.eltdx_provider.EltdxProvider.__init__", forbidden)
    report = replay_snapshot_inputs(sample())
    assert report["confirmed_stocks"] == 1


def test_web_loader_snapshot_path_never_initialises_opening_or_market_stores(tmp_path, monkeypatch):
    from core.realtime.day_replay import DayReplayLoader

    def forbidden(*a, **kw):
        pytest.fail("Snapshot loader must not initialise live state")

    monkeypatch.setattr("core.realtime.session_open_repository.SessionOpenRepository.__init__", forbidden)
    monkeypatch.setattr("core.realtime.quote_cache.RealtimeQuoteCache.__init__", forbidden)
    monkeypatch.setattr("backtest.trade_calendar.TradeCalendar.__init__", forbidden)
    loader = DayReplayLoader(web_data_dir=tmp_path)
    report = loader.run(sample()["request"])
    assert report["replay_mode"] == "archived_snapshot"
    assert report["failures"] and not report["stocks"]


def test_real_record_reconciliation_excludes_later_confirmations_and_deliveries():
    from core.realtime.runtime_diagnostics import attach_runtime_diagnostics

    report = replay_snapshot_inputs(sample())
    report["recorded_runtime"] = {
        "signal_ledger": {
            "rows": [
                {
                    "code": "000001",
                    "evidence_kind": "runtime_observation",
                    "observed_at": stamp("09:36:03"),
                    "evidence": {"status": "observe", "profile": "production", "reason": "recorded blocker"},
                },
                {
                    "code": "000001",
                    "evidence_kind": "runtime_observation",
                    "observed_at": stamp("09:36:05"),
                    "evidence": {"status": "confirmed", "profile": "production", "confirm_time": "09:36:05"},
                },
            ]
        }
    }
    attach_runtime_diagnostics(report)
    audit = report["stocks"][0]["runtime_diagnostic"]
    assert not audit["production_confirmed"]
    assert audit["confirmation_comparison"]["before"]["reason"] == "recorded blocker"
    assert audit["confirmation_comparison"]["after"] is None
    report["recorded_runtime"]["confirmation_history"] = {
        "rows": [
            {
                "code": "000001",
                "first_confirmed_at": stamp("09:36:00"),
                "updated_at": stamp("15:00:00"),
                "evidence": {"status": "confirmed", "profile": "production", "reason": "mutable closing summary"},
            }
        ]
    }
    attach_runtime_diagnostics(report)
    assert not report["stocks"][0]["runtime_diagnostic"]["production_confirmed"]


def test_new_archive_kind_round_trips_with_visibility_time(tmp_path):
    values = sample()
    journal = write_inputs(tmp_path, values)
    assert journal.archive_once() == 9  # Five minute versions, two sector events, two snapshots.
    loaded = load_snapshot_inputs(tmp_path, values["request"])
    assert replay_snapshot_inputs(loaded)["stocks"][0]["ever_confirmed"]
    with sqlite3.connect(journal.path) as con:
        assert con.execute("SELECT count(*) FROM market_events").fetchone()[0] == 0


def test_replay_does_not_use_snapshot_before_it_was_persisted(tmp_path):
    values = sample()
    journal = write_inputs(tmp_path, values)
    later = datetime.fromisoformat(stamp("09:36:13"))
    with sqlite3.connect(journal.path) as con:
        con.execute("UPDATE market_events SET persisted_ts=? WHERE kind='snapshot' AND received_at=?",
                    (later.timestamp(), stamp("09:36:03")))
    early = load_snapshot_inputs(tmp_path, values["request"])
    assert sum(e["kind"] == "snapshot" for e in early["events"]) == 1
    loaded = load_snapshot_inputs(tmp_path, {**values["request"], "until": "09:36:20"})
    delayed = next(e for e in loaded["events"] if e["kind"] == "snapshot" and e["received_at"] == stamp("09:36:03"))
    assert delayed["visible_at"] == stamp("09:36:13")
    assert delayed["payload"]["received_at"] == stamp("09:36:03")
    assert not replay_snapshot_inputs(loaded)["stocks"][0]["ever_confirmed"]


def test_snapshot_job_keeps_baseline_inputs_without_reloading_archives(tmp_path):
    from core.infrastructure.atomic_json import atomic_write_json
    from core.realtime.day_replay_jobs import DayReplayJobs

    jobs = DayReplayJobs(tmp_path, loader_factory=lambda: pytest.fail("Baseline must not reload archives"))
    values = sample()
    first, second = "a" * 32, "b" * 32
    report = replay_snapshot_inputs(values)
    report["run_id"] = first
    atomic_write_json(jobs.path(first, "inputs.json"), values)
    atomic_write_json(jobs.path(first, "report.json"), report)
    status = {"run_id": second, "request": values["request"], "state": "queued", "baseline_id": first}
    jobs._run(status)
    assert status["state"] == "done", status
    actual = jobs.get(second, report=True)
    assert actual["comparison"]["comparable"]
    assert actual["stocks"][0]["first_confirm_time"] == "09:36:03"
    started = jobs.start({**values["request"], "baseline_id": first})
    jobs.thread.join(timeout=10)
    assert not jobs.thread.is_alive()
    assert jobs.get(started["run_id"])["state"] == "done"


def test_archive_spool_duplicate_is_not_replayed_twice(tmp_path):
    values = sample()
    journal = write_inputs(tmp_path, values)
    with sqlite3.connect(journal.path) as con:
        row = con.execute("SELECT event_id,kind,code,received_at,payload FROM market_events LIMIT 1").fetchone()
    with sqlite3.connect(tmp_path / "market_archive" / f"market_{DATE}.sqlite3") as con:
        con.execute("CREATE TABLE market_events(event_id TEXT,kind TEXT,code TEXT,received_at TEXT,payload TEXT)")
        con.execute("INSERT INTO market_events VALUES (?,?,?,?,?)", row)
    loaded = load_snapshot_inputs(tmp_path, values["request"])
    assert len(loaded["events"]) == 9
    assert replay_snapshot_inputs(loaded)["stocks"][0]["snapshot_count"] == 2


def test_baseline_freshness_policy_is_part_of_captured_input_hash(monkeypatch):
    before = replay_snapshot_inputs(sample("09:36:20"))
    monkeypatch.setattr("config.settings.REALTIME_QUOTE_STALE_SECONDS", 90)
    after = replay_saved_inputs(before["replay_inputs"])
    assert before["input_hash"] == after["input_hash"]
    assert before["stocks"] == after["stocks"]
    fresh = replay_snapshot_inputs(sample("09:36:20"))
    assert before["input_hash"] != fresh["input_hash"]


def test_missing_directory_stays_absent(tmp_path):
    root = tmp_path / "absent"
    loaded = load_snapshot_inputs(root, sample()["request"])
    assert not root.exists()
    assert loaded["load_failures"]


def write_inputs(root, values):
    from core.infrastructure.atomic_json import atomic_write_json

    atomic_write_json(root / "collection_plans" / f"{DATE}.json", values["plan"])
    atomic_write_json(root / "collection_plans" / f"{DATE}.inputs.json", values["bundle"])
    from core.realtime.market_journal import MarketJournal

    journal = MarketJournal(root / "market_archive")
    for e in values["events"]:
        journal.clock = lambda event=e: datetime.fromisoformat(event["received_at"])
        journal.append(e["kind"], DATE, [e["payload"]])
    return journal


def test_archive_and_pending_spool_are_readonly_deduplicated_and_baseline_is_frozen(tmp_path):
    values = sample()
    journal = write_inputs(tmp_path, values)
    assert journal.archive_once(limit=2) == 2
    loaded = load_snapshot_inputs(tmp_path, values["request"])
    assert len(loaded["events"]) == 9
    before = replay_snapshot_inputs(loaded)
    journal.append(
        "snapshot", DATE, [{**values["events"][-1]["payload"], "received_at": stamp("09:36:04"), "time": "09:36:04"}]
    )
    after = replay_saved_inputs(deepcopy(before["replay_inputs"]))
    assert before["input_hash"] == after["input_hash"]
    assert before["stocks"] == after["stocks"]
    with sqlite3.connect(journal.path) as con:
        assert con.execute("SELECT count(*) FROM market_events").fetchone()[0] == 8


def test_corrupt_archive_is_explicit_not_created_or_repaired(tmp_path):
    values = sample()
    write_inputs(tmp_path, values)
    path = tmp_path / "market_archive" / f"market_{DATE}.sqlite3"
    path.write_bytes(b"bad database")
    loaded = load_snapshot_inputs(tmp_path, values["request"])
    assert any(f["evidence"] == "unreadable_archive" for f in loaded["load_failures"])
    assert path.read_bytes() == b"bad database"


def test_request_keeps_seconds_and_refuses_fetch_and_nonfrozen_scope():
    base = {"market_date": DATE, "until": "09:36:03", "replay_mode": "archived_snapshot"}
    assert replay_request(base)["until"] == "09:36:03"
    assert replay_request({**base, "replay_mode": "minute"})["until"] == "09:36:00"
    for extra in ({"fetch_missing": True}, {"lookback": 5}, {"replay_mode": "unknown"}):
        with pytest.raises(ValueError):
            replay_request({**base, **extra})


def test_sector_publish_archive_records_visibility_after_shared_write(tmp_path):
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.realtime.market_journal import MarketJournal
    from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
    from core.realtime.trading_session import shanghai_now

    backend = MemoryStateBackend()
    journal = MarketJournal(tmp_path / "market_archive")
    repo = SectorQuoteMinuteRepository(
        tmp_path / "cache",
        backend=backend,
        journal=journal,
        clock=lambda: shanghai_now(datetime.fromisoformat(stamp("09:36:03"))),
    )
    point = {
        "code": "000002",
        "date": DATE,
        "source": "eltdx_batch",
        "last_price": 10.1,
        "received_at": stamp("09:35:59"),
        "pre_close": 10,
        "open_price": 10,
    }
    assert repo.record_batch([point], now=datetime.fromisoformat(stamp("09:36:00"))) == 1
    with sqlite3.connect(journal.path) as con:
        payload = json.loads(con.execute("SELECT payload FROM market_events WHERE kind='sector_minutes'").fetchone()[0])
    assert payload["received_at"] == stamp("09:36:03")
    assert payload["points"][0]["time"] == "09:36:00"
    assert backend.get_json(repo._key(DATE, "000002")) is not None


def test_supplemental_evidence_saved_with_inputs_never_changes_buy_rules():
    from core.realtime.market_evidence import EvidenceResult, fingerprint
    inputs = sample()
    baseline = replay_snapshot_inputs(inputs)
    value = EvidenceResult('auction_series', '000001', DATE, received_at=stamp('09:36:00'),
                           rows=[{'event_time': stamp('09:24:00')}]).envelope(plan_id=inputs['plan']['plan_id'])
    value['observation_id'] = fingerprint(value)
    marker = dict(code='000001', received_at=stamp('09:36:03'), published_at=stamp('09:36:03'),
                  market_date=DATE, capture_mode='live', collection_plan_id=inputs['plan']['plan_id'],
                  observation_id=value['observation_id'])
    inputs['evidence_events'] = [
        dict(event_id='observation',kind='auction_series',code='000001',received_at=value['received_at'],payload=value),
        dict(event_id='publication',kind='evidence_publication',code='000001',received_at=marker['received_at'],payload=marker)]
    report = replay_snapshot_inputs(inputs)
    assert report['stocks'] == baseline['stocks']
    assert report['supplemental_evidence']['event_counts'] == {'auction_series': 1}
    assert not report['supplemental_evidence']['strategy_affected']
