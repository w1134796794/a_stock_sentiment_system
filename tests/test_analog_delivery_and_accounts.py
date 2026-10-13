from dataclasses import replace

import pytest

from core.operations.ledger import TradingLedger
from core.portfolio.analog_experiment import AnalogAccountExperiment
from core.portfolio.signal_comparison import ComparisonConfig, SignalComparisonService, freeze_holding_state, mature_holding_state
from core.signals.analog_runtime import AnalogRuntime, notification_summary


def forecast(config, at="2026-09-01T09:40:00", expected=.1):
    return {"status": "estimated", "forecast_id": "estimate", "as_of": at, "horizon_days": config.horizon_days,
            "cost_version": config.cost_version, "reliability": {"sufficient": True},
            "probability_interval": {"low": .6, "high": .8},
            "expected_return_interval": {"low": expected, "high": expected + .01},
            "return_distributions": {"net_return": {"p05": -.03}}, "probability": .7,
            "expected_net_return": expected, "prediction_generated_at": at}


def quote(at, price=10., **extra):
    return {"last_price": price, "observed_at": at, "volume": 1000, "limit_pct": 10,
            "limit_up_locked": False, "limit_down_locked": False, **extra}


def event(at, *, signals=None, codes=("600001",), **extra):
    return {"at": at, "quotes": {c: quote(at) for c in codes}, "signals": signals or [], **extra}


def tape(config):
    at = "2026-09-01T09:40:00"
    signal = {"signal_id": "s1", "code": "600001", "confirm_at": at, "forecast_id": "estimate",
              "forecast": forecast(config, at), "strength": 60, "theme": "A"}
    return [event(at, signals=[signal]), event("2026-09-01T09:41:00"),
            event("2026-09-01T15:00:00", session_close=True),
            event("2026-09-02T09:40:00", exits=["600001"]),
            event("2026-09-02T09:41:00"), event("2026-09-02T15:00:00", session_close=True)]


