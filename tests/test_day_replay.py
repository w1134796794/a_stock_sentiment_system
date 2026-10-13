from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from backtest.minute_entry import normalize_minute_bars

from core.realtime.day_replay import (_ReplayEntryService, DayReplayLoader, compare_reports,
                                      replay_minutes, replay_request, replay_saved_inputs)
from core.realtime.day_replay_jobs import DayReplayJobs


def request(**extra):
    return replay_request({"market_date": "20261009", "until": "09:51", **extra}, now=datetime(2026, 10, 13, 16))


def bars(code="000001", date="20261009"):
    return pd.DataFrame([{"time": f"09:{31+i:02d}:00", "trade_date": date, "code": code,
                          "open": 10, "high": 10.1, "low": 9.9, "close": 10,
                          "volume": 10000, "amount": 100000} for i in range(20)])


def profile():
    return SimpleNamespace(available=False, load=lambda: {}, expected_fraction=lambda *a: (None, 0))


def test_validation_clamps_today_and_rejects_future_and_bad_codes():
    value = replay_request({"market_date": "20261013", "until": "15:01"}, now=datetime(2026, 10, 13, 10, 6))
    assert value["until"] == "10:06:00"
    assert replay_request({"market_date":"20261013"}, now=datetime(2026, 10, 13, 10, 6, 49))["until"] == "10:06:00"
    for payload in ({"market_date": "20261014"}, {"market_date": "20260230"}, {"codes": "../000001"},
                    {"codes": ["000001.SZ"]}, {"until": "23:00"}, {"fetch_missing": "false"}):
        with pytest.raises(ValueError):
            replay_request(payload, now=datetime(2026, 10, 13, 16))


def test_growing_prefixes_cannot_see_later_bars_or_fabricated_receipts(monkeypatch):
    seen = []
    def evaluate(self, row, quote, frame, *args):
        seen.append((quote, frame.copy()))
        return {"signal_status": "confirmed" if len(frame) >= 6 else "observe",
                "reason": "test", "confirm_time": "09:36:00" if len(frame) >= 6 else "",
                "sector_detail": {"data_completeness": 1}, "data_status": "minute"}
    monkeypatch.setattr(_ReplayEntryService, "_evaluate_one", evaluate)
    result = replay_minutes(request=request(), candidate_date="20261008", rows=[{"code": "000001"}],
                            frames={"000001": bars()}, previous={"000001": {"close": 10}}, profiles=profile())
    assert [len(f) for _, f in seen] == list(range(1, 21))
    assert all("received_at" not in q for q, _ in seen)
    assert all(f["time"].max() < q["time"] for q, f in seen)
    assert result["stocks"][0]["first_evaluated_at"] == "09:37:00"
    assert not result["live_runtime_verified"] and not result["notification_sent"] and not result["account_modified"]


@pytest.mark.parametrize("kind", ["gap", "wrong_date", "wrong_code", "empty"])
def test_bad_minutes_are_reported_not_guessed(kind):
    frame = bars()
    if kind == "gap": frame = frame.drop(8)
    if kind == "wrong_date": frame["trade_date"] = "20261008"
    if kind == "wrong_code": frame["code"] = "000002"
    if kind == "empty": frame = pd.DataFrame()
    result = replay_minutes(request=request(), candidate_date="20261008", rows=[{"code": "000001"}],
                            frames={"000001": frame}, previous={"000001": {"close": 10}}, profiles=profile())
    assert not result["complete"]
    assert result["failures"][0]["code"] == "000001"
    assert result["stocks"][0]["status"] == "missing_evidence"


def test_sector_checker_requires_same_minute_real_peers_not_target_itself():
    frames = {"000001": bars(), "000002": bars("000002"), "000003": bars("000003")}
    for code in ("000002", "000003"):
        frames[code]["close"] = 10.1
    frames = {c: normalize_minute_bars(f) for c, f in frames.items()}
    service = _ReplayEntryService({c:{"close":10} for c in frames}, {}, {"电池":list(frames)}, frames, profile())
    checker, detail = service._sector_checker({"sector_names":["电池"]}, "000001", [], {}, {}, "20261009")
    assert checker("09:36:00") is True
    assert detail["observed_members"] == 2
    assert checker("09:52:00") is None
    assert detail["coverage"] == 0


