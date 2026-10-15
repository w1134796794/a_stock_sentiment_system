from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.infrastructure.duckdb_access import connect
from core.operations.health import collector_health, realtime_readiness
from core.realtime.models import QuoteSnapshot
from core.realtime.quote_service import RealtimeQuoteService

ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def clock(monkeypatch):
    import core.realtime.models as models
    values = [datetime(2026, 10, 13, 9, 46, tzinfo=ZONE)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return values[0].astimezone(tz) if tz else values[0].replace(tzinfo=None)
    monkeypatch.setattr(models, "datetime", Clock)
    return values


def quote(at):
    return {"code": "000001", "last_price": 10, "pre_close": 9,
            "date": at.strftime("%Y%m%d"), "time": at.strftime("%H:%M:%S"),
            "received_at": at.isoformat()}


def test_cache_refetches_old_quote_even_before_cache_ttl_expires(clock):
    calls = []
    def batch(codes):
        calls.append(codes)
        at = clock[0] - timedelta(seconds=13) if len(calls) == 1 else clock[0]
        return {"000001": quote(at)}
    service = RealtimeQuoteService(SimpleNamespace(get_quote_snapshots=batch), ttl_seconds=15,
                                   stale_after_seconds=12)
    first = service.get_quote("000001")
    second = service.get_quote("000001")
    assert len(calls) == 2
    assert first["is_stale"] and first["stale_seconds"] == 13
    assert not second["is_stale"] and second["stale_seconds"] == 0
    assert second["received_at"] == clock[0].isoformat()


def test_cache_age_evolves_and_refetch_cannot_retimestamp_old_source(clock):
    original = quote(clock[0])
    calls = []
    def batch(codes):
        calls.append(codes)
        return {"000001": original}
    service = RealtimeQuoteService(SimpleNamespace(get_quote_snapshots=batch), ttl_seconds=30,
                                   stale_after_seconds=12)
    assert service.get_quote("000001")["stale_seconds"] == 0
    clock[0] += timedelta(seconds=2)
    assert service.get_quote("000001")["stale_seconds"] == 2
    assert len(calls) == 1
    clock[0] += timedelta(seconds=11)
    result = service.get_quote("000001")
    assert len(calls) == 2 and result["is_stale"]
    assert result["received_at"] == original["received_at"]
    assert result["stale_seconds"] == 13


def test_quote_unknown_age_does_not_invent_receive_time(clock):
    result = QuoteSnapshot.from_raw({"code": "000001", "last_price": 10}).to_dict()
    assert result["received_at"] == ""
    assert result["is_stale"] and result["stale_seconds"] is None


def test_entry_reloads_quote_after_slow_minute_context_loading(clock):
    import pandas as pd
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    service = RealtimeEntrySignalService(data_manager=SimpleNamespace(),
                                        quote_service=SimpleNamespace(get_quote=lambda code: quote(clock[0])))
    original = {"000001": quote(clock[0])}
    old_stamp = original["000001"]["received_at"]
    def slow_minutes(*a):
        clock[0] += timedelta(seconds=14)
        return pd.DataFrame()
    service._minute_frame = slow_minutes
    service._previous_daily_map = lambda *a: {}
    service._evaluate_one = lambda row, current, *a: {"received_at": current["received_at"]}
    result = service.evaluate([{"code": "000001"}], original, market_date="20261013")
    assert result["000001"]["received_at"] != old_stamp
    assert original["000001"]["received_at"] == clock[0].isoformat()


def test_source_age_and_timezone_are_checked_independently_of_receive_time(clock):
    old = quote(clock[0] - timedelta(seconds=20))
    old["received_at"] = clock[0].astimezone(ZoneInfo("UTC")).isoformat()
    result = QuoteSnapshot.from_raw(old, stale_after_seconds=12)
    assert result.is_stale and result.stale_seconds == 20
    fresh = quote(clock[0])
    fresh["received_at"] = clock[0].astimezone(ZoneInfo("UTC")).isoformat()
    assert not QuoteSnapshot.from_raw(fresh, stale_after_seconds=12).is_stale
    fresh["received_at"] = (clock[0] + timedelta(seconds=1)).isoformat()
    assert QuoteSnapshot.from_raw(fresh, stale_after_seconds=12).is_stale


def test_web_cache_cannot_outlive_live_quote_gate(monkeypatch):
    import web.app as app
    import config.settings as settings
    service = SimpleNamespace(ttl_seconds=15, stale_after_seconds=90)
    monkeypatch.setattr(app, "_REALTIME_QUOTE_SERVICE", service)
    monkeypatch.setattr(settings, "ELTDX_POLL_INTERVAL_SECONDS", 3)
    monkeypatch.setattr(settings, "REALTIME_QUOTE_STALE_SECONDS", 12)
    actual = app._get_realtime_quote_service(90)
    assert actual.ttl_seconds == 3
    assert actual.stale_after_seconds == 12


def test_old_collector_is_explicitly_incompatible_not_grandfathered():
    now = datetime(2026, 10, 13, 9, 46, tzinfo=ZONE)
    collector = {"storage": "redis", "trade_date": "20261013", "collector": {
        "ok": True, "updated_at": now.isoformat(),
    }}
    result = collector_health(collector, "20261013", now=now, require_shared=True)
    assert not result["ok"]
    assert result["protocol"]["missing_fields"] == ["lease_owned", "session_started_at"]
    assert "升级" in result["protocol"]["action"]
    assert not realtime_readiness({"ok": True}, collector, "20261013", now=now, require_shared=True)["ready"]


def test_lease_without_continuous_session_proof_cannot_authorize_recovered_minutes():
    now = datetime(2026, 10, 13, 9, 46, tzinfo=ZONE)
    collector = {"storage": "redis", "trade_date": "20261013", "collector": {
        "ok": True, "lease_owned": True, "updated_at": now.isoformat(),
    }}
    assert not collector_health(collector, "20261013", now=now)["ok"]
    collector["collector"]["session_started_at"] = "2026-10-12T09:15:00+08:00"
    assert not collector_health(collector, "20261013", now=now)["ok"]
    collector["collector"]["session_started_at"] = "2026-10-13T09:15:00+08:00"
    assert collector_health(collector, "20261013", now=now)["ok"]


def test_utc_host_clock_produces_shanghai_collector_dates(monkeypatch):
    import core.realtime.trading_session as session
    from core.realtime.eltdx_collector import EltdxQuoteCollector
    utc = datetime(2026, 10, 13, 1, 46, tzinfo=ZoneInfo("UTC"))
    class UtcHost(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)
    monkeypatch.setattr(session, "datetime", UtcHost)
    collector = EltdxQuoteCollector()
    collector.calendar.is_trade_date = lambda date: date == "20261013"
    assert collector.clock().hour == 9
    assert collector._session_started_at.endswith("+08:00")
    assert collector._is_collection_session()


def test_snapshot_clock_and_naive_receive_time_use_same_market_timezone():
    import pandas as pd
    from backtest.minute_entry import ENTRY_WEAK, EntryDecision
    from core.realtime.snapshot_signal_service import SnapshotSignalService
    clock = [datetime(2026, 10, 13, 1, 43, 3, tzinfo=ZoneInfo("UTC"))]
    service = SnapshotSignalService(clock=lambda: clock[0])
    minutes = pd.DataFrame([{"time": f"09:{30+i:02d}:00", "open": 9.9, "high": 10,
                             "low": 9.8, "close": 9.9, "volume": 100} for i in range(5)])
    rows = [{"date": "20261013", "time": f"09:43:{i*3:02d}", "received_at": f"2026-10-13T09:43:{i*3:02d}",
             "last_price": 10.05, "ask1": 10.06, "delta_volume": 10, "quality_ok": True}
            for i in range(3)]
    common = dict(code="000001", trade_date="20261013", mode=ENTRY_WEAK, minute_bars=minutes,
                  prev_close=10, open_gap=-.01, sector_confirmed=True, is_leader=False,
                  limit_price=11, minute_decision=EntryDecision("confirmed"))
    assert service.evaluate(snapshots=pd.DataFrame(rows[:2]), **common).data_status == "snapshot_triggered"
    clock[0] += timedelta(seconds=3)
    assert service.evaluate(snapshots=pd.DataFrame(rows), **common).status == "filled"


def test_atomic_status_failure_preserves_previous_document(tmp_path):
    from core.infrastructure.atomic_json import atomic_write_json
    path = tmp_path / "proof.json"
    atomic_write_json(path, {"ok": True})
    with pytest.raises(TypeError):
        atomic_write_json(path, {"value": object()})
    assert json.loads(path.read_text()) == {"ok": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_fetch_failure_invalidates_previous_success_before_data_mutation(tmp_path, monkeypatch):
    import pandas as pd
    import core.etl.stage_status as status
    from core.etl.daily_pipeline import ETLDailyPipeline
    path = tmp_path / "fetch_status/fetch_20261013.json"
    status.write_fetch_manifest("20261013", web_data_dir=tmp_path, sources={"old": {"ok": True}},
                                writes={"old": {"duckdb": True}})
    monkeypatch.setattr(status, "post_close_data_ready", lambda date: True)
    monkeypatch.setattr(status, "fetch_status", lambda *a, **kw: {"complete": True})
    pipeline = ETLDailyPipeline(SimpleNamespace(get_limit_up_pool=lambda date: pd.DataFrame()),
                                duckdb_path=tmp_path / "factors.duckdb", web_data_dir=tmp_path)
    def fail_build(*a, **kw):
        record = json.loads(path.read_text())
        assert record["metadata"]["state"] == "running"
        assert not record["sources"] and not record["writes"]
        raise RuntimeError("fetch failed")
    pipeline.data_prep = SimpleNamespace(build=fail_build)
    with pytest.raises(RuntimeError, match="fetch failed"):
        pipeline.fetch_data("20261013", skip_existing=False)
    assert json.loads(path.read_text())["metadata"]["state"] == "running"


def test_no_confirmation_still_exposes_preopen_blocker(monkeypatch):
    import web.app as app
    import core.realtime.quote_cache as caches
    import core.operations.evidence as evidence
    monkeypatch.setattr(app, "_postclose_gate_for_date", lambda *a: {"ok": False, "reasons": ["缺少开盘前冻结证据"]})
    monkeypatch.setattr(caches, "RealtimeQuoteCache", lambda: SimpleNamespace(health=lambda: {}))
    monkeypatch.setattr(app, "_apply_realtime_confirmation_history", lambda value: value)
    monkeypatch.setattr(evidence, "attach_evidence", lambda value: value)
    value = {"candidate_date": "20261012", "market_date": "20261013",
             "rows": [{"code": "000001", "confirm_status": "observe"}]}
    result = app._prepare_signal_payload(value)
    assert not result["execution_health"]["ready"]
    assert "缺少开盘前冻结证据" in result["execution_health"]["reasons"]
    assert result["rows"][0]["confirm_status"] == "observe"


def test_health_api_response_success_is_not_execution_readiness(monkeypatch):
    import web.app as app
    import core.realtime.quote_cache as caches
    monkeypatch.setattr(app, "_realtime_market_date", lambda: "20261013")
    monkeypatch.setattr(app, "_realtime_candidate_date", lambda *a: "20261012")
    monkeypatch.setattr(app, "_postclose_gate_for_date", lambda *a: {"ok": False, "reasons": ["缺少开盘前冻结证据"]})
    monkeypatch.setattr(app, "_current_realtime_session", lambda: {"is_open": True})
    service = SimpleNamespace(health=lambda **kw: {"available": True})
    monkeypatch.setattr(app, "_get_realtime_quote_service", lambda **kw: service)
    monkeypatch.setattr(app, "_get_realtime_sector_service", lambda: service)
    monkeypatch.setattr(caches, "RealtimeQuoteCache", lambda: SimpleNamespace(health=lambda: {}))
    result = app._build_realtime_health_payload()
    assert result["ok"]
    assert not result["execution_ready"]
    assert "缺少开盘前冻结证据" in result["execution_health"]["reasons"]


@pytest.fixture
def runner_fixture(tmp_path, monkeypatch):
    import core.factors.jobs.runner as module
    from core.factors.jobs.gold_utils import FactorJobResult
    names = ["market", "lhb", "signals", "sector", "stock"]
    def make_job(key):
        class Job:
            name = key
            def run(self, con, trade_date):
                manifest = json.loads((tmp_path / "factor_status" / f"factors_{trade_date}.json").read_text())
                assert manifest["state"] == "running" and not manifest["complete"]
                return FactorJobResult(name=key, trade_date=trade_date, ok=True)
        return Job
    monkeypatch.setattr(module.FactorJobRunner, "JOBS", {name: make_job(name) for name in names})
    monkeypatch.setattr(module, "connect_duckdb", lambda path: connect(":memory:"))
    monkeypatch.setattr(module, "release_process_memory", lambda: None)
    return module, module.FactorJobRunner(tmp_path / "factors.duckdb"), tmp_path


def test_direct_factor_entry_publishes_full_success_proof(runner_fixture):
    module, runner, root = runner_fixture
    result = runner.run("20261013")
    assert len(result) == 5
    manifest = json.loads((root / "factor_status/factors_20261013.json").read_text())
    assert manifest["state"] == "done" and manifest["complete"]
    assert module.FactorJobRunner.successful_job_keys(manifest["jobs"]) == set(module.FactorJobRunner.JOBS)
    assert manifest["generated_at"]
    assert datetime.fromisoformat(manifest["generated_at"]).utcoffset() == timedelta(hours=8)


def test_partial_or_failed_factor_rerun_cannot_keep_old_full_success(runner_fixture):
    module, runner, root = runner_fixture
    runner.run("20261013")
    runner.run("20261013", jobs=["stock"])
    manifest = json.loads((root / "factor_status/factors_20261013.json").read_text())
    assert not manifest["complete"] and manifest["selected_jobs"] == ["stock"]
    def failing(*a):
        raise RuntimeError("failed factor")
    module.FactorJobRunner.JOBS["stock"].run = failing
    results = runner.run("20261013")
    assert not results[-1].ok
    manifest = json.loads((root / "factor_status/factors_20261013.json").read_text())
    assert not manifest["complete"]


def test_aborted_factor_run_leaves_running_not_previous_done(runner_fixture):
    module, runner, root = runner_fixture
    runner.run("20261013")
    class Broken:
        name = "stock"
        def __init__(self):
            raise RuntimeError("worker aborted")
    module.FactorJobRunner.JOBS["stock"] = Broken
    with pytest.raises(RuntimeError, match="worker aborted"):
        runner.run("20261013")
    manifest = json.loads((root / "factor_status/factors_20261013.json").read_text())
    assert manifest["state"] == "running" and not manifest["complete"]


def test_cli_defaults_to_all_required_factor_jobs(monkeypatch):
    from scripts.run_factor_jobs import _parse_args
    from core.factors.jobs.runner import FactorJobRunner
    monkeypatch.setattr("sys.argv", ["run_factor_jobs.py"])
    assert _parse_args().jobs.split(",") == list(FactorJobRunner.JOBS)
