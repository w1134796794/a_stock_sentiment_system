from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from core.operations.ledger import TradingLedger
from core.operations.replay_research import (
    ARMS,
    FrozenCalendar,
    FrozenEvidence,
    ReplayResearch,
    ResearchEngine,
    ResearchSpec,
    account_metrics,
    atomic_json,
    experiment_lock,
    select_plans,
)


def test_atomic_json_retries_transient_windows_reader(tmp_path, monkeypatch):
    from pathlib import Path

    file = tmp_path / "state.json"
    atomic_json(file, {"state": "previous"})
    original = Path.replace
    calls = []
    def replace(path, target):
        calls.append(target)
        if len(calls) < 3:
            assert json.loads(file.read_text(encoding="utf-8")) == {"state": "previous"}
            exc = PermissionError("busy reader")
            exc.winerror = 5
            raise exc
        return original(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("core.operations.replay_research.time.sleep", lambda value: None)
    atomic_json(file, {"state": "complete"})
    assert len(calls) == 3
    assert json.loads(file.read_text(encoding="utf-8")) == {"state": "complete"}


def test_atomic_json_does_not_hide_permanent_write_failure(tmp_path, monkeypatch):
    from pathlib import Path

    file = tmp_path / "state.json"
    atomic_json(file, {"state": "previous"})
    def denied(path, target):
        raise PermissionError("permission denied")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        atomic_json(file, {"state": "complete"})
    assert json.loads(file.read_text(encoding="utf-8")) == {"state": "previous"}


class Calendar(FrozenCalendar):
    pass


class Data:
    def get_stock_tick(self, code, date):
        times = pd.date_range(f"{date} 09:31", f"{date} 11:30", freq="min").append(
            pd.date_range(f"{date} 13:01", f"{date} 15:00", freq="min"))
        return pd.DataFrame({"time": times.strftime("%H:%M:%S"), "trade_date": date,
                             "open": 10, "high": 10.1, "low": 10, "close": 10,
                             "volume": 10000, "amount": 100000, "volume_unit": "shares"})

    def get_stock_daily_data(self, code, date):
        return {"open": 10, "high": 10.1, "low": 10, "close": 10, "pre_close": 10, "vol": 100, "amount": 1000}

    def get_auction_data(self, code, date):
        return {}

    def get_suspension_status(self, code, date, **kwargs):
        return {"state": "trading"}


@pytest.fixture
def research(tmp_path, monkeypatch):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, tzinfo=tz)
    monkeypatch.setattr("core.operations.replay_research.datetime", FixedTime)
    service = ReplayResearch(root=tmp_path / "research", ledger=TradingLedger(tmp_path / "research.sqlite"))
    calendar = Calendar(["20261008", "20261009", "20261012"])
    plans = tmp_path / "plans"
    plans.mkdir()
    frame = pd.DataFrame([{"代码": "000001", "名称": "测试", "策略ID": "weak_to_strong", "综合评分": 60,
        "入场门控来源": "production_decision_v1", "生产决策允许入场": True,
        "策略执行": json.dumps({"allowed_entry_modes": ["weak_to_strong"], "confirmation_deadline": "10:30:00"}),
        "模式": "弱转强", "目标价": 0, "共振板块": "电池"}])
    for date in ["20261008", "20261009"]:
        frame.to_csv(plans / f"交易计划_{date}.csv", index=False)
    monkeypatch.setattr("core.operations.replay_research.BacktestEngine._load_sector_peer_codes", lambda *args, **kwargs: {"000001": []})
    return service, calendar, plans


def freeze(research):
    service, calendar, plans = research
    return service.freeze(plans_dir=plans, start="20261009", end="20261012", data_manager=Data(), calendar=calendar)


def test_contract_freezes_all_inputs_and_readonly_evidence(research):
    manifest = freeze(research)
    service, _, _ = research
    assert service.verify(manifest["experiment_id"])["data_hash"] == manifest["data_hash"]
    evidence = FrozenEvidence(service._directory(manifest["experiment_id"]))
    assert evidence.get_stock_daily_data("000001.SZ", "20261012")["close"] == 10
    assert len(evidence.get_stock_tick("000001", "20261009")) == 240
    assert manifest["production_changes"] is False
    assert manifest["release_allowed"] is False


def test_tampered_data_refused(research):
    manifest = freeze(research)
    service, _, _ = research
    path = service._directory(manifest["experiment_id"]) / "amount_profile.json"
    path.write_text('{"new":"model"}')
    with pytest.raises(ValueError, match="冻结数据"):
        service.verify(manifest["experiment_id"])


