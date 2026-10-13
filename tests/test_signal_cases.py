from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig
from backtest.minute_entry import EntryDecision
from core.operations.evidence import attach_evidence
from core.operations.ledger import TradingLedger
from core.signals.case_features import build_signal_case
from core.signals.case_outcomes import CaseDataSource, SignalCaseBuilder, closed_trade_result, validate_minutes
from core.signals.case_repository import SignalCaseRepository
from core.signals.confidence_service import HistoricalSignalStatsRepository


class Calendar:
    is_real = True
    days = ["20260831", "20260901", "20260902", "20260903", "20260904", "20260907", "20260908", "20260909"]

    def next(self, date):
        return self.days[self.days.index(date) + 1]

    def prev(self, date):
        return self.days[self.days.index(date) - 1]

    def offset(self, date, count):
        return self.days[self.days.index(date) + count]

    def get_trade_dates(self, start, end):
        return [date for date in self.days if start <= date <= end]

    def holding_days(self, start, end):
        return self.days.index(end) - self.days.index(start)


def bars(date="20260901", price=10):
    times = []
    for start in ("09:30", "13:00"):
        moment = datetime.strptime(start, "%H:%M")
        times += [(moment + timedelta(minutes=i)).strftime("%H:%M:%S") for i in range(120)]
    return pd.DataFrame({"time": times, "date": date, "open": price, "high": price,
                         "low": price, "close": price, "volume": 1000, "amount": price * 1000})


def stats_csv(tmp_path, *, version="v1", run="1", sell_date="20260902", available="2026-09-02T15:01:00", partial=False):
    common = {"stock_code": "000001", "entry_date": "20260901", "entry_signal": "弱转强",
              "strategy_id": "weak_to_strong", "strategy_version": version, "exit_rule_version": "exit1",
              "cost_version": "cost1", "entry_price": 10, "mfe_pct": .12, "mae_pct": -.03}
    records = [{**common, "date": "20260901", "action": "BUY", "shares": 100,
                "position_size": 1001, "pnl": 0},
               {**common, "date": sell_date, "exit_time": "10:00:00", "action": "SELL",
                "shares": 50 if partial else 100, "position_size": 1001, "pnl": 99,
                "result_available_at": available}]
    pd.DataFrame(records).to_csv(tmp_path / f"backtest_trades_{run}.csv", index=False)


def test_history_requires_closed_available_result_and_version(tmp_path):
    stats_csv(tmp_path)
    repository = HistoricalSignalStatsRepository(tmp_path)
    assert not repository.get("弱转强", as_of_date="20260902")
    assert not repository.get("弱转强", as_of_time="2026-09-02T15:01:00")
    result = repository.get("弱转强", as_of_date="20260903")
    assert result["sample_size"] == 1
    assert result["expected_return"] == pytest.approx(99 / 1001)
    assert result["calibration_status"] == "uncalibrated"
    stats_csv(tmp_path, run="2")
    assert repository.get("弱转强", as_of_date="20260903")["sample_size"] == 1
    stats_csv(tmp_path, version="v2", run="3")
    assert not repository.get("弱转强", as_of_date="20260903")
    assert repository.get("弱转强", as_of_date="20260903", strategy_version="v1")["sample_size"] == 1


def test_history_does_not_count_partial_or_late_published_trades(tmp_path):
    stats_csv(tmp_path, partial=True)
    assert not HistoricalSignalStatsRepository(tmp_path).get("弱转强", as_of_date="20260903")
    stats_csv(tmp_path, available="2026-10-01T20:00:00")
    assert not HistoricalSignalStatsRepository(tmp_path).get("弱转强", as_of_date="20260903")
    assert HistoricalSignalStatsRepository(tmp_path).get("弱转强", as_of_date="20261002")["sample_size"] == 1


