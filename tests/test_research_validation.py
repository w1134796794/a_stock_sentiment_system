import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.validate_research_baseline import (
    PRODUCTION_STRATEGY_IDS,
    factor_status_ready,
    prefetch_holding_evidence,
    progress,
    reconcile_prices,
    recover_holding_minutes,
    repair,
    requirements,
    validate_repair,
)


def test_requirements_use_previous_plan_and_research_database(tmp_path, monkeypatch):
    class Calendar:
        def prev(self, date):
            return "20260907"
    pd.DataFrame([{"代码": "600479", "共振板块": "医药"}]).to_csv(tmp_path / "交易计划_20260907.csv", index=False)
    calls = []
    def peers(date, sectors, *, duckdb_path):
        calls.append((date, sectors, duckdb_path))
        return {"600479": ["000001", "600479"]}
    monkeypatch.setattr("scripts.validate_research_baseline.BacktestEngine._load_sector_peer_codes", peers)
    db = tmp_path / "factors.duckdb"
    assert requirements(tmp_path, ["20260908"], Calendar(), db) == [("20260908", "000001"), ("20260908", "600479")]
    assert calls == [("20260908", {"600479": {"医药"}}, db)]


def test_zero_candidate_proof_does_not_create_minute_requests(tmp_path):
    class Calendar:
        def prev(self, date):
            return "20260907"
    assert requirements(tmp_path, ["20260908"], Calendar(), tmp_path / "db") == []


def test_batch_memberships_match_single_stock_filter_and_scan_once(tmp_path, monkeypatch):
    from pathlib import Path

    from core.factors.jobs.first_board_factors import load_membership_map, load_stock_memberships
    directory = tmp_path / "sector/stock_sectors"
    directory.mkdir(parents=True)
    frame = pd.DataFrame([{"ts_code": "885001.TI", "name": "机器人", "type": "N"},
                          {"ts_code": "885001.TI", "name": "机器人", "type": "N"},
                          {"ts_code": "000001.SH", "name": "上证指数", "type": "X"}])
    frame.to_csv(directory / "000001.SZ.csv", index=False)
    expected = load_stock_memberships("000001.SZ", tmp_path)
    (directory / "000002.SZ.csv").write_text('"broken', encoding="utf-8")
    scans = []
    original = Path.glob
    def glob(path, pattern):
        scans.append(pattern)
        return original(path, pattern)
    monkeypatch.setattr(Path, "glob", glob)
    assert load_membership_map(["000001.SZ", "000002", "000003"], tmp_path) == {
        "000001": expected, "000002": [], "000003": []}
    assert scans == ["*.csv"]


def test_recover_only_real_held_exit_minutes(monkeypatch):
    calls = []
    class Data:
        def get_stock_tick(self, code, date, **kwargs):
            calls.append((code, date, kwargs))
            return pd.DataFrame({"close": [10]})
    monkeypatch.setattr("scripts.validate_research_baseline.inspect_minute_bars", lambda *args, **kwargs: {"valid": True})
    row = {"scope": "holding", "evidence": "exit_minute_bars", "trade_date": "20260909", "code": "002855"}
    report = {"results": {"a": {"blocked": [row]}, "b": {"blocked": [row, dict(row, scope="candidate"),
                                                                                dict(row, evidence="daily")]}}}
    assert recover_holding_minutes(report, Data()) == [{"date": "20260909", "code": "002855"}]
    assert calls == [("002855.SZ", "20260909", {"require_full_day": True})]


@pytest.mark.parametrize("scope,valid,expected", [("holding", True, 2), ("holding", False, 0),
                                                ("candidate", True, 0)])