def test_tampered_parameter_refused(research):
    manifest = freeze(research)
    service, _, _ = research
    manifest["spec"]["fixed_risk_per_trade"] = 0.02
    atomic_json(service._directory(manifest["experiment_id"]) / "manifest.json", manifest)
    with pytest.raises(ValueError, match="合同"):
        service.verify(manifest["experiment_id"])


def test_old_plan_and_unknown_zero_candidate_day_refused(research):
    service, calendar, plans = research
    (plans / "交易计划_20261008.csv").unlink()
    with pytest.raises(ValueError, match="零候选"):
        freeze(research)
    pd.DataFrame([{"代码": "000001", "综合评分": 80}]).to_csv(plans / "交易计划_20261008.csv", index=False)
    with pytest.raises(ValueError, match="生产决策"):
        freeze(research)


def test_selection_is_within_strategy_deterministic():
    frame = pd.DataFrame([{"代码": c, "策略ID": s, "综合评分": v} for c, s, v in
        [("000002", "a", 60), ("000001", "a", 60), ("000003", "b", 30), ("000004", "b", 40)]])
    assert set(select_plans(frame, 0.5)["代码"]) == {"000001", "000004"}
    assert len(frame) == 4


def test_fixed_risk_only_changes_sizing(research):
    manifest = freeze(research)
    service, _, _ = research
    dm = FrozenEvidence(service._directory(manifest["experiment_id"]))
    engines = [ResearchEngine(dm, BacktestConfig(**manifest["base_config"]), manifest, arm=arm) for arm in ARMS]
    assert all(asdict(e.config) == manifest["base_config"] for e in engines)
    plan = pd.Series({"代码": "000001", "策略执行": json.dumps({"exit": {"hard_stop_loss": 0.05}})})
    assert engines[0]._calculate_position_size(plan) == 340000
    for engine in engines[1:]:
        engine.research_entry_prices["000001"] = 10
        assert 90000 < engine._calculate_position_size(plan) < 100000
        assert engine.config.account_position_pct == 0.34  # Identical cap semantics, separate sizing override.


def test_metrics_include_first_loss_and_exact_costs():
    state = {"daily_nav": [{"date": "20261009", "total_value": 90}, {"date": "20261012", "total_value": 95}],
             "trade_history": [{"date": "20261009", "entry_date": "20261009", "action": "BUY", "entry_price": 10,
                                "shares": 2, "position_size": 21},
                               {"date": "20261012", "entry_date": "20261009", "action": "SELL", "exit_price": 12,
                                "shares": 2, "position_size": 21, "pnl": 2, "pnl_pct": 2/21}]}
    result = account_metrics(state, 100)
    assert result["max_drawdown_pct"] == pytest.approx(10)
    assert result["fees"] == 2
    assert result["return_pct"] == pytest.approx(-5)
    assert account_metrics(state, 100, start="20261012")["closed_trades"] == 0  # Carried-in trade is not a new validation cohort.


def test_independent_run_and_resume_no_duplicate_days(research, monkeypatch):
    manifest = freeze(research)
    service, _, _ = research
    def day(engine, date, directory):
        engine.daily_nav.append({"date": date, "cash": engine.cash, "position_value": 0,
                                 "total_value": engine.cash, "position_count": 0})
        return {}
    monkeypatch.setattr(ResearchEngine, "run_one_day", day)
    result = service.run(manifest["experiment_id"])
    again = service.run(manifest["experiment_id"])
    assert result["status"] == "complete"
    assert all(len(r["nav"]) == 2 for r in again["results"].values())
    assert len(service.ledger.list_experiments(kind="replay")) == 1
    assert service.ledger.list_experiments(kind="strategy") == []
    assert again["release_allowed"] is False


def test_validation_outcomes_do_not_select_training_winner(research):
    service, _, _ = research
    directory = service.root / "rolling"
    directory.mkdir(parents=True)
    spec = ResearchSpec(train_days=2, validation_days=2, embargo_days=1, min_train_closed=1)
    dates = ["20261012", "20261013", "20261014", "20261015", "20261016"]
    results = {}
    for arm in ARMS:
        state = {"daily_nav": [{"date": d, "total_value": (101 if arm == "fixed_risk" else 100) if i < 2
                               else (50 if arm == "fixed_risk" else 200)} for i, d in enumerate(dates)],
                 "trade_history": [{"action": "SELL", "date": dates[1], "entry_date": dates[0],
                     "pnl": 1, "pnl_pct": 0.01, "position_size": 10, "shares": 1, "exit_price": 11}]}
        file = directory / f"{arm}.json"
        atomic_json(file, {"state": state})
        results[arm] = {"state_path": file.name, "complete": True, "data_quality": {}}
    report = {"spec": asdict(spec), "dates": dates, "results": results, "base_config": {"initial_capital": 100}, "regimes": {}}
    folds = service._rolling(report, directory)
    assert len(folds) == 1
    assert folds[0]["selected_arm"] == "fixed_risk"
    assert folds[0]["train_end"] < folds[0]["validation_start"]
    assert folds[0]["classification"] == "retrospective_walk_forward"