def test_missing_sector_does_not_turn_into_confirmation():
    result = replay_minutes(request=request(), candidate_date="20261008", rows=[{"code":"000001", "amount_ratio":1}],
                            frames={"000001":bars()}, previous={"000001":{"close":10, "amount":1000}}, profiles=profile())
    assert result["confirmed_stocks"] == 0
    assert any(f["evidence"] == "sector_minutes" for f in result["failures"])


def test_captured_input_replay_has_identical_input_hash_and_never_uses_loader():
    result = replay_minutes(request=request(), candidate_date="20261008", rows=[{"code":"000001"}],
                            frames={"000001":bars()}, previous={"000001":{"close":10}}, profiles=profile())
    saved = json.loads(json.dumps(result["replay_inputs"]))
    replayed = replay_saved_inputs(saved)
    assert replayed["input_hash"] == result["input_hash"]
    assert replayed["stocks"] == result["stocks"]


def test_baseline_rejects_changed_request(tmp_path):
    jobs = DayReplayJobs(tmp_path)
    identifier = "a" * 32
    path = jobs.path(identifier, "inputs.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"request":{k:v for k,v in request().items() if k!='fetch_missing'}}))
    with pytest.raises(ValueError, match="保持基准"):
        jobs.start({"market_date":"20261009", "until":"14:30", "baseline_id":identifier})


def test_same_inputs_compare_and_different_inputs_are_not_code_evidence():
    before = {"input_hash":"a", "code_hash":"old", "stocks":[{"code":"000001", "status":"observe"}]}
    after = {"input_hash":"a", "code_hash":"new", "stocks":[{"code":"000001", "status":"confirmed", "first_confirm_time":"09:46:00"}]}
    assert compare_reports(before, after)["comparable"]
    assert len(compare_reports(before, after)["changes"]) == 1
    after["input_hash"] = "b"
    assert not compare_reports(before, after)["comparable"]


def test_job_is_persistent_isolated_and_can_be_read_after_new_manager(tmp_path):
    class Loader:
        def run(self, request, progress):
            progress({"stage":"replay", "completed":1, "total":1})
            return {"classification":"minute_rule_diagnostic", "stocks":[], "input_hash":"a"}
    jobs = DayReplayJobs(tmp_path, loader_factory=Loader)
    run = jobs.start({"market_date":"20261009"})
    jobs.thread.join(timeout=5)
    assert not jobs.thread.is_alive()
    assert jobs.get(run["run_id"])["state"] == "done"
    assert DayReplayJobs(tmp_path).get(run["run_id"], report=True)["classification"] == "minute_rule_diagnostic"
    assert not (tmp_path / "portfolio").exists()
    with pytest.raises(ValueError): jobs.get("../status")


def test_progress_writes_are_throttled_but_terminal_progress_is_saved(tmp_path, monkeypatch):
    import core.realtime.day_replay_jobs as module
    writes = []
    original = module.atomic_write_json
    def recording_write(path, payload, **kwargs):
        if path.name == "status.json":
            writes.append(dict(payload))
        return original(path, payload, **kwargs)
    monkeypatch.setattr(module, "atomic_write_json", recording_write)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    class Loader:
        def run(self, request, progress):
            for i in range(1000):
                progress({"stage":"检查证据", "completed":i+1, "total":1000})
            return {"stocks":[], "classification":"minute_rule_diagnostic"}
    jobs = DayReplayJobs(tmp_path, loader_factory=Loader)
    result = jobs.start({"market_date":"20261013"})
    jobs.thread.join(timeout=5)
    assert not jobs.thread.is_alive()
    assert len(writes) == 3  # queued, first progress, terminal; never per stock.
    status = jobs.get(result["run_id"])
    assert status["state"] == "done" and status["progress"]["completed"] == 1000


def test_loader_fetch_uses_existing_minute_api_and_only_selected_day(tmp_path, monkeypatch):
    import core.realtime.overlay_service as overlays
    import scripts.diagnose_intraday_signal as diagnostics
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    monkeypatch.setattr(overlays, "RealtimeOverlayService", lambda **kw: SimpleNamespace(candidate_rows=lambda *a, **kw:[{"code":"000001"}]))
    monkeypatch.setattr(diagnostics, "runtime_evidence", lambda *a: {"status":"no_runtime_evidence", "rows":[]})
    monkeypatch.setattr(MinuteAmountProfileRepository, "load", lambda self: {})
    daily = tmp_path / "cache/stock/all_daily"
    daily.mkdir(parents=True)
    pd.DataFrame([{"ts_code":"000001.SZ", "close":10}]).to_csv(daily / "20261008.csv", index=False)
    calls = []
    dm = SimpleNamespace(get_stock_tick=lambda code,date:(calls.append((code,date)) or bars()), get_auction_data=lambda *a:{})
    loader = DayReplayLoader(web_data_dir=tmp_path, cache_dir=tmp_path / "cache", db_path=tmp_path / "missing.duckdb",
                            calendar=SimpleNamespace(prev=lambda date:"20261008"), data_manager=dm)
    result = loader.run(request(profile="decision_pool", fetch_missing=True))
    assert calls == [("000001.SZ", "20261009")]
    assert result["candidate_date"] == "20261008"
    assert result["input_basis"] == "research_candidates_not_frozen"
    assert not (tmp_path / "preopen_evidence").exists()


def test_loader_reads_collector_minute_cache_without_remote_access(tmp_path, monkeypatch):
    import core.realtime.overlay_service as overlays
    import scripts.diagnose_intraday_signal as diagnostics
    from core.signals.minute_amount_profile import MinuteAmountProfileRepository
    monkeypatch.setattr(overlays, "RealtimeOverlayService", lambda **kw: SimpleNamespace(candidate_rows=lambda *a, **kw:[{"code":"000001"}]))
    monkeypatch.setattr(diagnostics, "runtime_evidence", lambda *a: {"rows":[]})
    monkeypatch.setattr(MinuteAmountProfileRepository, "load", lambda self: {})
    daily = tmp_path / "cache/stock/all_daily"
    daily.mkdir(parents=True)
    pd.DataFrame([{"ts_code":"000001.SZ", "close":10}]).to_csv(daily / "20261008.csv", index=False)
    calls = []
    cache = SimpleNamespace(read=lambda date,code:(calls.append((date,code)) or bars()))
    loader = DayReplayLoader(web_data_dir=tmp_path, cache_dir=tmp_path / "cache", db_path=tmp_path / "missing.duckdb",
                            calendar=SimpleNamespace(prev=lambda date:"20261008"), minute_cache=cache)
    result = loader.run(request(profile="decision_pool"))
    assert calls == [("20261009", "000001")]
    assert result["minute_sources"]["000001"] == "collector_minute_cache"
    assert result["stocks"][0]["status"] != "missing_evidence"


def test_diagnostic_permissions_are_admin_only():
    from web.permissions import requires_admin
    assert requires_admin("/intraday/replay", "GET")
    assert requires_admin("/api/intraday-replay/abc", "GET")
    assert requires_admin("/api/intraday-replay", "POST")


def test_web_routes_validate_requests_and_serve_real_template(tmp_path, monkeypatch):
    import web.app as app
    from starlette.requests import Request
    monkeypatch.setattr(app, "_realtime_market_date", lambda: "20261009")
    monkeypatch.setattr(app.templates.env, "globals", {**app.templates.env.globals, "visible_menu_groups":lambda *a:[]})
    monkeypatch.setattr(app, "_DAY_REPLAY_JOBS", DayReplayJobs(tmp_path))
    request = Request({"type":"http", "path":"/intraday/replay", "headers":[], "query_string":b"",
                       "app":app.app, "state":{"user":{"role":"admin", "username":"test"}, "is_admin":True}})
    page = app.intraday_replay_page(request)
    assert page.status_code == 200 and b'dr-start' in page.body
    assert app.api_intraday_replay_start({"codes":"bad"}).status_code == 400
    assert app.api_intraday_replay_status("invalid", False).status_code == 400
    assert app.api_intraday_replay_recent()["runs"] == []
