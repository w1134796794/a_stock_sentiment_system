import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_snapshot_archive_replay import DATE, sample, stamp, write_inputs

from core.realtime.collection_plan import digest
from core.realtime.market_readiness import AuditLimits, at, audit_market, calendar_day, continuity


def calendar(root):
    path = root / "calendar.csv"
    path.write_text(
        "exchange,cal_date,is_open\nSSE,2026-10-21,1\nSSE,2026-10-22,1\nSSE,2026-10-23,1\nSSE,2026-10-24,0\n",
        encoding="utf-8",
    )
    return path


def healthy_inputs(root):
    values = sample()
    values["bundle"]["health"] = {"ok": True}
    values["plan"]["inputs_hash"] = digest(values["bundle"])
    values["plan"]["plan_id"] = digest({k: v for k, v in values["plan"].items() if k != "plan_id"})
    values["events"] = []
    now = at(DATE, "09:36:06")
    ticks = []
    for index in range(123):
        point = at(DATE, "09:30:00") + timedelta(seconds=index * 3)
        ticks.append(
            {
                "code": "000001",
                "date": DATE,
                "received_at": point.isoformat(),
                "time": point.strftime("%H:%M:%S"),
                "source": "eltdx_batch",
                "quality_ok": True,
                "last_price": 10.1,
                "open_price": 9.9,
                "pre_close": 10,
                "vol_hand": 10000 + index,
                "amount_yuan": 10000000 + index * 1000,
            }
        )
    values["events"].extend({"kind": "snapshot", "payload": point} for point in ticks)
    bars = [
        dict(time=f"09:{minute}:00", open=9.9, high=10, low=9.8, close=9.95, volume=10000, amount=99500)
        for minute in range(31, 36)
    ]
    values["events"].append(
        {"kind": "minutes", "payload": {"code": "000001", "received_at": stamp("09:36:01"), "bars": bars}}
    )
    points = {}
    for code in ("000002", "000003"):
        for minute in range(31, 37):
            end = at(DATE, f"09:{minute}:00")
            point = {
                "code": code,
                "time": end.strftime("%H:%M:%S"),
                "price": 10.1,
                "pre_close": 10,
                "open_price": 10,
                "source": "eltdx_batch",
                "received_at": (end - timedelta(seconds=1)).isoformat(),
            }
            points[code, point["time"]] = point
            values["events"].append(
                {
                    "kind": "sector_minutes",
                    "payload": {
                        "code": code,
                        "date": DATE,
                        "received_at": (end + timedelta(seconds=1)).isoformat(),
                        "source": "collector_closed_quote_minutes",
                        "points": [point],
                    },
                }
            )
    from core.infrastructure.atomic_json import atomic_write_json
    from core.realtime.market_journal import MarketJournal

    atomic_write_json(root / "collection_plans" / f"{DATE}.json", values["plan"])
    atomic_write_json(root / "collection_plans" / f"{DATE}.inputs.json", values["bundle"])
    journal = MarketJournal(root / "market_archive")
    for kind in ("snapshot", "minutes", "sector_minutes"):
        journal.append(kind, DATE, [e["payload"] for e in values["events"] if e["kind"] == kind])
    ledger = root / "ledger.sqlite3"
    with sqlite3.connect(ledger) as con:
        con.execute(
            "CREATE TABLE runtime_observations(code TEXT, market_date TEXT, profile TEXT, observed_at TEXT, payload_json TEXT)"
        )
        for minute in range(30, 37):
            con.execute(
                "INSERT INTO runtime_observations VALUES (?,?,?,?,?)",
                (
                    "000001",
                    DATE,
                    "production",
                    stamp(f"09:{minute}:03"),
                    json.dumps({"evaluation_timing": {"quote_age_at_completion_seconds": 3.6, "duration_ms": 654}}),
                ),
            )
    health = {
        "realtime:collector:health": {
            "ok": True,
            "updated_at": now.isoformat(),
            "lease_owned": True,
            "collection_plan_id": values["plan"]["plan_id"],
        },
        "realtime:archive:health": {"ok": True, "updated_at": now.isoformat(), "pending": 0},
        "realtime:strategy:health": {
            "ok": True,
            "updated_at": now.isoformat(),
            "plan_id": values["plan"]["plan_id"],
            "market_date": DATE,
            "duration_ms": 654,
        },
        "realtime:quotes:latest:000001": ticks[-1],
        "realtime:minutes:20261022:000001": bars,
    }

    class Live:
        def many(self, keys):
            return {key: health.get(key) for key in keys}

        def ticks(self, date, codes, start, end):
            return {
                "000001": [point for point in ticks if start <= datetime.fromisoformat(point["received_at"]) <= end]
            }

        def sector_points(self, date, codes, minutes):
            return {key: value for key, value in points.items() if key[1] in minutes}

    return values, now, ledger, Live(), health, ticks, points, journal