def test_case_features_are_prefix_only_and_missing_not_neutral():
    row = {"code": "000001", "entry_mode": "weak_only", "confirm_time": "09:35:00", "strategy_version": "v1"}
    context = {"market_date": "20260901", "candidate_date": "20260831"}
    full = bars()
    full.loc[full["time"] > "09:35:00", ["close", "high", "amount"]] = 99999
    first = build_signal_case(row, context, minutes=full.iloc[:6])
    second = build_signal_case(row, context, minutes=full)
    assert first["features"] == second["features"]
    assert second["features"]["confirm_close"] == 10
    assert second["features"]["profit_effect_score"] is None
    assert "profit_effect_score" in second["quality"]["missing_fields"]
    assert not second["pit_verified"]
    row["sector_detail"] = {"observed_at": "2026-09-01T10:00:00", "change_pct": 8}
    assert build_signal_case(row, context, origin="reconstructed")["features"]["sector_change_pct"] is None
    live = build_signal_case(row, context)
    assert live["confirm_at"].endswith("10:00:00")
    assert live["features"]["sector_change_pct"] == 8


def test_first_valid_case_is_immutable_across_strategies_and_refreshes(tmp_path):
    ledger = TradingLedger(tmp_path / "holdings.sqlite")
    payload = {"candidate_date": "20260831", "market_date": "20260901", "rows": [
        {"code": "000001", "confirm_status": "confirmed", "entry_mode": "weak_only",
         "confirm_time": "09:35:00", "strategy_version": "v1", "leader_score": 70}]}
    attach_evidence(payload)
    row = payload["rows"][0]
    ledger.evidence(row["signal_id"], row["signal_evidence"])
    case_id = row["case_id"]
    row.update(leader_score=99, confirm_time="10:00:00", strategy_id="another")
    attach_evidence(payload)
    assert row["case_id"] == case_id
    ledger.evidence(row["signal_id"], row["signal_evidence"])
    repo = SignalCaseRepository(tmp_path / "cases.duckdb")
    assert repo.import_staged(ledger.path, through_date="20260901") == 1
    assert repo.import_staged(ledger.path, through_date="20260901") == 0
    assert repo.get(case_id)["features"]["leader_score"] == 70
    assert repo.get(case_id)["confirm_at"].endswith("09:36:00")


def test_labels_are_availability_gated_and_mature_values_immutable(tmp_path):
    repo = SignalCaseRepository(tmp_path / "cases.duckdb")
    case = build_signal_case({"code": "000001", "entry_mode": "weak_only", "confirm_time": "09:35:00"},
        {"market_date": "20260901", "source_available_at": "2026-08-31T20:00:00", "pit_verified": True})
    repo.record(case)
    label = {"state": "mature", "value": .2, "label_end_at": "2026-09-02T15:00:00",
             "available_at": "2026-09-02T20:00:00"}
    repo.save_outcome(case["case_id"], "return_d1", label)
    assert not repo.outcomes(case["case_id"], as_of="2026-09-02T20:00:00")
    repo.save_outcome(case["case_id"], "return_d1", {**label, "value": 999})
    assert repo.outcomes(case["case_id"], as_of="2026-09-03T00:00:00")["return_d1"]["value"] == .2
    with pytest.raises(ValueError):
        repo.save_outcome(case["case_id"], "mae", {**label, "available_at": "2026-09-01T00:00:00"})


def test_partial_fills_and_costs_are_episode_level():
    fills = [{"side": "buy", "shares": 200, "price": 10, "commission": 1},
             {"side": "sell", "shares": 100, "price": 11, "commission": .5, "tax": .5}]
    assert closed_trade_result(fills)["state"] == "pending"
    fills += [{"side": "sell", "shares": 100, "price": 9, "commission": .5, "tax": .5}]
    result = closed_trade_result(fills)
    assert not result["success"]
    assert result["net_return"] == pytest.approx(-3 / 2001)
    assert result["fees"] == 3


def test_wrong_day_and_incomplete_minutes_are_not_no_opportunity():
    assert validate_minutes(bars("20260902"), "20260901")[1] == "wrong_minute_date"
    assert validate_minutes(bars().iloc[:50], "20260901", full_session=True)[1] == "incomplete_minute_session"