def test_preparation_is_readonly_deduplicated_and_restores_failed_day(tmp_path, monkeypatch,
                                                                    scope, valid, expected):
    from backtest.evidence_policy import ReplayEvidenceBlocked

    file = tmp_path / "evidence.sqlite"
    with sqlite3.connect(file) as connection:
        connection.execute("CREATE TABLE evidence(kind TEXT,code TEXT,date TEXT,payload TEXT)")
    original = file.read_bytes()
    engines, calls = [], []

    class Engine:
        def __init__(self, evidence, config, manifest, *, arm):
            self.dm, self.cash, self._exit_minute_frames = evidence, 100, {}
            engines.append(self)

        def export_state(self):
            return {"cash": self.cash}

        def import_state(self, state):
            self.cash = state["cash"]

        def run_one_day(self, date, plans):
            self.cash -= 1
            if self.dm.get_stock_tick("002855.SZ", date).empty:
                raise ReplayEvidenceBlocked([{"scope": scope, "evidence": "exit_minute_bars",
                    "trade_date": date, "code": "002855"}], {})

    class Data:
        def get_stock_tick(self, code, date, **kwargs):
            calls.append((code, date, kwargs))
            return pd.DataFrame({"close": [10]}) if valid else pd.DataFrame()

    monkeypatch.setattr("scripts.validate_research_baseline.ResearchEngine", Engine)
    monkeypatch.setattr("scripts.validate_research_baseline.ARMS", {"a": {}, "b": {}})
    monkeypatch.setattr("scripts.validate_research_baseline.inspect_minute_bars",
                        lambda frame, *args, **kwargs: {"valid": not frame.empty})
    result = prefetch_holding_evidence({"base_config": {}, "dates": ["20260909", "20260910"]},
                                      tmp_path, Data())
    assert result["classification"] == "preparation_only_not_performance"
    assert len(result["recovered"]) == expected
    assert len(calls) == (2 if expected else 1 if scope == "holding" else 0)
    assert all(engine.cash == (98 if expected else 100) for engine in engines)
    assert bool(result["blocked"]) is (not bool(expected))
    assert file.read_bytes() == original


def test_incomplete_repair_cannot_be_published(tmp_path):
    with pytest.raises(RuntimeError, match="20260907"):
        validate_repair(tmp_path, ["20260907"])


@pytest.mark.parametrize("corruption", [None, "contract", "state"])
def test_preparation_resumes_verified_checkpoints_without_writing_them(tmp_path, monkeypatch, corruption):
    from core.operations.replay_research import atomic_json, digest

    state = {"cash": 98, "daily_nav": [{"date": "20260909"}]}
    checkpoint = {"contract_hash": "sealed", "state": state, "state_hash": digest(state)}
    if corruption == "contract":
        checkpoint["contract_hash"] = "another"
    if corruption == "state":
        checkpoint["state_hash"] = "damaged"
    directory = tmp_path / "states"
    directory.mkdir()
    file = directory / "a.json"
    atomic_json(file, checkpoint)
    original = file.read_bytes()
    calls = []

    class Engine:
        def __init__(self, *args, **kwargs):
            self.state = {}

        def import_state(self, value):
            self.state = dict(value)

        def export_state(self):
            return self.state

        def run_one_day(self, date, plans):
            calls.append((date, self.state["cash"]))

    monkeypatch.setattr("scripts.validate_research_baseline.ResearchEngine", Engine)
    manifest = {"base_config": {}, "contract_hash": "sealed", "arms": {"a": {}},
                "dates": ["20260908", "20260909", "20260910"]}
    if corruption:
        with pytest.raises(ValueError, match="检查点"):
            prefetch_holding_evidence(manifest, tmp_path, None, checkpoint_dir=directory)
        assert not calls
    else:
        result = prefetch_holding_evidence(manifest, tmp_path, None, checkpoint_dir=directory)
        assert result["resumed_checkpoints"] == {"a": "20260909"}
        assert calls == [("20260910", 98)]
    assert file.read_bytes() == original


def test_progress_preview_lock_does_not_abort_financial_work(tmp_path, monkeypatch, capsys):
    def denied(*args):
        raise PermissionError("preview is open")
    monkeypatch.setattr("scripts.validate_research_baseline.atomic_json", denied)
    progress(tmp_path, stage="running", completed=1)
    assert json.loads(capsys.readouterr().out) == {"stage": "running", "completed": 1}


def test_repair_preflight_checks_every_strategy_and_date(tmp_path):
    date = "20260907"
    def put(path, payload):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(payload), encoding="utf-8")
    put("repair_checkpoint.json", {"completed": [date]})
    jobs = ["market_factor_job", "lhb_factor_job", "short_signal_factors", "sector_factor_job", "stock_factor_job"]
    put(f"factor_status/factors_{date}.json", {"trade_date": date, "jobs": [
        {"name": name, "trade_date": date, "ok": True} for name in jobs]})
    for strategy in PRODUCTION_STRATEGY_IDS:
        put(f"screening/combinations/{strategy}/screening_{date}.json",
            {"trade_date": date, "ok": True, "final": [], "final_count": 0})
    put(f"screening/decision_pool/decision_pool_{date}.json", {"trade_date": date})
    put(f"snapshots/{date}.json", {"meta": {"date": date}})
    validate_repair(tmp_path, [date])
    with pytest.raises(RuntimeError, match="策略版本不一致"):
        validate_repair(tmp_path, [date], strategy_versions={key: "fixed" for key in PRODUCTION_STRATEGY_IDS})
    put(f"screening/combinations/limit_pullback/screening_{date}.json",
        {"trade_date": date, "ok": True, "final": [], "final_count": 1})
    with pytest.raises(RuntimeError, match="limit_pullback"):
        validate_repair(tmp_path, [date])
    put(f"screening/combinations/limit_pullback/screening_{date}.json",
        {"trade_date": date, "ok": True, "final": [], "final_count": 0})
    put(f"snapshots/{date}.json", {"meta": {"date": "20260908"}})
    with pytest.raises(RuntimeError, match="快照"):
        validate_repair(tmp_path, [date])