def test_immutable_jobs_and_read_only_results(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.db")
    case = {"case_id": "c", "signal_id": "s", "code": "600001", "market_date": "20260902", "candidate_date": "20260901"}
    first = ledger.enqueue_analog(case)
    ledger.enqueue_analog({**case, "code": "bad"})
    assert ledger.pending_analog_jobs()[0]["case"]["code"] == "600001"
    assert ledger.claim_analog_job(first["forecast_id"])
    assert not ledger.claim_analog_job(first["forecast_id"])
    runtime = AnalogRuntime(ledger, tmp_path / "absent.json")
    saved = runtime.score_job({"forecast_id": first["forecast_id"], "signal_id": "s", "case_id": "c", "case": case})
    assert saved["forecast_id"] == first["forecast_id"]
    assert ledger.analog_analysis(market_date="20260901", code="600001")["forecast_id"] == first["forecast_id"]
    assert not ledger.pending_analog_jobs()
    assert "尚未完成" in notification_summary(saved)
    ledger.save_case_forecast({**saved, "probability": .99})
    assert ledger.case_forecast(saved["forecast_id"])["probability"] is None


def test_pagination_frozen_matches(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.db")
    ledger.save_case_forecast({"forecast_id": "f", "case_id": "c", "signal_id": "s", "model_version": "m",
                              "as_of": "2026-09-01T09:40:00", "matches": [{"case_id": str(i), "distance": i / 10} for i in range(12)]})
    assert ledger.analog_matches("f", offset=10)["total"] == 12
    assert len(ledger.analog_matches("f", offset=10)["rows"]) == 2
    assert "matches" not in ledger.analog_analysis(signal_id="s")


def test_worker_recovers_existing_queue_and_stops(tmp_path, monkeypatch):
    from threading import Event

    ledger = TradingLedger(tmp_path / "ledger.db")
    ledger.enqueue_analog({"case_id": "restart", "signal_id": "s", "code": "600001",
                           "market_date": "20260901"})
    runtime = AnalogRuntime(ledger, tmp_path / "absent.json")
    completed = Event()
    score = runtime.score_job

    def observed(job):
        result = score(job)
        completed.set()
        return result

    monkeypatch.setattr(runtime, "score_job", observed)
    runtime.start()
    try:
        assert completed.wait(5)
        assert not ledger.pending_analog_jobs()
    finally:
        runtime.stop()
    assert not runtime._thread.is_alive()


@pytest.mark.parametrize("change", ["today", "old_forecast", "wrong_horizon", "missing_cost", "same_theme"])
def test_rotation_guards(change):
    config = ComparisonConfig(max_theme_positions=1)
    service = SignalComparisonService(config)
    new = forecast(config, expected=.2)
    old = {**forecast(config, expected=.03), "state_id": "holding"}
    holding = {"code": "600002", "entry_date": "20260831", "theme": "B", "forecast": old,
               "sellable": True, "executable": True, "proceeds": 1000, "rotation_cost_return": .002}
    if change == "today": holding["entry_date"] = "20260901"
    if change == "old_forecast": holding["forecast"]["as_of"] = "2026-08-31T09:40:00"
    if change == "wrong_horizon": holding["forecast"]["horizon_days"] = 3
    if change == "missing_cost": holding.pop("rotation_cost_return")
    holdings = [holding]
    if change == "same_theme": holdings.append({**holding, "code": "600003", "theme": "A", "sellable": False})
    result = service.compare(new, holdings, cash=0, required_cash=500, theme="A", market_date="20260901")
    assert result["recommended_research_action"] == "no_action"


def test_rotation_and_cash_comparison():
    config = ComparisonConfig()
    service = SignalComparisonService(config)
    new = forecast(config, expected=.2)
    holding = {"code": "600002", "entry_date": "20260831", "theme": "B",
               "forecast": {**forecast(config, expected=.03), "state_id": "holding"},
               "sellable": True, "executable": True, "proceeds": 1000, "rotation_cost_return": .002}
    assert service.compare(new, [holding], cash=0, required_cash=500, theme="A", market_date="20260901")["recommended_research_action"] == "rotate"
    assert service.compare(new, [holding], cash=1000, required_cash=500, theme="A", market_date="20260901")["recommended_research_action"] == "cash_buy"
    assert service.compare(new, [holding], cash=0, required_cash=500, theme="A", market_date="20260901", allow_rotation=False)["recommended_research_action"] == "no_action"


def test_holding_state_no_reuse_entry_probability():
    config = ComparisonConfig()
    at = "2026-09-01T09:40:00"
    state = freeze_holding_state({"code": "600001", "entry_price": 9., "entry_date": "20260831",
        "structural_stop": 8., "strategy_id": "weak_to_strong", "metadata": {"strategy_version": "v", "probability": .99}},
        as_of=at, holding_days=1, quote=quote(at), market={"market_score": 50, "observed_at": at},
        sector={"breadth": .6, "observed_at": at}, config=config)
    assert state["pit_verified"]
    assert state["features"]["pnl"] == pytest.approx(10 / 9 - 1)
    result = SignalComparisonService(config).holding_horizon(state, [])
    assert result["status"] == "insufficient"
    assert result.get("probability") is None
    end = "2026-09-08T15:00:00"
    label = mature_holding_state(state, horizon_end=end, exit_quote=quote(end), reference_price=10,
                                 available_at=end, config=config)
    assert label["label"]["value"] < 0  # Forward sell costs are charged.
    locked = mature_holding_state(state, horizon_end=end, exit_quote=quote(end, limit_down_locked=True),
                                  reference_price=10, available_at=end, config=config)
    assert locked["label"] is None


def test_three_accounts_next_observation_and_idempotence(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.db")
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=ledger, comparison=config)
    contract = runner.freeze()
    report = runner.replay(contract["experiment_id"], tape(config), trading_dates=["20260901", "20260902"], research=True)
    assert report["status"] == "complete"
    for account in report["accounts"].values():
        assert len(account["fills"]) == 2
        assert account["fills"][0]["at"].endswith("09:41:00")
        assert account["fills"][1]["side"] == "sell"
        assert account["summary"]["fees"] > 0
        assert account["summary"]["max_drawdown_pct"] > 0
        assert account["account_key"].startswith("analog:")
    again = runner.replay(contract["experiment_id"], tape(config), trading_dates=["20260901", "20260902"], research=True)
    assert again == report
    assert not report["production_release_allowed"]


def test_missing_quotes_roll_back_entire_day(tmp_path):
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=TradingLedger(tmp_path / "ledger.db"), comparison=config)
    contract = runner.freeze()
    events = tape(config)
    events[1]["quotes"] = {}
    result = runner.replay(contract["experiment_id"], events, trading_dates=["20260901", "20260902"], research=True)
    assert result["status"] == "blocked"
    assert result["completed_dates"] == []
    assert all(a["fills"] == [] for a in result["accounts"].values())


def test_t1_and_limit_lock_never_sell_same_day(tmp_path):
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=TradingLedger(tmp_path / "ledger.db"), comparison=config)
    contract = runner.freeze()
    events = tape(config)
    events.insert(2, event("2026-09-01T09:42:00", exits=["600001"]))
    events[5]["quotes"]["600001"]["limit_down_locked"] = True
    events[6]["quotes"]["600001"]["limit_down_locked"] = True
    result = runner.replay(contract["experiment_id"], events, trading_dates=["20260901", "20260902"], research=True)
    for account in result["accounts"].values():
        assert len(account["fills"]) == 1
        assert account["positions"]["600001"]["shares"] > 0