def test_offline_case_data_never_calls_remote_and_caches_reads(tmp_path):
    class DM:
        stock_dir = tmp_path
        def get_stock_tick(self, *_args):
            raise AssertionError("remote request")
        def get_auction_data(self, *_args):
            raise AssertionError("remote request")
    data = CaseDataSource(DM())
    assert data.get_stock_tick("000001.SZ", "20260901").empty
    assert data.get_auction_data("000001.SZ", "20260901") == {}
    assert len(data.frames) == 1


class DM:
    def __init__(self):
        self.missing = set()
    def get_stock_tick(self, code, date):
        return pd.DataFrame() if date in self.missing else bars(date)
    def get_auction_data(self, *_args):
        return {}
    def get_stock_daily_data(self, code, date):
        return {"open": 10, "high": 10, "low": 10, "close": 10, "pre_close": 10, "amount_yuan": 10000000}


def make_builder(tmp_path, monkeypatch, *, code="000001"):
    dm = DM()
    config = BacktestConfig(slippage=0, stop_loss_pct=.05, trailing_stop=False, time_stop_days=1,
                            time_stop_profit_threshold=.02, max_positions=0, commission_rate=.001, stamp_duty_rate=.001)
    repo = SignalCaseRepository(tmp_path / "cases.duckdb")
    builder = SignalCaseBuilder(dm, repository=repo, config=config, calendar=Calendar())
    monkeypatch.setattr(builder.scanner, "_load_sector_peer_codes", lambda *_args: {})
    def confirm(plan, date, stock_code, stock_name):
        decision = EntryDecision("filled", signal="弱转强", confirm_time="09:35:00", entry_time="09:36:00", entry_price=10)
        builder.scanner._record_entry_attempt(plan, date, stock_code, stock_name, decision, entry_mode="weak_only")
        builder.scanner._last_entry_meta[stock_code] = {"confirm_time": "09:35:00", "entry_time": "09:36:00"}
        builder.scanner._last_entry_signal[stock_code] = "弱转强"
        return True, 10
    monkeypatch.setattr(builder.scanner, "_check_buy_conditions", confirm)
    row = {"code": code, "name": "Test", "strategy_id": "weak_to_strong", "strategy_version": "v1"}
    return builder, dm, repo, row


def test_independent_cases_ignore_account_limits_resume_and_do_not_duplicate(tmp_path, monkeypatch):
    builder, dm, repo, row = make_builder(tmp_path, monkeypatch)
    assert builder.discover([row], candidate_date="20260831", market_date="20260901") == 1
    dm.missing.add("20260902")
    builder.refresh("20260903")
    assert repo.coverage()["progress"] == {"blocked": 1}
    with repo.connect() as con:
        case_id, state = con.execute("SELECT case_id, payload_json FROM signal_case_progress").fetchone()
    state = json.loads(state)
    assert state["last_processed"] == "20260901"
    assert state["engine"]["current_positions"]["000001"]["shares"] == 100
    dm.missing.clear()
    builder.refresh("20260908")
    result = repo.outcomes(case_id, as_of="2099-01-01T00:00:00", research=True)
    assert result["net_return"]["net_return"] == pytest.approx(-.003 / 1.001)
    assert result["net_return"]["exit_time"] == "14:56:00"
    assert result["return_d1"]["value"] == 0
    assert result["return_d5"]["value"] == 0
    assert repo.coverage()["progress"] == {"complete": 1}
    assert builder.discover([row], candidate_date="20260831", market_date="20260901") == 0
    assert not repo.outcomes(case_id, as_of="2099-01-01T00:00:00")  # Reconstructed research, not PIT proof.