def test_reconcile_closing_prices_does_not_hide_missing_evidence(tmp_path):
    path = tmp_path / "evidence.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence(kind TEXT,code TEXT,date TEXT,payload TEXT)")
        for code, minute_close in [("000001", 10), ("000002", 11), ("000003", None)]:
            connection.execute("INSERT INTO evidence VALUES(?,?,?,?)", ("daily", code, "20260907", json.dumps({"close": 10})))
            if minute_close is not None:
                bars = [{"time": "15:00:00", "open": 10, "high": 11, "low": 10,
                         "close": minute_close, "volume": 100, "amount": 1000}]
                connection.execute("INSERT INTO evidence VALUES(?,?,?,?)", ("minutes", code, "20260907", json.dumps(bars)))
    report = reconcile_prices(path)
    assert report["checked"] == 2
    assert report["unavailable"] == 1
    assert report["mismatches"] == [{"date": "20260907", "code": "000002", "daily_close": 10, "minute_close": 11}]


def test_failed_or_corrupt_factor_status_requires_recalculation(tmp_path):
    date = "20260907"
    directory = tmp_path / "factor_status"
    directory.mkdir()
    file = directory / f"factors_{date}.json"
    assert not factor_status_ready(tmp_path, date)
    file.write_text('{"jobs":', encoding="utf-8")
    assert not factor_status_ready(tmp_path, date)
    file.write_text('[]', encoding="utf-8")
    assert not factor_status_ready(tmp_path, date)
    file.write_text(json.dumps({"trade_date": date, "jobs": [{"name": "stock_factor_job", "ok": False}]}), encoding="utf-8")
    assert not factor_status_ready(tmp_path, date)


@pytest.mark.parametrize("force", [False, True])
def test_repair_rebuilds_invalid_or_forced_checkpoint_then_resumes(tmp_path, monkeypatch, force):
    import scripts.validate_research_baseline as module

    date = "20260907"
    root = tmp_path / "research"
    root.mkdir()
    (root / "factors.duckdb").write_bytes(b"placeholder")
    checkpoint = root / "repair_checkpoint.json"
    checkpoint.write_text(json.dumps({"completed": [date]}), encoding="utf-8")
    calls = []
    class Calendar:
        def get_trade_dates(self, start, end):
            return [f"202601{n:02d}" for n in range(1, 23)]
        def prev(self, value):
            return "20260904"
    class Profiles:
        def frozen(self):
            return self
        def get_profile(self, key):
            return {"version": "fixed"}
    class Pipeline:
        def __init__(self, *args, **kwargs):
            pass
        def compute_factors(self, *args):
            calls.append("factors")
            return SimpleNamespace(ok=True)
        def run_screening(self, *args, **kwargs):
            calls.append("screening")
            assert kwargs["archive_cases"] is False
            assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed"] == []
            return SimpleNamespace(ok=True, warnings=[])
    def invalid(*args, **kwargs):
        raise RuntimeError("configuration changed")
    monkeypatch.setattr(module, "WEB_DATA_DIR", tmp_path / "production")
    monkeypatch.setattr(module, "SNAPSHOT_DIR", tmp_path / "production_snapshots")
    monkeypatch.setattr(module, "DataManager", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "ETLDailyPipeline", Pipeline)
    monkeypatch.setattr(module, "StrategyProfileRepository", Profiles)
    monkeypatch.setattr(module, "factor_status_ready", lambda *args: True)
    monkeypatch.setattr(module, "validate_repair", invalid)
    repair(root, [date], Calendar(), recompute_all=force)
    assert calls == (["factors", "screening"] if force else ["screening"])
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed"] == [date]
    calls.clear()
    monkeypatch.setattr(module, "validate_repair", lambda *args, **kwargs: None)
    repair(root, [date], Calendar(), recompute_all=False)
    assert calls == []