def test_future_model_not_used_in_forward_replay(tmp_path):
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=TradingLedger(tmp_path / "ledger.db"), comparison=config)
    contract = runner.freeze()
    report = runner.replay(contract["experiment_id"], tape(config), trading_dates=["20260901", "20260902"])
    assert len(report["accounts"]["original_rules"]["fills"]) == 2
    assert report["accounts"]["analog_filter"]["fills"] == []


def test_future_forecast_cutoff_rejected_even_in_research(tmp_path):
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=TradingLedger(tmp_path / "ledger.db"), comparison=config)
    contract = runner.freeze()
    events = tape(config)
    events[0]["signals"][0]["forecast"]["as_of"] = "2026-09-02T09:40:00"
    report = runner.replay(contract["experiment_id"], events, trading_dates=["20260901", "20260902"], research=True)
    assert report["accounts"]["analog_filter"]["fills"] == []


def test_analog_api_permission_boundaries():
    from web.permissions import permission_key_for_path, can_access_path
    assert permission_key_for_path("/api/signals/id/analog-analysis") == "workbench"
    assert permission_key_for_path("/api/v1/workbench/analog-analysis") == "workbench"
    assert permission_key_for_path("/api/operations/experiments/id/analog-validation") == "strategy_evaluation"
    assert not can_access_path(None, "/api/signals/id/analog-analysis")


def test_contract_changes_rejected(tmp_path):
    ledger = TradingLedger(tmp_path / "ledger.db")
    runner = AnalogAccountExperiment(ledger=ledger)
    contract = runner.freeze()
    changed = AnalogAccountExperiment(ledger=ledger, comparison=replace(ComparisonConfig(), horizon_days=3))
    with pytest.raises(ValueError, match="Frozen"):
        changed.replay(contract["experiment_id"], tape(ComparisonConfig()), trading_dates=["20260901", "20260902"])


def test_runtime_estimate_is_frozen_with_winners_losers(tmp_path):
    import json
    from dataclasses import asdict
    from backtest.backtest_engine import BacktestConfig
    from core.signals.analog_runtime import query_record
    from core.signals.similar_case_service import SIMILARITY_VERSION
    from test_similar_case_service import record, sample
    _, records = sample()
    query = record("20260210")["case"]
    query["backtest_config"] = asdict(BacktestConfig())
    versions = query_record(query)["versions"]
    for r in records:
        r["versions"] = versions
    index = tmp_path / "matching_index.json"
    index.write_text(json.dumps({"index_version": SIMILARITY_VERSION, "data_version": "test", "records": records}), encoding="utf-8")
    ledger = TradingLedger(tmp_path / "ledger.db")
    queued = ledger.enqueue_analog(query)
    saved = AnalogRuntime(ledger, index).score_job(ledger.pending_analog_jobs()[0])
    assert saved["status"] == "estimated"
    assert saved["forecast_id"] == queued["forecast_id"]
    assert {m["success"] for m in saved["matches"]} == {True, False}
    assert "不保证盈利" in notification_summary(saved)


def test_workbench_api_uses_existing_envelope_and_never_scores(tmp_path, monkeypatch):
    import json
    from web import app as webapp
    ledger = TradingLedger(tmp_path / "ledger.db")
    monkeypatch.setattr(webapp, "_trading_ledger", lambda: ledger)
    response = webapp.api_workbench_analog_analysis("20260901", "600001")
    value = json.loads(response.body)
    assert value["ok"]
    assert value["data"]["analysis"]["status"] == "not_ready"
    assert ledger.pending_analog_jobs() == []


