from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime

import pytest

from core.infrastructure.duckdb_access import connect
from core.infrastructure.shared_state import MemoryStateBackend
from core.operations.health import gate_realtime_payload, intraday_health, postclose_health
from core.operations.preopen_evidence import PreopenEvidenceStore
from core.realtime.watchlist_repository import RealtimeWatchlistRepository


def test_duckdb_reader_releases_lock_before_subprocess_writer(tmp_path):
    database = tmp_path / "shared.duckdb"
    with connect(database) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
    program = """
import sys
from core.infrastructure.duckdb_access import connect
with connect(sys.argv[1], timeout=5) as con:
    con.execute('INSERT INTO sample VALUES (7)')
print('written', flush=True)
"""
    with connect(database, read_only=True) as reader:
        reader.execute("SELECT * FROM sample").fetchall()
        child = subprocess.Popen([sys.executable, "-c", program, str(database)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.3)
        assert child.poll() is None
    try:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert "written" in stdout
        with connect(database, read_only=True) as reader:
            assert reader.execute("SELECT value FROM sample").fetchall() == [(7,)]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_duckdb_failed_connect_and_query_release_mutex(tmp_path):
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(Exception):
        connect(missing, read_only=True, timeout=0.1)
    with pytest.raises(Exception):
        with connect(missing, timeout=0.1) as con:
            con.execute("SELECT * FROM nonexistent")
    with connect(missing, timeout=0.1) as con:
        assert con.execute("SELECT 1").fetchone() == (1,)


def _freeze(store):
    return store.freeze("20261009", "20261012", now=datetime(2026, 10, 11, 20),
                        health={"ok": True, "trade_date": "20261009", "reasons": []},
                        inputs={"candidates": [{"code": "002733"}],
                                "artifacts": {"fetch": {"fetched_at": "2026-10-09T20:00:00"}}})


def test_preopen_is_immutable_and_health_ignores_later_research_manifest(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    first = _freeze(store)
    assert first["ok"]
    retry = store.freeze("20261009", "20261012", now=datetime(2026, 10, 12, 10),
                         health={"ok": False}, inputs={"candidates": []})
    assert retry["existing"] and retry["bundle_id"] == first["bundle_id"]
    root = tmp_path / "fetch_status"
    root.mkdir()
    (root / "fetch_20261009.json").write_text(json.dumps({"fetched_at": "2026-10-12T09:42:43"}))
    result = postclose_health("20261009", as_of_market_date="20261012", web_data_dir=tmp_path)
    assert result["ok"] and result["evidence_bundle_id"] == first["bundle_id"]
    assert store.load("20261009", "20261012")["inputs"]["candidates"] == [{"code": "002733"}]


def test_preopen_cannot_be_created_during_or_after_session(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    for now in (datetime(2026, 10, 12, 9, 25), datetime(2026, 10, 12, 9, 41)):
        result = store.freeze("20261009", "20261012", now=now, health={"ok": True}, inputs={})
        assert not result["ok"] and result["reason"] == "past_preopen_cutoff"
    assert not store.path("20261009", "20261012").exists()


def test_preopen_tampering_fails_closed(tmp_path):
    store = PreopenEvidenceStore(tmp_path)
    _freeze(store)
    path = store.path("20261009", "20261012")
    payload = json.loads(path.read_text())
    payload["inputs"]["candidates"] = []
    path.write_text(json.dumps(payload))
    result = postclose_health("20261009", as_of_market_date="20261012", web_data_dir=tmp_path)
    assert not result["ok"]


def test_strategy_view_uses_frozen_profile_candidates_not_mutable_screening(tmp_path, monkeypatch):
    from core.realtime.overlay_service import RealtimeOverlayService
    from types import SimpleNamespace
    store = PreopenEvidenceStore(tmp_path)
    store.freeze("20261009", "20261012", now=datetime(2026, 10, 11, 20), health={"ok": True}, inputs={
        "candidates": [{"code": "002733"}],
        "candidates_by_profile": {"first_board_launch": [{"code": "600000", "name": "frozen", "score": 80}]},
        "profiles": [{"id": "first_board_launch", "version": "before_open", "execution": {}}],
    })
    service = RealtimeOverlayService(screening_dir=tmp_path / "screening",
                                     quote_service=SimpleNamespace(get_quotes=lambda codes: {"quotes": []}))
    monkeypatch.setattr(service, "_load_candidates", lambda *a, **kw: pytest.fail("must not read rewritten candidates"))
    result = service.build_overlay("20261009", market_date="20261012", profile="first_board_launch")
    assert [row["code"] for row in result["rows"]] == ["600000"]
    assert result["strategy"]["version"] == "before_open"
    assert result["evidence_bundle_id"]


def test_sector_snapshot_watchlist_does_not_expand_minute_downloads():
    lists = RealtimeWatchlistRepository(MemoryStateBackend("minute_watchlist_separation"))
    lists.publish("candidates", ["002733"])
    lists.publish("sectors", ["002733", "600000", "000001"], minutes=False)
    assert set(lists.read_all()) == {"002733", "600000", "000001"}
    assert lists.read_all(minutes_only=True) == ["002733"]


def test_constituents_update_quotes_without_storing_unused_tick_history():
    from core.realtime.quote_cache import RealtimeQuoteCache
    from types import SimpleNamespace
    recorded = []
    cache = RealtimeQuoteCache(backend=MemoryStateBackend("constituent_quote_only"),
                              snapshot_repository=SimpleNamespace(append_batch=lambda date, rows: recorded.extend(rows)))
    now = datetime.now()
    rows = [{"code": code, "last_price": 10, "pre_close": 9, "date": now.strftime("%Y%m%d"),
             "time": now.strftime("%H:%M:%S"), "received_at": now.isoformat()}
            for code in ("002733", "600000")]
    result = cache.write_batch(rows, source="eltdx_batch", collector_id="test", snapshot_codes=["002733"])
    assert result["count"] == 2
    assert [row["code"] for row in recorded] == ["002733"]


def test_collector_recovery_starts_new_live_evidence_interval(monkeypatch):
    import core.realtime.eltdx_collector as module
    from types import SimpleNamespace
    clock = [datetime(2026, 10, 12, 9, 41, 24)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(module, "datetime", Clock)
    collector = module.EltdxQuoteCollector(clock=lambda: clock[0])
    collector.targets = lambda **kw: ["002733"]
    collector.provider = SimpleNamespace(get_quote_snapshots=lambda codes: {"002733": {"code": "002733"}})
    collector.quote_cache = SimpleNamespace(
        write_batch=lambda *a, **kw: {"ok": True, "count": 1},
        backend=MemoryStateBackend("collector_recovery"))
    collector._schedule_minute_sync = lambda codes: None
    assert collector.collect_once()["ok"]
    assert collector._session_started_at.startswith("2026-10-12T09:41:24")
    clock[0] = datetime(2026, 10, 12, 9, 41, 27)
    collector.collect_once()
    assert collector._session_started_at.startswith("2026-10-12T09:41:24")
    clock[0] = datetime(2026, 10, 12, 9, 42, 10)
    collector.collect_once()
    assert collector._session_started_at.startswith("2026-10-12T09:42:10")


def test_live_gate_rejects_minute_signal_from_collector_downtime():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 12, 9, 43, tzinfo=ZoneInfo("Asia/Shanghai"))
    collector = {"storage": "redis", "trade_date": "20261012", "collector": {
        "ok": True, "lease_owned": True, "updated_at": "2026-10-12T09:42:59",
        "session_started_at": "2026-10-12T09:41:24",
    }}
    row = {"confirm_status": "confirmed", "confirm_time": "09:35:00",
           "received_at": "2026-10-12T09:42:59", "sector_detail": {
               "observed_at": "2026-10-12T09:42:59", "member_count": 80,
               "observed_members": 80, "data_completeness": 1.0,
           }}
    result = gate_realtime_payload({"market_date": "20261012", "rows": [row]},
                                   collector=collector, now=now, require_shared=True)
    assert result["rows"][0]["confirm_status"] == "observe"
    assert "采集空窗" in row["reason"]


@pytest.mark.parametrize("confirm_time", ["09:35:00", "09:44:00", ""])
def test_fresh_quotes_do_not_make_historical_or_invalid_confirmation_live(confirm_time):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 10, 12, 9, 43, tzinfo=ZoneInfo("Asia/Shanghai"))
    collector = {"storage": "redis", "trade_date": "20261012", "collector": {
        "ok": True, "lease_owned": True, "updated_at": "2026-10-12T09:42:59",
        "session_started_at": "2026-10-12T09:30:00",
    }}
    row = {"confirm_time": confirm_time, "received_at": "2026-10-12T09:42:59", "sector_detail": {
        "observed_at": "2026-10-12T09:42:59", "member_count": 80, "observed_members": 80,
        "data_completeness": 1.0,
    }}
    assert not intraday_health(row, "20261012", collector=collector, now=now)["ok"]
    row["confirm_time"] = "09:42:30"
    assert intraday_health(row, "20261012", collector=collector, now=now)["ok"]


def test_preopen_producer_captures_daily_and_genuine_constituents(tmp_path, monkeypatch):
    import core.operations.health as health_module
    import core.realtime.overlay_service as overlay_module
    import core.realtime.leader_pool_service as leader_module
    import core.realtime.sector_service as sector_module
    from core.portfolio.holding_repository import HoldingRepository
    from core.operations.preopen_evidence import freeze_preopen_inputs
    from types import SimpleNamespace

    database = tmp_path / "factors.duckdb"
    with connect(database) as con:
        con.execute("CREATE TABLE stock_daily_silver(trade_date VARCHAR, code VARCHAR, close DOUBLE)")
        con.execute("INSERT INTO stock_daily_silver VALUES ('20261009', '002733', 10)")
        con.execute("CREATE TABLE factor_stock_wide AS SELECT * FROM stock_daily_silver")
    paths = [tmp_path / "fetch_status/fetch_20261009.json",
             tmp_path / "factor_status/factors_20261009.json",
             tmp_path / "screening/decision_pool/decision_pool_20261009.json",
             tmp_path / "snapshots/20261009.json"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"trade_date": "20261009", "fetched_at": "2026-10-09T20:00:00"}))
    monkeypatch.setattr(health_module, "postclose_health", lambda *a, **kw: {"ok": True, "reasons": []})
    monkeypatch.setattr(overlay_module.RealtimeOverlayService, "candidate_rows",
                        lambda *a, **kw: [{"code": "002733", "sector": "电池"}])
    monkeypatch.setattr(leader_module.LeaderPoolService, "build_pool",
                        lambda *a, **kw: {"trade_date": "20261009", "rows": []})
    monkeypatch.setattr(HoldingRepository, "list_positions", lambda *a, **kw: [])
    service = SimpleNamespace(resolve_codes_by_names=lambda *a, **kw: {"电池": "885001"},
                              member_codes=lambda code: ["002733", "600000"])
    monkeypatch.setattr(sector_module, "get_realtime_sector_service", lambda: service)
    kwargs = dict(web_data_dir=tmp_path, db_path=database, snapshot_dir=tmp_path / "snapshots",
                  now=datetime(2026, 10, 11, 20))
    result = freeze_preopen_inputs("20261009", "20261012", **kwargs)
    assert result["ok"]
    frozen = PreopenEvidenceStore(tmp_path).load("20261009", "20261012")
    assert frozen["inputs"]["daily_rows"][0]["close"] == 10
    assert frozen["inputs"]["sector_members"] == {"电池": ["002733", "600000"]}
    assert frozen["inputs"]["source_fingerprint"]

    def mutate_candidates(*a, **kw):
        paths[0].write_text(json.dumps({"fetched_at": "2026-10-12T09:42:00"}))
        return []
    monkeypatch.setattr(overlay_module.RealtimeOverlayService, "candidate_rows", mutate_candidates)
    result = freeze_preopen_inputs("20261009", "20261013", **kwargs)
    assert result == {"ok": False, "reason": "inputs_changed_during_capture"}
    assert not PreopenEvidenceStore(tmp_path).path("20261009", "20261013").exists()
