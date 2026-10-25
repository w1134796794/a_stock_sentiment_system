from datetime import datetime
from types import SimpleNamespace

import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.operations.collection_center import collection_status, date_key, start_preopen


@pytest.mark.parametrize("value", ["", "../20261023", "20260230", "202610231"])
def test_dates_are_validated(value):
    with pytest.raises(ValueError):
        date_key(value)


def test_intraday_status_only_reads_storage_and_marks_stale(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Status reads must never create a market provider")
    monkeypatch.setattr("core.data.data_manager_main.DataManager.__init__", forbidden)
    monkeypatch.setattr("core.data.providers.eltdx_provider.EltdxProvider.__init__", forbidden)
    backend = MemoryStateBackend("test")
    backend.set_json("realtime:collector:health", {"ok": True, "updated_at": "2026-10-23T09:30:00+08:00"})
    status = collection_status("intraday", "20261023", root=tmp_path, backend=backend,
                               now=datetime.fromisoformat("2026-10-23T10:00:00+08:00"))
    assert status["collector"]["fresh"] is False
    assert status["collector"]["age_seconds"] == 1800
    assert status["strategy_executed"] is False
    assert status["notification_sent"] is False
    assert status["missing"]


def test_current_heartbeat_is_not_proof_for_historical_day(tmp_path):
    backend = MemoryStateBackend("historical")
    backend.set_json("realtime:collector:health", {"ok": True, "updated_at": "2026-10-23T10:00:00+08:00"})
    result = collection_status("intraday", "20261022", root=tmp_path, backend=backend,
                               now=datetime.fromisoformat("2026-10-23T10:00:01+08:00"))
    assert result["collector"]["fresh"]
    assert not result["live_date_matches"]


def test_preopen_past_date_never_launches_a_process(monkeypatch):
    monkeypatch.setattr("core.operations.collection_center.subprocess.Popen",
        lambda *a, **kw: pytest.fail("Historical preopen must not fetch"))
    with pytest.raises(ValueError):
        start_preopen("20261023")


def test_strategy_waits_for_collection_without_provider_or_repairs(monkeypatch):
    from scripts.postclose_strategy import run_strategy
    monkeypatch.setattr("core.etl.stage_status.fetch_status", lambda *a, **kw: {"ready": False, "missing": ["daily"]})
    monkeypatch.setattr("core.data.data_manager_main.DataManager.__init__",
        lambda *a, **kw: pytest.fail("Strategy must not create providers"))
    with pytest.raises(RuntimeError, match="策略禁止补取行情"):
        run_strategy("20261023")


def test_ready_strategy_has_no_data_provider(monkeypatch):
    from scripts.postclose_strategy import run_strategy
    monkeypatch.setattr("core.etl.stage_status.fetch_status", lambda *a, **kw: {"ready": True})
    calls = []
    class Pipeline:
        def __init__(self, dm):
            assert dm is None
        def compute_factors(self, date):
            calls.append("factors")
            return SimpleNamespace(ok=True)
        def run_screening(self, date, *, archive_cases):
            assert archive_cases is False
            calls.append("screening")
            return SimpleNamespace(ok=True)
    monkeypatch.setattr("core.etl.daily_pipeline.ETLDailyPipeline", Pipeline)
    assert run_strategy("20261023") == {"factors": True, "screening": True}
    assert calls == ["factors", "screening"]


def test_strategy_schedule_waits_without_dispatching_collection(monkeypatch):
    from core.automation.internal_scheduler import InternalScheduler
    monkeypatch.setenv("AUTOMATION_DAILY_CATCH_UP", "false")
    scheduler = InternalScheduler()
    monkeypatch.setattr(scheduler, "_now", lambda: datetime.fromisoformat("2026-10-23T20:16:00+08:00"))
    monkeypatch.setattr("core.etl.stage_status.fetch_status", lambda *a, **kw: {"ready": False})
    monkeypatch.setattr(scheduler, "_dispatch_job", lambda *a, **kw: pytest.fail("Missing data cannot dispatch any work"))
    assert scheduler._recover_due_daily_job("strategy") is False
    assert scheduler.strategy_state.load()["status"] == "waiting_data"


def test_postclose_worker_only_calls_fetch(monkeypatch, tmp_path):
    from scripts.collect_postclose import collect
    monkeypatch.setattr("core.data.data_manager_main.DataManager", lambda *a: object())
    calls = []
    class Pipeline:
        def __init__(self, dm):
            pass
        def fetch_data(self, date, previous, **options):
            calls.append((date, previous, options))
            return SimpleNamespace(ok=True)
        def __getattr__(self, name):
            pytest.fail("Collector attempted strategy work: " + name)
    monkeypatch.setattr("core.etl.daily_pipeline.ETLDailyPipeline", Pipeline)
    assert collect("20261023").ok
    assert calls[0][0] == "20261023"


@pytest.mark.parametrize("phase,expected", [("postclose", {"fetch": True}), ("strategy", {"factors": True, "screening": True})])
def test_worker_phase_does_not_chain_or_notify(tmp_path, monkeypatch, phase, expected):
    import json
    import sys

    from scripts import automation_daily_worker
    path = tmp_path / "result.json"
    calls = []
    monkeypatch.setattr(sys, "argv", ["worker", "--date", "20261023", "--result", str(path), "--phase", phase])
    monkeypatch.setattr("core.infrastructure.shared_state.TaskLease.acquire", lambda *a: SimpleNamespace(release=lambda: None))
    monkeypatch.setattr("scripts.collect_postclose.collect", lambda *a, **kw: calls.append("postclose") or SimpleNamespace(ok=True))
    monkeypatch.setattr("scripts.postclose_strategy.run_strategy", lambda *a: calls.append("strategy") or expected)
    assert automation_daily_worker.main() == 0
    result = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [phase]
    assert result["stages"] == expected
    assert result["notification_sent"] is False
    assert result["account_modified"] is False
