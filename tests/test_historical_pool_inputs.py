import json
from copy import deepcopy

import duckdb
import pytest

from core.portfolio.historical_pools import read_historical_pools
from core.portfolio.history_workflow import HistoryWorkflows


@pytest.fixture
def archived(tmp_path, monkeypatch):
    root = tmp_path / "webdata"
    decision = root / "screening/decision_pool/decision_pool_20261021.json"
    decision.parent.mkdir(parents=True)
    decision.write_text(
        json.dumps(
            {
                "trade_date": "20261021",
                "rows": [
                    {"code": "000002", "strategy_id": "disabled_today", "allowed_entry_modes": ["weak_to_strong"]},
                    {"code": "000003", "execution_eligible": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    database = root / "factors.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE leader_signal_history(signal_date VARCHAR, code VARCHAR, primary_sector VARCHAR)")
        con.execute(
            "INSERT INTO leader_signal_history VALUES ('20261021','000001','test'),('20261021','000002','test'),('20261022','000004','future')"
        )
    monkeypatch.setattr("config.settings.WEB_DATA_DIR", root)
    monkeypatch.setattr("config.settings.FACTOR_DB_PATH", database)
    return root, database, decision


def test_read_exact_postclose_pools_without_factors_profiles_or_reselection(archived, monkeypatch):
    root, database, decision = archived
    before = database.read_bytes(), decision.read_bytes()

    def forbidden(*a, **kw):
        pytest.fail("Reading historical pool membership must not run ETL, reselect stocks or call market APIs")

    monkeypatch.setattr("main.SentimentSystem", forbidden)
    monkeypatch.setattr("core.realtime.leader_pool_service.LeaderPoolService.build_pool", forbidden)
    monkeypatch.setattr("core.screening.strategy_profiles.StrategyProfileRepository.get_profile", forbidden)
    result = read_historical_pools(root, database, "20261021", "20261022")
    assert result["counts"] == {"leader_pool": 2, "decision_pool": 2}
    assert {r["code"] for r in result["rows"]} == {"000001", "000002", "000003"}
    shared = next(r for r in result["rows"] if r["code"] == "000002")
    assert shared["pool_sources"] == ["decision_pool", "leader_pool"]
    assert shared["strategy_id"] == "disabled_today"
    assert (database.read_bytes(), decision.read_bytes()) == before


@pytest.mark.parametrize("fault", ["missing_decision", "wrong_date", "missing_leader"])
def test_missing_pool_artifact_is_explicit_not_regenerated(archived, fault):
    root, database, decision = archived
    if fault == "missing_decision":
        decision.unlink()
    elif fault == "wrong_date":
        decision.write_text(json.dumps({"trade_date": "20261022", "rows": []}), encoding="utf-8")
    else:
        with duckdb.connect(str(database)) as con:
            con.execute("DELETE FROM leader_signal_history WHERE signal_date='20261021'")
    with pytest.raises(ValueError):
        read_historical_pools(root, database, "20261021", "20261022")


def test_frozen_membership_is_read_without_database_or_live_profiles(tmp_path, monkeypatch):
    saved = {
        "bundle_id": "saved",
        "inputs": {
            "leader_pools": {"10": {"rows": [{"code": "000001"}]}},
            "candidates_by_profile": {"decision_pool": []},
        },
    }
    monkeypatch.setattr("core.operations.preopen_evidence.PreopenEvidenceStore.load", lambda *a: deepcopy(saved))
    result = read_historical_pools(tmp_path, tmp_path / "missing.duckdb", "20261021", "20261022")
    assert result["basis"] == "preopen_frozen" and len(result["rows"]) == 1
    assert saved["inputs"]["leader_pools"]["10"]["rows"] == [{"code": "000001"}]


def test_read_phase_only_uses_requested_dates_and_preserves_source_files(archived, monkeypatch, tmp_path):
    from core.portfolio.history_workflow import atomic_write_json

    root, database, decision = archived

    def forbidden(*a, **kw):
        pytest.fail("No data source or model training is allowed in the read-list phase")

    monkeypatch.setattr("main.SentimentSystem", forbidden)
    monkeypatch.setattr("core.data.DataManager", forbidden)
    calendar = tmp_path / "calendar.csv"
    calendar.write_text("exchange,cal_date,is_open\nSSE,20261021,1\nSSE,20261022,1\nSSE,20261025,0\n", encoding="utf-8")
    monkeypatch.setattr("core.portfolio.history_workflow.calendar_path", lambda: calendar)
    jobs = HistoryWorkflows(tmp_path / "jobs")
    job = jobs.create("20261022", "20261022", 100000)
    before = database.read_bytes(), decision.read_bytes()
    result = jobs.execute(job["job_id"], "prepare")
    assert result["state"] == "complete", result
    assert len(result["pool_scope"]) == 1
    assert result["pool_scope"][0]["candidate_date"] == "20261021"
    assert jobs._pool_inputs(jobs.directory(job["job_id"]), "20261022")["counts"]["leader_pool"] == 2
    assert (database.read_bytes(), decision.read_bytes()) == before


def test_supplied_pool_rows_bypass_current_production_filter(monkeypatch, tmp_path):
    from core.operations.replay_research import FrozenCalendar
    from core.realtime.day_replay import DayReplayLoader

    def forbidden(*a, **kw):
        pytest.fail("Stored pool rows must not be reselected")

    monkeypatch.setattr("core.signals.production_rules.production_candidates", forbidden)
    monkeypatch.setattr("core.realtime.leader_pool_service.LeaderPoolService.build_pool", forbidden)
    monkeypatch.setattr(
        "core.realtime.sector_evidence.prepare_sector_cache",
        lambda *a, **kw: {"sector_members": {}, "sector_codes": {}},
    )
    loader = DayReplayLoader(
        web_data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "none.duckdb",
        calendar=FrozenCalendar(["20261021", "20261022"]),
    )
    supplied = {
        "candidate_date": "20261021",
        "market_date": "20261022",
        "inputs": {},
        "bundle_id": "",
        "rows": [{"code": "000001", "strategy_id": "disabled_today"}],
    }
    packet = loader.run(
        {
            "market_date": "20261022",
            "until": "15:01:00",
            "profile": "production",
            "fetch_missing": False,
            "codes": [],
            "lookback": 10,
        },
        inputs_only=True,
        pool_inputs=supplied,
    )
    assert packet["rows"] == supplied["rows"]


@pytest.mark.parametrize("suspension_date", [None, "20261021", "20261022"])
def test_pool_minutes_are_fetched_before_constituents_and_opening_requests(monkeypatch, tmp_path, suspension_date):
    from types import SimpleNamespace

    import pandas as pd

    from core.operations.replay_research import FrozenCalendar
    from core.realtime.day_replay import DayReplayLoader

    calls, steps = [], []

    def minutes(code, date):
        calls.append(code.split(".")[0])
        if code.startswith("000001"):
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "time": f"09:{31 + i:02d}:00",
                    "trade_date": date,
                    "code": code,
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "volume": 100,
                    "amount": 100000,
                    "source": "axdata",
                }
                for i in range(20)
            ]
        )

    monkeypatch.setattr(
        "core.realtime.sector_evidence.prepare_sector_cache",
        lambda *a, **kw: {"sector_members": {"板块": ["000001", "600001"]}, "sector_codes": {"板块": "881001"}},
    )
    monkeypatch.setattr("core.realtime.entry_signal_service.RealtimeEntrySignalService._sectors", lambda row: {"板块"})
    dm = SimpleNamespace(
        get_stock_tick=minutes,
        get_auction_data=lambda *a: {},
        get_suspension_status=lambda code, date, **kw: {
            "state": "full_day" if code == "000001" and suspension_date else "unknown",
            "trade_date": suspension_date,
            "source": "tushare.suspend_d",
        },
        get_minute_quality_report=lambda *a: {
            "reason": "axdata: 请求日期无数据",
            "attempts": [{"source": "axdata", "reason": "请求日期无数据"}],
        },
    )
    loader = DayReplayLoader(
        web_data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "missing",
        data_manager=dm,
        calendar=FrozenCalendar(["20261021", "20261022"]),
        minute_cache=SimpleNamespace(read=lambda *a: pd.DataFrame()),
    )

    def openings(*a, **kw):
        calls.append("openings")
        if suspension_date == "20261022":
            assert "000001" not in a[1]
        return {}, "missing"

    monkeypatch.setattr(loader, "_opening_evidence", openings)
    result = loader.run(
        {"market_date": "20261022", "until": "09:51:00", "profile": "production", "fetch_missing": True, "codes": []},
        inputs_only=True,
        progress=steps.append,
        rule_risk={"entry_sector_mode": "breadth_required"},
        pool_inputs={
            "candidate_date": "20261021",
            "market_date": "20261022",
            "inputs": {},
            "bundle_id": "",
            "rows": [{"code": "600001"}],
        },
    )
    suspended = suspension_date == "20261022"
    assert calls == (["600001", "openings"] if suspended else ["600001", "000001", "openings"])
    assert steps[0]["stage"] == "补取池内分钟" and steps[0]["code"] == "600001"
    last = [step for step in steps if "last_result" in step][-1]
    assert last["success"] == 1 and last["failed"] == int(not suspended)
    assert last["skipped"] == int(suspended)
    assert last["pool_success"] == last["pool_total"] == 1
    if not suspended:
        assert last["last_result"]["reason"] == "axdata: 请求日期无数据"
        assert result["minute_acquisition"]["failures"][0]["code"] == "000001"
    assert "000001" not in result["frames"]  # Failure does not shrink the sector denominator.
    assert result["members"]["板块"] == ["000001", "600001"]


def test_frozen_calendar_supports_feedback_history_without_external_calendar(tmp_path):
    from core.operations.replay_research import FrozenCalendar
    from core.realtime.board_feedback_repository import BoardFeedbackRepository
    from core.signals.board_feedback import history_dates

    prior = ["20261014", "20261015", "20261016", "20261019", "20261020", "20261021"]
    calendar = FrozenCalendar([*prior, "20261022"])
    assert calendar.is_trade_date("20261022") and not calendar.is_trade_date("20261025")
    assert history_dates("20261022", calendar) == prior
    captured = BoardFeedbackRepository(tmp_path, db_path=tmp_path / "missing", calendar=calendar).capture(
        "20261022", ["000001"]
    )
    assert captured["calendar_verified"] and captured["dates"] == prior