def run(root, now, ledger, live=None, **kw):
    return audit_market(
        root=root, calendar_path=calendar(root), ledger_path=ledger, date=DATE, now=now, live=live, **kw
    )


def test_live_and_recent_replay_ready_do_not_require_buy_signal(tmp_path):
    _, now, ledger, live, *_ = healthy_inputs(tmp_path)
    report = run(tmp_path, now, ledger, live)
    assert report["status"] == "ready", [item for item in report["checks"] if item["status"] != "pass"]
    assert report["live_ready"] is True and report["replay_ready"] is True
    assert report["whole_day_replay_ready"] is None
    assert report["archive_window"]["scope"] == "recent_window_only"
    assert not report["notification_sent"] and not report["remote_market_fetch"]


def test_preopen_does_not_demand_evidence_not_yet_due(tmp_path):
    values, _, ledger, live, *_ = healthy_inputs(tmp_path)
    report = run(tmp_path, at(DATE, "09:20:00"), ledger, live)
    assert report["live_ready"] is True and report["replay_ready"] is None
    assert not any(c["key"].startswith("snapshots:") for c in report["checks"])


def test_missing_real_calendar_never_falls_back_to_weekdays(tmp_path):
    report = audit_market(
        root=tmp_path,
        calendar_path=tmp_path / "missing.csv",
        ledger_path=tmp_path / "missing.db",
        date=DATE,
        now=at(DATE, "09:20:00"),
    )
    assert report["status"] == "not_ready"
    assert not (tmp_path / "missing.csv").exists()
    with pytest.raises(ValueError):
        calendar_day(calendar(tmp_path), "20270101")


def test_holiday_skips_without_redis_or_data(tmp_path):
    report = audit_market(
        root=tmp_path / "absent",
        calendar_path=calendar(tmp_path),
        ledger_path=tmp_path / "absent.db",
        date="20261024",
        now=at("20261024", "09:40:00"),
    )
    assert report["status"] == "skipped_non_trading_day"
    assert report["live_ready"] is None and not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "kind",
    ["stale_quote", "dead_collector", "plan_mismatch", "archive_lag", "missing_hot_minutes", "missing_hot_sector"],
)
def test_live_failure_is_distinct_from_healthy_persistent_archive(tmp_path, kind):
    _, now, ledger, live, health, ticks, points, _ = healthy_inputs(tmp_path)
    if kind == "stale_quote":
        health["realtime:quotes:latest:000001"] = {**ticks[-1], "received_at": stamp("09:35:00"), "time": "09:35:00"}
    elif kind == "dead_collector":
        health["realtime:collector:health"]["lease_owned"] = False
    elif kind == "plan_mismatch":
        health["realtime:strategy:health"]["plan_id"] = "wrong"
    elif kind == "archive_lag":
        health["realtime:archive:health"].update(pending=50, oldest_pending_at=stamp("09:30:00"))
    elif kind == "missing_hot_minutes":
        health["realtime:minutes:20261022:000001"] = []
    else:
        points.clear()
    report = run(tmp_path, now, ledger, live)
    assert report["live_ready"] is False and report["replay_ready"] is True


def test_archive_gap_does_not_masquerade_as_complete_day(tmp_path):
    _, now, ledger, live, _, _, _, journal = healthy_inputs(tmp_path)
    with sqlite3.connect(journal.path) as con:
        con.execute(
            "DELETE FROM market_events WHERE kind='snapshot' AND received_at>? AND received_at<?",
            (stamp("09:34:00"), stamp("09:35:00")),
        )
    report = run(tmp_path, now, ledger, live)
    assert report["live_ready"] is True and report["replay_ready"] is False
    assert next(c for c in report["checks"] if c["key"] == "snapshots:000001")["details"]["gap_count"] > 0


def test_missing_visibility_record_cannot_be_replaced_by_minute_file(tmp_path):
    _, now, ledger, live, _, _, _, journal = healthy_inputs(tmp_path)
    with sqlite3.connect(journal.path) as con:
        con.execute("DELETE FROM market_events WHERE kind='sector_minutes'")
    report = run(tmp_path, now, ledger, live)
    assert report["replay_ready"] is False and report["live_ready"] is True


@pytest.mark.parametrize(
    "field,value", [("received_at", "2026-10-21T09:32:59+08:00"), ("time", "09:32:00"), ("code", "999999")]
)
def test_hot_sector_pair_rejects_wrong_identity_or_day(tmp_path, field, value):
    _, now, ledger, live, _, _, points, _ = healthy_inputs(tmp_path)
    for code in ("000002", "000003"):
        points[code, "09:33:00"] = {**points[code, "09:33:00"], field: value}
    report = run(tmp_path, now, ledger, live)
    assert report["live_ready"] is False and report["replay_ready"] is True
    sector = next(c for c in report["checks"] if c["key"] == "hot_sector:000001")
    assert sector["details"]["coverage"] == 1 and sector["details"]["paired_coverage"] == 0


