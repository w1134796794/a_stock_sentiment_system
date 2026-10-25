import json
from copy import deepcopy

import pandas as pd
import pytest

from core.portfolio.history_evidence import FrozenHistory, audit_day, pack_inputs, read_packet, write_packet
from core.portfolio.history_workflow import HistoryWorkflows
from risk.risk_config import RiskConfig

DATE = "20261022"


def packet(date=DATE):
    times = [
        f"{h:02}:{m:02}:00"
        for h in (9, 10, 11, 13, 14, 15)
        for m in range(60)
        if "09:31:00" <= f"{h:02}:{m:02}:00" <= "11:30:00" or "13:01:00" <= f"{h:02}:{m:02}:00" <= "15:00:00"
    ]
    frames = {}
    for code in ("000001", "000002", "000003"):
        price = 9.95 if code == "000001" else 10.1
        frames[code] = pd.DataFrame(
            {
                "time": times,
                "open": price,
                "high": price + 0.03,
                "low": price - 0.03,
                "close": price,
                "volume": 10000,
                "amount": price * 10000,
            }
        )
    row = dict(
        code="000001",
        name="test",
        sector_names=["test"],
        strategy_id="weak_to_strong",
        strategy_execution={"allowed_entry_modes": ["weak_to_strong"]},
        allowed_entry_modes=["weak_to_strong"],
    )
    return dict(
        rows=[row],
        frames=frames,
        previous={c: {"close": 10, "amount_yuan": 100_000_000} for c in frames},
        auctions={},
        members={"test": ["000001", "000002", "000003"]},
        sector_codes={"test": "881001"},
        opening_prices={"000001": 9.9, "000002": 10, "000003": 10},
        opening_evidence={},
        opening_price_source="historical",
        board_feedback_inputs={},
        sector_quote_minutes={},
        risk_config=RiskConfig().to_dict(),
        input_basis="research_candidates_not_frozen",
        bundle_id="",
        execution_frozen=True,
        minute_amount_profile={"profiles": {"high": {"09:30:00": {"fraction": 0.2, "samples": 200}}}},
        daily={"000001": {"open": 9.9, "high": 10, "low": 9.8, "close": 9.95, "pre_close": 10, "vol": 100000}},
        suspensions={"000001": {"state": "trading", "trade_date": date}},
        required_rows={"000001": row},
    )