@pytest.mark.parametrize("kwargs", [{"fixed_risk_per_trade": 0}, {"train_days": 0}, {"selection_fraction": 2}, {"missing_evidence_policy": "legacy"}])
def test_spec_validation(kwargs):
    with pytest.raises(ValueError):
        ResearchSpec(**kwargs)


def test_same_experiment_process_lock(tmp_path):
    file = tmp_path / "run.lock"
    with experiment_lock(file):
        with pytest.raises(ValueError, match="其他进程"):
            with experiment_lock(file):
                pass
    with experiment_lock(file):
        pass


@pytest.mark.parametrize("supported", [True, False])
def test_timing_waits_for_completed_bars_and_next_open(research, monkeypatch, supported):
    manifest = freeze(research)
    service, _, _ = research
    engine = ResearchEngine(FrozenEvidence(service._directory(manifest["experiment_id"])),
                            BacktestConfig(**manifest["base_config"]), manifest, arm="timing")
    plan = pd.read_csv(service._directory(manifest["experiment_id"]) / "plans/timing/交易计划_20261008.csv",
                       dtype={"代码": str}).iloc[0]
    engine._minute_frames[("20261009", "000001")] = pd.DataFrame({
        "time": ["09:41:00", "09:42:00", "09:43:00", "09:44:00"],
        "close": [10, 10.1 if supported else 9.8, 10.2, 10.3], "vwap": 10,
        "open": [10, 10, 10.1, 10.2], "volume": 1000})
    def original(engine, *args):
        engine._last_entry_meta["000001"] = {"confirm_time": "09:41:00", "entry_time": "09:42:00"}
        engine._last_entry_gap["000001"] = 0
        engine.entry_attempts.append({"status": "filled", "entry_mode": "weak_to_strong"})
        return True, 10
    monkeypatch.setattr(BacktestEngine, "_check_buy_conditions", original)
    monkeypatch.setattr(engine, "_sector_sync_checker", lambda *args: lambda time: True)
    allowed, price = engine._check_buy_conditions(plan, "20261009", "000001", "测试")
    assert allowed is supported
    if supported:
        assert price == pytest.approx(10.2 * (1 + engine.config.slippage))
        assert engine._last_entry_meta["000001"]["confirm_time"] == "09:43:00"
        assert engine._last_entry_meta["000001"]["entry_time"] == "09:44:00"
    else:
        assert price == 0
        assert not any(row["status"] == "filled" for row in engine.entry_attempts)


def test_sizing_uses_this_prepared_event_price(research, monkeypatch):
    manifest = freeze(research)
    service, _, _ = research
    engine = ResearchEngine(FrozenEvidence(service._directory(manifest["experiment_id"])),
                            BacktestConfig(**manifest["base_config"]), manifest, arm="fixed_risk")
    engine.research_entry_prices["000001"] = 99
    monkeypatch.setattr(BacktestEngine, "_execute_buy", lambda *args, **kwargs: None)
    engine._execute_buy(pd.Series({"代码": "000001"}), "20261009", entry_decision=(True, 10))
    assert engine.research_entry_prices["000001"] == 10


def test_real_engine_blocks_missing_sector_instead_of_daily_guess(research):
    manifest = freeze(research)
    service, _, _ = research
    result = service.run(manifest["experiment_id"])
    assert result["status"] == "incomplete"
    assert all(not r["complete"] and r["blocked"] for r in result["results"].values())
    assert result["release_allowed"] is False
    assert not any(r["nav"] for r in result["results"].values())


def test_real_engine_skip_persists_incomplete_coverage_after_resume(research):
    service, calendar, plans = research
    manifest = service.freeze(plans_dir=plans, start="20261009", end="20261012", data_manager=Data(),
                               calendar=calendar, spec=ResearchSpec(missing_evidence_policy="skip_candidates"))
    result = service.run(manifest["experiment_id"])
    again = service.run(manifest["experiment_id"])
    assert result["status"] == again["status"] == "incomplete"
    for arm in ARMS:
        assert len(again["results"][arm]["nav"]) == 2
        assert again["results"][arm]["data_quality"]["skipped_candidate_count"] == 2
        assert again["results"][arm]["data_quality"]["candidate_coverage_pct"] == 0