def test_lunch_break_is_not_a_snapshot_gap():
    left, right = at(DATE, "11:30:00"), at(DATE, "13:00:00")
    stamps = [left - timedelta(seconds=3), left, right, right + timedelta(seconds=3)]
    assert continuity(stamps, DATE, stamps[0], stamps[-1], 8)["ok"]


def test_completion_quote_age_is_not_evaluation_duration(tmp_path):
    _, now, ledger, live, health, *_ = healthy_inputs(tmp_path)
    health["realtime:strategy:health"]["duration_ms"] = 38000
    report = run(tmp_path, now, ledger, live)
    assert report["live_ready"] is True and report["status"] == "warning"
    age = next(c for c in report["checks"] if c["key"] == "completion_quote_age")
    assert age["status"] == "pass" and age["details"]["max_seconds"] == 3.6
    assert age["details"]["max_evaluation_ms"] == 654


def test_no_data_store_provider_or_market_journal_initialisation(tmp_path, monkeypatch):
    _, now, ledger, live, *_ = healthy_inputs(tmp_path)

    def forbidden(*a, **kw):
        pytest.fail("Readiness audit must be strictly read-only")

    monkeypatch.setattr("core.infrastructure.shared_state.get_shared_state_backend", forbidden)
    monkeypatch.setattr("core.realtime.market_journal.MarketJournal.__init__", forbidden)
    monkeypatch.setattr("core.data.providers.eltdx_provider.EltdxProvider.__init__", forbidden)
    monkeypatch.setattr("core.infrastructure.atomic_json.atomic_read_json", forbidden)
    calendar_path = calendar(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.sqlite3")}
    before_files = set(tmp_path.rglob("*"))
    report = audit_market(root=tmp_path, calendar_path=calendar_path, ledger_path=ledger, date=DATE, now=now, live=live)
    assert report["live_ready"] is True
    assert before == {p: p.read_bytes() for p in before}
    created = set(tmp_path.rglob("*")) - before_files
    assert all(p.name.endswith((".sqlite3-shm", ".sqlite3-wal")) for p in created)


def test_read_only_daily_archive_handoff_is_deduplicated(tmp_path):
    _, now, ledger, live, _, _, _, journal = healthy_inputs(tmp_path)
    with sqlite3.connect(journal.path) as con:
        row = con.execute(
            "SELECT event_id,kind,code,received_at,payload FROM market_events WHERE kind='snapshot' LIMIT 1"
        ).fetchone()
    with sqlite3.connect(tmp_path / "market_archive" / f"market_{DATE}.sqlite3") as con:
        con.execute("CREATE TABLE market_events(event_id TEXT,kind TEXT,code TEXT,received_at TEXT,payload TEXT)")
        con.execute("INSERT INTO market_events VALUES (?,?,?,?,?)", row)
    assert run(tmp_path, now, ledger, live)["status"] == "ready"


def test_systemd_readiness_timer_is_explicit_and_never_controls_workers(tmp_path, monkeypatch):
    from scripts.install_market_readiness_systemd import SERVICE, TIMER, main, render_units

    units = render_units("/opt/a-stock", "deploy")
    assert set(units) == {SERVICE, TIMER}
    assert "User=deploy" in units[SERVICE]
    assert "--notifications" not in units[SERVICE] and "Restart=always" not in units[SERVICE]
    assert "09:20:00 Asia/Shanghai" in units[TIMER] and "15:15:00 Asia/Shanghai" in units[TIMER]
    assert "Persistent=false" in units[TIMER]
    monkeypatch.setattr(
        "scripts.install_market_readiness_systemd.subprocess.run",
        lambda *a, **kw: pytest.fail("Preview cannot call systemctl"),
    )
    assert main(["--user", "deploy", "--project-root", "/opt/a-stock", "--output-dir", str(tmp_path)]) == 0
    for invalid in ("relative", "/opt/%n", "/opt/a\nUser=root"):
        with pytest.raises(ValueError):
            render_units(invalid, "deploy")


def test_postclose_all_day_allows_final_minute_received_after_1500(tmp_path):
    _, _, ledger, _, _, _, _, journal = healthy_inputs(tmp_path)
    snapshots, publications, bars, observations = [], [], [], []
    for start_time, end_time in (("09:30:00", "11:30:00"), ("13:00:00", "15:00:00")):
        point, end = at(DATE, start_time), at(DATE, end_time)
        while point <= end:
            index = len(snapshots)
            snapshots.append(
                {
                    "code": "000001",
                    "date": DATE,
                    "time": point.strftime("%H:%M:%S"),
                    "received_at": point.isoformat(),
                    "source": "eltdx_batch",
                    "quality_ok": True,
                    "last_price": 10.1,
                    "pre_close": 10,
                    "open_price": 9.9,
                    "amount_yuan": 10000000 + 1000 * index,
                    "vol_hand": 10000 + index,
                }
            )
            point += timedelta(seconds=3)
        minute = at(DATE, start_time) + timedelta(minutes=1)
        while minute <= end:
            bars.append(
                {
                    "time": minute.strftime("%H:%M:%S"),
                    "open": 9.9,
                    "high": 10,
                    "low": 9.8,
                    "close": 9.95,
                    "volume": 10000,
                    "amount": 99500,
                }
            )
            for code in ("000002", "000003"):
                publications.append(
                    {
                        "code": code,
                        "date": DATE,
                        "source": "collector_closed_quote_minutes",
                        "received_at": (minute + timedelta(seconds=1)).isoformat(),
                        "points": [
                            {
                                "code": code,
                                "time": minute.strftime("%H:%M:%S"),
                                "price": 10.1,
                                "open_price": 10,
                                "pre_close": 10,
                                "source": "eltdx_batch",
                                "received_at": (minute - timedelta(seconds=1)).isoformat(),
                            }
                        ],
                    }
                )
            observations.append(
                (
                    "000001",
                    DATE,
                    "production",
                    (minute - timedelta(seconds=5)).isoformat(),
                    json.dumps({"evaluation_timing": {"duration_ms": 654, "quote_age_at_completion_seconds": 3.6}}),
                )
            )
            minute += timedelta(minutes=1)
    with sqlite3.connect(journal.path) as con:
        con.execute("DELETE FROM market_events")
    journal.append("snapshot", DATE, snapshots)
    journal.append("sector_minutes", DATE, publications)
    journal.append("minutes", DATE, [{"code": "000001", "received_at": stamp("15:01:00"), "bars": bars}])
    with sqlite3.connect(ledger) as con:
        con.execute("DELETE FROM runtime_observations")
        con.executemany("INSERT INTO runtime_observations VALUES (?,?,?,?,?)", observations)
    report = run(tmp_path, at(DATE, "15:15:00"), ledger)
    assert report["whole_day_replay_ready"] is True, [c for c in report["checks"] if c["status"] == "fail"]
    assert report["live_ready"] is None and report["archive_window"]["scope"] == "full_day"


def test_optional_shadow_missing_never_becomes_formal_gate(tmp_path):
    _, now, ledger, live, *_ = healthy_inputs(tmp_path)
    report = run(tmp_path, now, ledger, live, board_shadow=True)
    assert report["board_shadow_ready"] is False and report["status"] == "warning"
    assert report["live_ready"] is True and report["replay_ready"] is True


def test_cli_holiday_report_is_saved_and_does_not_connect_to_redis(tmp_path, monkeypatch, capsys):
    from scripts.check_market_readiness import main

    monkeypatch.setattr("core.realtime.trading_session.shanghai_now", lambda *a: at("20261024", "09:40:00"))
    monkeypatch.setattr(
        "core.realtime.market_readiness.RedisAuditReader.__init__",
        lambda *a, **kw: pytest.fail("Holiday must not connect"),
    )
    assert (
        main(
            [
                "--date",
                "20261024",
                "--calendar",
                str(calendar(tmp_path)),
                "--data-root",
                str(tmp_path / "absent"),
                "--output-dir",
                str(tmp_path / "reports"),
            ]
        )
        == 0
    )
    report = json.loads((tmp_path / "reports/latest.json").read_text(encoding="utf-8"))
    assert report["status"] == "skipped_non_trading_day"
    assert not (tmp_path / "absent").exists()


def test_cli_missing_frozen_inputs_returns_failure_without_creating_sources(tmp_path, monkeypatch, capsys):
    from scripts.check_market_readiness import main

    monkeypatch.setattr("core.realtime.trading_session.shanghai_now", lambda *a: at("20261024", "16:00:00"))
    assert (
        main(
            [
                "--date",
                DATE,
                "--phase",
                "postclose",
                "--offline",
                "--calendar",
                str(calendar(tmp_path)),
                "--data-root",
                str(tmp_path / "absent"),
                "--ledger",
                str(tmp_path / "absent.db"),
                "--output-dir",
                str(tmp_path / "reports"),
            ]
        )
        == 2
    )
    report = json.loads((tmp_path / "reports/latest.json").read_text(encoding="utf-8"))
    assert report["status"] == "not_ready" and report["whole_day_replay_ready"] is False
    assert not (tmp_path / "absent").exists() and not (tmp_path / "absent.db").exists()