@pytest.fixture
def job(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.csv"
    calendar.write_text(
        "exchange,cal_date,is_open\nSSE,2026-10-21,1\nSSE,2026-10-22,1\nSSE,2026-10-23,1\nSSE,2026-10-24,0\nSSE,2026-10-25,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("core.portfolio.history_workflow.calendar_path", lambda: calendar)
    monkeypatch.setattr(
        "core.realtime.snapshot_replay.load_snapshot_inputs",
        lambda *a, **kw: {"load_failures": [{"evidence": "missing_archive"}]},
    )
    jobs = HistoryWorkflows(tmp_path / "jobs")
    value = jobs.create(DATE, DATE, 100000)
    directory = jobs.directory(value["job_id"])
    value["stages"] = {"prepare": "complete", "prefetch": "complete"}
    (directory / "status.json").write_text(json.dumps(value), encoding="utf-8")
    write_packet(directory / "staging" / f"{DATE}.json.gz", pack_inputs(packet()))
    context = dict(
        profiles={"weak_to_strong": {"id": "weak_to_strong", "enabled": True}},
        risk_config=RiskConfig().to_dict(),
        account=dict(max_positions=3, position_pct=0.34, rotation_min_edge=6),
    )
    (directory / "context.json").write_text(json.dumps(context), encoding="utf-8")
    return jobs, value["job_id"], directory


def test_validation_checks_availability_not_whether_a_buy_is_confirmed():
    data = pack_inputs(packet())
    assert not audit_day(data, DATE, data["required_rows"])
    data["frames"]["000001"].pop(20)
    failures = audit_day(data, DATE, data["required_rows"])
    assert any(row["evidence"] == "minutes" for row in failures)


def test_prefetch_stops_after_losing_the_shared_writer_lease(job, monkeypatch):
    from types import SimpleNamespace

    jobs, ident, directory = job
    released = []
    monkeypatch.setattr(
        "core.infrastructure.shared_state.TaskLease.acquire",
        lambda *a: SimpleNamespace(is_owner=lambda: False, release=lambda: released.append(True)),
    )
    monkeypatch.setattr(jobs, "_collect", lambda directory, value, progress: progress(stage="test"))
    with pytest.raises(ValueError, match="租约已丢失"):
        jobs._prefetch(directory, jobs.get(ident), lambda **step: None)
    assert released == [True]


def test_freeze_missing_data_cannot_proceed_to_run(job):
    jobs, ident, directory = job
    day = read_packet(directory / "staging" / f"{DATE}.json.gz")
    day["frames"]["000001"] = []
    write_packet(directory / "staging" / f"{DATE}.json.gz", day)
    result = jobs.execute(ident, "freeze")
    assert result["state"] == "blocked" and not result["frozen_version"]
    assert any(row["evidence"] == "minutes" for row in result["failures"])
    with pytest.raises(ValueError, match="前一阶段"):
        jobs.execute(ident, "run")


def test_sealed_run_never_constructs_live_or_local_source_readers(job, monkeypatch):
    jobs, ident, directory = job
    assert jobs.execute(ident, "freeze")["state"] == "complete"

    def forbidden(*a, **kw):
        pytest.fail("A frozen run must not read mutable source data or fetch market interfaces")

    monkeypatch.setattr("core.data.DataManager", forbidden)
    monkeypatch.setattr("core.realtime.day_replay.DayReplayLoader.__init__", forbidden)
    monkeypatch.setattr("core.screening.strategy_profiles.StrategyProfileRepository.__init__", forbidden)
    monkeypatch.setattr("backtest.trade_calendar.TradeCalendar.__init__", forbidden)
    monkeypatch.setattr("risk.risk_config.RiskConfig.load", forbidden)
    result = jobs.execute(ident, "run")
    assert result["state"] == "complete", result
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    assert report["completed_dates"] == [DATE]
    assert not report["account_modified"] and not report["notification_sent"]
    assert report["data_quality"]["full_sample_metrics_valid"]
    assert not list(directory.rglob("portfolio.sqlite"))
    assert jobs.execute(ident, "run")["state"] == "complete"


def test_missing_original_archives_are_not_fabricated_or_a_minute_gate(job):
    jobs, ident, directory = job
    assert jobs.execute(ident, "freeze")["state"] == "complete"
    assert jobs.execute(ident, "run")["state"] == "complete"
    result = jobs.execute(ident, "reconcile")
    assert result["state"] == "complete"
    assert {row["status"] for row in result["reconciliation"]} == {"missing"}
    assert not list((directory / "frozen").rglob("snapshots/*.gz"))


def test_archive_reconciliation_uses_sealed_snapshots_not_mutable_sources(job, monkeypatch):
    from test_snapshot_archive_replay import sample

    jobs, ident, directory = job
    archived = sample()
    monkeypatch.setattr("core.realtime.snapshot_replay.load_snapshot_inputs", lambda *a, **kw: deepcopy(archived))
    monkeypatch.setattr(
        "scripts.diagnose_intraday_signal.runtime_evidence",
        lambda *a: {"status": "no_local_runtime_evidence", "rows": []},
    )
    assert jobs.execute(ident, "freeze")["state"] == "complete"
    assert jobs.execute(ident, "run")["state"] == "complete"
    archived["events"] = []

    def forbidden(*a, **kw):
        pytest.fail("Reconciliation must use sealed original evidence only")

    monkeypatch.setattr("core.realtime.snapshot_replay.load_snapshot_inputs", forbidden)
    monkeypatch.setattr("scripts.diagnose_intraday_signal.runtime_evidence", forbidden)
    result = jobs.execute(ident, "reconcile")
    assert result["state"] == "complete", result
    assert len(result["reconciliation"]) == 2
    assert {row["snapshot_confirm_time"] for row in result["reconciliation"]} == {"09:36:03"}
    assert all(row["runtime_diagnostic"] for row in result["reconciliation"])
    report = json.loads((directory / "reconciliation.json").read_text(encoding="utf-8"))
    assert report["frozen_version"] == result["frozen_version"] and not report["notification_sent"]


@pytest.mark.parametrize("target", ["day", "manifest"])
def test_changed_frozen_inputs_are_rejected(job, target):
    jobs, ident, directory = job
    value = jobs.execute(ident, "freeze")
    frozen = directory / "frozen" / value["frozen_version"]
    path = frozen / ("manifest.json" if target == "manifest" else f"days/{DATE}.json.gz")
    if target == "manifest":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["capital"] += 1
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        FrozenHistory(frozen)
    assert jobs.execute(ident, "run")["state"] == "blocked"


def test_code_changes_require_new_freeze(job, monkeypatch):
    jobs, ident, _ = job
    assert jobs.execute(ident, "freeze")["state"] == "complete"
    monkeypatch.setattr("core.portfolio.history_evidence.code_version", lambda: "changed")
    value = jobs.execute(ident, "run")
    assert value["state"] == "blocked" and "代码已变化" in value["error"]


def test_cannot_skip_preparation_or_use_path_traversal(job):
    jobs, _, _ = job
    value = jobs.create(DATE, DATE, 100000)
    with pytest.raises(ValueError, match="前一阶段"):
        jobs.execute(value["job_id"], "freeze")
    with pytest.raises(ValueError):
        jobs.get("../anything")
    for capital in (float("nan"), -1, float("inf")):
        with pytest.raises(ValueError):
            jobs.create(DATE, DATE, capital)


def test_source_changes_after_sealing_do_not_change_run(job):
    jobs, ident, directory = job
    value = jobs.execute(ident, "freeze")
    version = value["frozen_version"]
    before = (directory / "frozen" / version / f"days/{DATE}.json.gz").read_bytes()
    source = read_packet(directory / "staging" / f"{DATE}.json.gz")
    source["frames"] = {}
    write_packet(directory / "staging" / f"{DATE}.json.gz", source)
    assert jobs.execute(ident, "run")["state"] == "complete"
    assert before == (directory / "frozen" / version / f"days/{DATE}.json.gz").read_bytes()


def test_frozen_loader_refuses_network_or_uncovered_holdings(job):
    jobs, ident, directory = job
    value = jobs.execute(ident, "freeze")
    frozen = FrozenHistory(directory / "frozen" / value["frozen_version"])
    with pytest.raises(ValueError, match="只允许"):
        frozen.run({"market_date": DATE, "fetch_missing": True}, inputs_only=True)
    with pytest.raises(ValueError, match="未覆盖持仓"):
        frozen.run({"market_date": DATE, "fetch_missing": False}, inputs_only=True, extra_codes=["999999"])


def test_daily_checkpoint_resumes_without_replaying_committed_days(job, monkeypatch):
    from backtest.backtest_engine import BacktestEngine

    jobs, ident, directory = job
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    status["end"], status["dates"] = "20261023", [DATE, "20261023"]
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    write_packet(directory / "staging/20261023.json.gz", pack_inputs(packet("20261023")))
    assert jobs.execute(ident, "freeze")["state"] == "complete"
    original = BacktestEngine.run_one_day
    calls = []

    def interrupted(self, date, plans):
        calls.append(date)
        if date == "20261023":
            raise RuntimeError("test interruption")
        return original(self, date, plans)

    monkeypatch.setattr(BacktestEngine, "run_one_day", interrupted)
    assert jobs.execute(ident, "run")["state"] == "blocked"
    assert calls == [DATE, "20261023"]
    calls.clear()

    def resumed(self, date, plans):
        calls.append(date)
        return original(self, date, plans)

    monkeypatch.setattr(BacktestEngine, "run_one_day", resumed)
    result = jobs.execute(ident, "run")
    assert result["state"] == "complete", result
    assert calls == ["20261023"]
    assert json.loads((directory / "result.json").read_text(encoding="utf-8"))["completed_dates"] == [DATE, "20261023"]


def test_cancellation_before_worker_start_preserves_preceding_stages(job):
    jobs, ident, directory = job
    status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    status.update(state="queued", phase="freeze")
    (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert jobs.cancel(ident)["cancel_requested"]
    cancelled = jobs.execute(ident, "freeze")
    assert cancelled["state"] == "interrupted"
    assert cancelled["stages"]["prefetch"] == "complete"
    assert not cancelled["frozen_version"]
    assert jobs.execute(ident, "freeze")["state"] == "complete"


def test_child_process_roles_are_scoped_and_steps_cannot_overlap(job, monkeypatch):
    from types import SimpleNamespace

    jobs, ident, _ = job
    calls = []

    def spawn(command, **options):
        calls.append((command, options))
        return SimpleNamespace(pid=1234, poll=lambda: None)

    monkeypatch.setattr("core.portfolio.history_workflow.subprocess.Popen", spawn)
    queued = jobs.start(ident, "freeze")
    assert queued["state"] == "queued"
    assert calls[0][1]["env"]["MARKET_DATA_NODE_ROLE"] == "server"
    assert calls[0][0][-2:] == [ident, "freeze"]
    with pytest.raises(ValueError, match="运行"):
        jobs.start(ident, "freeze")