def test_star_case_uses_minimum_200_shares(tmp_path, monkeypatch):
    builder, _, repo, row = make_builder(tmp_path, monkeypatch, code="688001")
    builder.discover([row], candidate_date="20260831", market_date="20260901")
    with repo.connect() as con:
        state = json.loads(con.execute("SELECT payload_json FROM signal_case_progress").fetchone()[0])
    assert state["engine"]["current_positions"]["688001"]["shares"] == 200


def test_pre_entry_and_post_exit_extremes_do_not_pollute_labels(tmp_path, monkeypatch):
    builder, dm, repo, row = make_builder(tmp_path, monkeypatch)
    original = dm.get_stock_tick
    def altered(code, date):
        frame = original(code, date)
        if date == "20260901":
            frame.loc[frame["time"] < "09:36:00", "high"] = 100
            frame.loc[frame["time"] < "09:36:00", "low"] = 1
        else:
            frame.loc[frame["time"] > "14:56:00", "high"] = 100
        return frame
    monkeypatch.setattr(dm, "get_stock_tick", altered)
    builder.discover([row], candidate_date="20260831", market_date="20260901")
    builder.refresh("20260902")
    with repo.connect() as con:
        case_id = con.execute("SELECT case_id FROM signal_cases").fetchone()[0]
    labels = repo.outcomes(case_id, as_of="2099-01-01T00:00:00", research=True)
    assert labels["mfe"]["value"] == 0
    assert labels["mae"]["value"] == 0
    assert labels["net_return"]["label_end_at"].startswith("2026-09-02")  # No T+0 exit.


def test_locked_limit_down_carries_exit_intent_to_next_tradable_day(tmp_path, monkeypatch):
    builder, dm, repo, row = make_builder(tmp_path, monkeypatch)
    def daily(code, date):
        price = 9 if date == "20260902" else 9.2 if date == "20260903" else 10
        return {"open": price, "close": price, "high": price, "low": price,
                "pre_close": 9 if date == "20260903" else 10}
    monkeypatch.setattr(dm, "get_stock_daily_data", daily)
    monkeypatch.setattr(dm, "get_stock_tick", lambda code, date: bars(date, daily(code, date)["close"]))
    builder.discover([row], candidate_date="20260831", market_date="20260901")
    builder.refresh("20260902")
    with repo.connect() as con:
        case_id, payload = con.execute("SELECT case_id, payload_json FROM signal_case_progress").fetchone()
    assert json.loads(payload)["engine"]["current_positions"]
    assert "net_return" not in repo.outcomes(case_id, as_of="2099-01-01", research=True)
    builder.refresh("20260903")
    result = repo.outcomes(case_id, as_of="2099-01-01", research=True)["net_return"]
    assert result["label_end_at"] == "2026-09-03T09:30:00"
    assert result["net_return"] < 0


def test_day_labels_and_checkpoint_roll_back_together(tmp_path, monkeypatch):
    builder, _, repo, row = make_builder(tmp_path, monkeypatch)
    builder.discover([row], candidate_date="20260831", market_date="20260901")
    builder.refresh("20260901")
    original = builder._advance
    def broken(engine, case, progress, date, con):
        original(engine, case, progress, date, con)
        raise RuntimeError("interrupted before checkpoint")
    monkeypatch.setattr(builder, "_advance", broken)
    builder.refresh("20260902")
    with repo.connect() as con:
        case_id, payload = con.execute("SELECT case_id, payload_json FROM signal_case_progress").fetchone()
    progress = json.loads(payload)
    assert progress["last_processed"] == "20260901"
    assert progress["engine"]["current_positions"]
    assert not repo.outcomes(case_id, as_of="2099-01-01", research=True)
    monkeypatch.setattr(builder, "_advance", original)
    builder.refresh("20260902")
    assert repo.outcomes(case_id, as_of="2099-01-01", research=True)["net_return"]["state"] == "mature"