def test_holding_statistics_exclude_future_labels_and_deduplicate():
    import copy
    config = ComparisonConfig(min_cases=4, min_days=2, min_effective=3)
    now = "2026-09-01T09:40:00"
    query = freeze_holding_state({"code": "600001", "entry_price": 10, "entry_date": "20260831", "structural_stop": 9,
        "strategy_id": "s", "metadata": {"strategy_version": "v"}}, as_of=now, holding_days=1,
        quote=quote(now), market={"market_score": 60, "observed_at": now}, sector={"breadth": .6, "observed_at": now}, config=config)
    records = []
    for day in range(1, 7):
        state = copy.deepcopy(query)
        state.update(state_id=f"h{day}", as_of=f"2026-08-{day:02d}T09:40:00")
        records.append({"state": state, "label": {"available_at": f"2026-08-{day+10:02d}T20:00:00",
            "label_end": f"2026-08-{day+10:02d}T15:00:00", "value": .02 if day % 2 else -.01}})
    future = copy.deepcopy(records[0]); future["state"]["state_id"] = "future"; future["label"]["available_at"] = "2026-09-02T00:00:00"
    result = SignalComparisonService(config).holding_horizon(query, records + records + [future])
    assert result["status"] == "estimated"
    assert result["reliability"]["raw_sample_count"] == 6
    assert "future" not in result["sample_case_ids"]


def test_full_account_rotation_uses_new_holding_state(tmp_path):
    import copy
    from core.portfolio.analog_experiment import AccountExperimentConfig
    config = ComparisonConfig()
    runner = AnalogAccountExperiment(ledger=TradingLedger(tmp_path / "ledger.db"),
                                    config=AccountExperimentConfig(position_pct=1, max_positions=1), comparison=config)
    contract = runner.freeze()
    events = tape(config)
    first = events[0]["signals"][0]
    first.update(strategy_id="s", config_version="v", structural_stop=9)
    at = "2026-09-02T09:40:00"
    second = {**first, "code": "600002", "signal_id": "s2", "confirm_at": at, "strength": 100,
              "theme": "B", "forecast": forecast(config, at, expected=.3)}
    events[3].pop("exits")
    events[3]["signals"] = [second]
    for e in events[3:]:
        e["quotes"]["600002"] = quote(e["at"])
        e["market"] = {"market_score": 60, "observed_at": e["at"]}
        e["sectors"] = {"600001": {"breadth": .6, "observed_at": e["at"]}}
    state = freeze_holding_state({"code": "600001", "entry_price": 10.01, "entry_date": "20260901", "structural_stop": 9,
        "strategy_id": "s", "metadata": {"strategy_version": "v"}}, as_of=at, holding_days=1,
        quote=quote(at), market={"market_score": 60, "observed_at": at}, sector={"breadth": .6, "observed_at": at}, config=config)
    historical = []
    for day in range(1, 21):
        old = copy.deepcopy(state)
        old.update(state_id=str(day), as_of=f"2026-08-{day:02d}T09:40:00")
        historical.append({"state": old, "label": {"value": .01, "available_at": "2026-08-28T20:00:00", "label_end": "2026-08-28T15:00:00"}})
    report = runner.replay(contract["experiment_id"], events, trading_dates=["20260901", "20260902"],
                           historical_states=historical, research=True)
    assert report["status"] == "complete"
    assert list(report["accounts"]["analog_filter"]["positions"]) == ["600001"]
    account = report["accounts"]["analog_rotation"]
    assert list(account["positions"]) == ["600002"]
    assert [f["side"] for f in account["fills"]] == ["buy", "sell", "buy"]
    assert account["comparisons"][-1]["recommended_research_action"] == "rotate"


def test_partial_exit_costs_and_forecast_binding(tmp_path):
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.holding_service import HoldingService
    repo = HoldingRepository(tmp_path / "ledger.db")
    repo.ensure_account("default", name="test", initial_capital=1_000_000)
    position = repo.open_position({"code": "600001", "entry_date": "20260901", "entry_price": 10, "shares": 100,
        "metadata": {"forecast_id": "f", "analog_at_order": {"status": "not_ready"}}})
    ledger = TradingLedger(repo.db_path)
    ledger.save_case_forecast({"forecast_id": "f", "case_id": "c", "model_version": "m", "as_of": "2026-09-01T09:40:00",
        "status": "estimated", "probability": .6, "expected_net_return": .02})
    dashboard = HoldingService(repository=repo).dashboard()
    trade = dashboard["trades"][0]
    assert trade["entry_analog"]["forecast_id"] == "f"
    assert trade["entry_analog"]["ready_at_order"] is False