def test_discovery_failure_blocks_only_one_stock_and_is_retryable(tmp_path, monkeypatch):
    builder, _, repo, row = make_builder(tmp_path, monkeypatch)
    original = builder._seed
    def broken(plan, case, price, scanner):
        if case["code"] == "000001":
            raise RuntimeError("missing case evidence")
        return original(plan, case, price, scanner)
    monkeypatch.setattr(builder, "_seed", broken)
    assert builder.discover([row, {**row, "code": "000002"}], candidate_date="20260831", market_date="20260901") == 1
    assert repo.coverage()["attempts"] == {"blocked": 1, "filled": 1}
    monkeypatch.setattr(builder, "_seed", original)
    assert builder.discover([row, {**row, "code": "000002"}], candidate_date="20260831", market_date="20260901") == 1
    with repo.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM signal_cases").fetchone()[0] == 2


def live_case(builder, *, code="000001", malformed=False):
    case = build_signal_case({"code": code, "name": "Test", "entry_mode": "weak_only",
        "confirm_status": "confirmed", "confirm_time": "09:35:00", "received_at": "09:36:02",
        "strategy_id": "weak_to_strong", "strategy_version": "frozen1",
        "strategy_execution": {"entry": {"mode": "weak_only"}}},
        {"market_date": "20260901", "candidate_date": "20260831"})
    case["backtest_config"] = {**asdict(builder.config), "commission_rate": .002,
                               "slippage": .001, "exit_policy_mode": "oos_selected"}
    case["resolved_exit_policy"] = {"policy": "fixed_stop"}
    if malformed:
        case["backtest_config"]["unknown_setting"] = True
    return case


def test_live_case_uses_frozen_cost_policy_and_next_observed_minute(tmp_path, monkeypatch):
    from backtest.exit_policy import ExitPolicyRepository
    builder, _, repo, _ = make_builder(tmp_path, monkeypatch)
    def forbid_today_policy(*_args):
        raise AssertionError("Must use frozen policy")
    monkeypatch.setattr(ExitPolicyRepository, "resolve", forbid_today_policy)
    case = live_case(builder)
    repo.record(case)
    assert builder.initialize_live("20260901") == 1
    assert builder.initialize_live("20260901") == 0
    with repo.connect() as con:
        progress = json.loads(con.execute("SELECT payload_json FROM signal_case_progress").fetchone()[0])
    assert progress["entry_time"] == "09:37:00"
    assert progress["entry_price"] == pytest.approx(10.01)
    assert progress["fills"][0]["commission"] == pytest.approx(2.002)
    assert progress["engine"]["current_positions"]["000001"]["exit_config"]["policy"] == "fixed_stop"


def test_live_case_missing_minutes_retries_without_reset(tmp_path, monkeypatch):
    builder, dm, repo, _ = make_builder(tmp_path, monkeypatch)
    repo.record(live_case(builder))
    dm.missing.add("20260901")
    assert builder.initialize_live("20260901") == 0
    assert repo.coverage()["progress"] == {"initialization_blocked": 1}
    dm.missing.clear()
    assert builder.initialize_live("20260901") == 1
    assert repo.coverage()["progress"] == {"pending": 1}


def test_live_case_bad_configuration_does_not_block_other_stocks(tmp_path, monkeypatch):
    builder, _, repo, _ = make_builder(tmp_path, monkeypatch)
    repo.record(live_case(builder, malformed=True))
    repo.record(live_case(builder, code="000002"))
    assert builder.initialize_live("20260901") == 1
    assert repo.coverage()["progress"] == {"initialization_blocked": 1, "pending": 1}


@pytest.mark.parametrize("strategy", ["mainline_leader", "weak_to_strong", "first_board_launch", "limit_pullback", "limit_reversal", "leader_pool"])
def test_all_strategy_sources_preserved(tmp_path, monkeypatch, strategy):
    builder, _, repo, row = make_builder(tmp_path, monkeypatch)
    row["strategy_id"] = strategy
    builder.discover([row], candidate_date="20260831", market_date="20260901")
    with repo.connect() as con:
        case = json.loads(con.execute("SELECT payload_json FROM signal_cases").fetchone()[0])
    assert case["strategy_id"] == strategy
