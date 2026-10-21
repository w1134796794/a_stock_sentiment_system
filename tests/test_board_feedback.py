from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.trade_calendar import TradeCalendar
from core.portfolio.exit_decision_service import ExitDecisionService
from core.realtime.board_feedback_repository import BoardFeedbackRepository, feedback_inputs
from core.realtime.day_replay import _ReplayEntryService, replay_minutes, replay_saved_inputs
from core.signals.board_feedback import assess_feedback, feedback_policy, history_dates, rebound_exit
from core.signals.production_rules import minute_evaluator
from core.utils.price_limit import get_price_limit_pct, round_price
from risk.risk_config import RiskConfig


CODE = "603968"
POLICY = feedback_policy(RiskConfig())
CALENDAR = TradeCalendar()
DAILY = [
    ("20261008", 12.30, 11.50), ("20261009", 11.86, 12.30),
    ("20261012", 13.05, 11.86), ("20261013", 14.36, 13.05),
    ("20261014", 15.80, 14.36), ("20261015", 14.22, 15.80),
    ("20261016", 13.35, 14.22), ("20261019", 12.59, 13.35),
]


def inputs(day="20261016", *, code=CODE, records=None):
    records = records if records is not None else [
        {"code": code, "trade_date": date, "close": close, "pre_close": previous}
        for date, close, previous in DAILY]
    return feedback_inputs(day, [code], history_dates(day, CALENDAR), records,
                           source_basis="test_frozen", calendar_verified=True)


def context(day="20261016", **kwargs):
    return assess_feedback(inputs(day, **kwargs), kwargs.get("code", CODE), day, POLICY)


def position(**overrides):
    return {"code": CODE, "entry_date": "20261015", "entry_price": 14.30,
            "shares": 100, "high_watermark": 14.30, "sellable_shares": 100,
            "board_feedback": context(), **overrides}


def quote(**overrides):
    return {"code": CODE, "date": "20261016", "time": "09:40:00", "is_stale": False,
            "pre_close": 14.22, "open_price": 13.80, "last_price": 14.08,
            "high_price": 14.08, "change_pct": -1.0, **overrides}


@pytest.mark.parametrize("day,active,board_day", [
    ("20261015", False, None), ("20261016", True, 5),
    ("20261019", True, 6), ("20261020", False, None),
])
def test_cuhua_veto_is_d5_d6_not_d4_or_d7(day, active, board_day):
    result = context(day)
    assert result["assessed"]
    assert result["active"] is active
    assert result.get("board_day") == board_day
    if active:
        assert result["third_board_date"] == "20261014"
        assert result["feedback_date"] == "20261015"
        assert result["feedback_change_pct"] == -10


def test_future_d4_close_cannot_veto_d4_entry():
    captured = inputs("20261015")
    captured["stocks"][CODE].extend(inputs()["stocks"][CODE])
    assert not assess_feedback(captured, CODE, "20261015", POLICY)["active"]


@pytest.mark.parametrize("code", ["603968", "300001", "688001", "920001"])
def test_feedback_threshold_scales_with_the_stock_limit(code):
    limit = get_price_limit_pct(code)
    rows = [{"code": code, "trade_date": "20261009", "close": 10, "pre_close": 10.2}]
    price = 10
    for day in ("20261012", "20261013", "20261014"):
        close = round_price(price * (1 + limit))
        rows.append({"code": code, "trade_date": day, "close": close, "pre_close": price})
        price = close
    rows.append({"code": code, "trade_date": "20261015", "close": price * (1 - limit * .5), "pre_close": price})
    assert context(code=code, records=rows)["active"]
    rows[-1]["close"] = price * (1 - limit * .5 + .0001)
    assert not context(code=code, records=rows)["active"]


@pytest.mark.parametrize("day,close,previous", [
    ("20261013", 14.0, 13.05),  # Only two sealed limit-up closes.
    ("20261009", 11.86, round_price(11.86 / 1.1)),  # A four-board, not three-board, run.
])
def test_non_three_board_streak_does_not_trigger(day, close, previous):
    records = deepcopy(inputs()["stocks"][CODE])
    next(row for row in records if row["trade_date"] == day).update(close=close, pre_close=previous)
    assert not context(records=records)["active"]


@pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), True])
def test_invalid_failure_price_is_unknown_not_passed(bad):
    captured = inputs()
    next(row for row in captured["stocks"][CODE] if row["trade_date"] == "20261015")["close"] = bad
    result = assess_feedback(captured, CODE, "20261016", POLICY)
    assert not result["assessed"]
    assert not result["active"]


def test_duplicate_conflict_and_wrong_snapshot_date_are_unknown():
    captured = inputs()
    conflict = dict(next(row for row in captured["stocks"][CODE] if row["trade_date"] == "20261015"), close=15.8)
    captured["stocks"][CODE].append(conflict)
    assert not assess_feedback(captured, CODE, "20261016", POLICY)["assessed"]
    assert not assess_feedback(inputs("20261019"), CODE, "20261016", POLICY)["assessed"]
    captured = inputs()
    captured["calendar_verified"] = False
    assert not assess_feedback(captured, CODE, "20261016", POLICY)["assessed"]


def test_disabled_policy_does_not_require_history():
    assert assess_feedback({}, CODE, "20261016", feedback_policy())["assessed"]


def test_known_non_limit_closes_can_disprove_an_episode_without_unrelated_daily_rows():
    captured = inputs("20261020")
    captured["stocks"][CODE] = [row for row in captured["stocks"][CODE] if row["trade_date"] != "20261019"]
    result = assess_feedback(captured, CODE, "20261020", POLICY)
    assert result["assessed"]
    assert not result["active"]


def test_repository_never_reads_local_history_for_a_saved_bundle(tmp_path):
    repository = BoardFeedbackRepository(tmp_path, db_path=tmp_path / "missing.db")
    frozen = {"board_feedback_inputs": inputs()}
    captured = repository.capture("20261016", [CODE], frozen_inputs=frozen)
    assert context() == assess_feedback(captured, CODE, "20261016", POLICY)
    assert repository._cached is None
    missing = repository.capture("20261016", [CODE], frozen_inputs={})
    assert not assess_feedback(missing, CODE, "20261016", POLICY)["assessed"]


def test_repository_uses_only_prior_partitions_and_reloads_repaired_inputs(tmp_path):
    folder = tmp_path / "stock" / "all_daily"
    folder.mkdir(parents=True)
    rows = inputs()["stocks"][CODE]
    for row in rows:
        if row["trade_date"] != "20261015":
            pd.DataFrame([row]).to_csv(folder / f"{row['trade_date']}.csv", index=False)
    repository = BoardFeedbackRepository(tmp_path, db_path=tmp_path / "missing.db")
    assert not assess_feedback(repository.capture("20261016", [CODE]), CODE, "20261016", POLICY)["assessed"]
    row = next(row for row in rows if row["trade_date"] == "20261015")
    pd.DataFrame([row]).to_csv(folder / "20261015.csv", index=False)
    assert assess_feedback(repository.capture("20261016", [CODE]), CODE, "20261016", POLICY)["active"]
    assert not (folder / "20261016.csv").exists()


def profiles():
    return SimpleNamespace(available=False, load=lambda: {}, expected_fraction=lambda *a: (None, 0))


@pytest.mark.parametrize("gap", [-.01, .02, .06])
def test_live_veto_precedes_all_entry_modes_and_snapshot_confirmation(gap):
    evaluator = minute_evaluator(RiskConfig())
    evaluator.evaluate = lambda **kwargs: pytest.fail("veto must run before minute confirmation")
    service = _ReplayEntryService({}, {}, {}, {}, profiles(), evaluator=evaluator,
                                  board_feedback_inputs=inputs())
    signal = service._evaluate_one({"code": CODE, "name": "Cuhua", "score": 100},
        quote(open_price=14.22 * (1 + gap)), pd.DataFrame(), {}, {}, {"close": 14.22},
        "20261016", [], {})
    assert signal["signal_status"] == "cancelled"
    assert signal["data_status"] == "three_board_feedback_veto"
    assert signal["board_feedback"]["active"]
    assert not signal["confirm_time"]


def test_missing_history_cannot_confirm_production_entry():
    service = _ReplayEntryService({}, {}, {}, {}, profiles(), evaluator=minute_evaluator(RiskConfig()))
    signal = service._evaluate_one({"code": CODE}, quote(), pd.DataFrame(), {}, {},
                                   {"close": 14.22}, "20261016", [], {})
    assert signal["data_status"] == "missing_board_feedback"
    assert signal["signal_status"] == "observe"


def minute_bars():
    return pd.DataFrame([{"code": CODE, "trade_date": "20261016", "time": f"09:{n:02d}:00",
                         "open": 14.1, "high": 14.1, "low": 14.1, "close": 14.1,
                         "volume": 100, "amount": 141000} for n in range(31, 34)])


def test_replay_freezes_feedback_history_policy_and_missing_evidence(monkeypatch):
    kwargs = dict(request={"market_date": "20261016", "until": "09:34:00", "profile": "production"},
        candidate_date="20261015", rows=[{"code": CODE}], frames={CODE: minute_bars()},
        previous={CODE: {"close": 14.22}}, opening_prices={CODE: 13.8}, profiles=profiles(),
        risk_config=RiskConfig().to_dict())
    report = replay_minutes(**kwargs, board_feedback_inputs=inputs())
    assert report["stocks"][0]["status"] == "cancelled"
    assert report["confirmed_stocks"] == 0
    assert report["complete"]
    monkeypatch.setattr(BoardFeedbackRepository, "capture", lambda *a, **kw: pytest.fail("saved inputs must not read live data"))
    monkeypatch.setattr(RiskConfig, "load", lambda: pytest.fail("saved policy must not use current config"))
    replayed = replay_saved_inputs(report["replay_inputs"])
    assert replayed["input_hash"] == report["input_hash"]
    assert replayed["stocks"][0]["reason"] == report["stocks"][0]["reason"]
    missing = replay_minutes(**kwargs, board_feedback_inputs={})
    assert not missing["complete"]
    assert any(item["evidence"] == "missing_board_feedback" for item in missing["failures"])


@pytest.mark.parametrize("last,triggered", [(14.07, False), (14.08, True), (14.3, True)])
def test_d5_rebound_exits_at_current_price_not_a_future_high(last, triggered):
    result = rebound_exit(context(), position(), quote(last_price=last, high_price=15.15), "20261016")
    assert bool(result) is triggered
    if result:
        assert result["target_price"] == 14.08


@pytest.mark.parametrize("patch", [
    {"is_stale": True}, {"date": "20261015"}, {"open_price": 0},
    {"last_price": float("nan")}, {"quality_ok": False}, {"is_suspended": True},
])
def test_invalid_quotes_cannot_generate_rebound_exit(patch):
    assert not rebound_exit(context(), position(), quote(**patch), "20261016")


def test_rebound_exit_only_applies_to_d4_entries_on_d5_and_respects_sellability():
    service = ExitDecisionService()
    sold = service.evaluate(position(), quote(), signal_date="20261016")
    assert sold.action == "sell"
    assert sold.evidence["board_feedback_rebound"]["target_price"] == 14.08
    blocked = service.evaluate(position(sellable_shares=0), quote(), signal_date="20261016")
    assert blocked.action == "blocked"
    assert not rebound_exit(context(), position(entry_date="20261014"), quote(), "20261016")
    assert not rebound_exit(context("20261019"), position(), quote(date="20261019"), "20261019")


def test_exit_hard_stop_remains_a_reason_even_before_rebound():
    decision = ExitDecisionService().evaluate(position(), quote(last_price=13.0), signal_date="20261016")
    assert decision.action == "sell"
    assert not decision.evidence["board_feedback_rebound"]
    assert any("风险底线" in item for item in decision.reasons)


def engine():
    result = BacktestEngine(None, BacktestConfig(three_board_feedback_enabled=True))
    result._board_feedback_repository = SimpleNamespace(capture=lambda day, codes: inputs(day))
    return result


def test_backtest_records_the_same_buy_veto_as_live():
    result = engine()
    assert result._check_buy_conditions(pd.Series({"目标价": 0, "综合评分": 100}), "20261016", CODE, "Cuhua") == (False, 0)
    attempt = result.entry_attempts[-1]
    assert attempt["reason_code"] == "three_board_feedback_veto"
    assert attempt["board_feedback"] == context()


@pytest.mark.parametrize("hard_stop", [False, True])
def test_backtest_rebound_is_chronological_and_hard_stop_has_priority(hard_stop):
    result = engine()
    result.config.trailing_stop_pct = 0
    result._load_exit_minute_bars = lambda *a: pd.DataFrame([
        {"time": "09:31:00", "open": 13.8, "high": 15.15, "low": 12.9 if hard_stop else 13.8, "close": 14.07},
        {"time": "09:32:00", "open": 14.07, "high": 14.10, "low": 14.07, "close": 14.08}])
    held = {**position(), "stop_loss_price": 13.0, "highest_price": 14.30}
    decision = result._minute_exit_decision(CODE, held, "20261016", {"open": 13.8, "pre_close": 14.22})
    assert decision["sell"]
    assert decision["reason"] == ("stop_loss_minute" if hard_stop else "three_board_feedback_rebound")
    assert decision["time"] == ("09:31:00" if hard_stop else "09:32:00")
    assert decision["price"] == (13.0 if hard_stop else 14.08)


def test_risk_projection_keeps_feedback_thresholds_in_sync():
    risk = RiskConfig(three_board_feedback_limit_fraction=.7, three_board_rebound_exit_gain=.03)
    projected = BacktestConfig.from_risk_config(risk)
    assert feedback_policy(projected) == minute_evaluator(risk).board_feedback_policy == feedback_policy(risk)
    assert not feedback_policy(BacktestConfig())["enabled"]
    assert feedback_policy(BacktestConfig(production_rules=True))["enabled"]
    assert not feedback_policy(BacktestConfig(production_rules=True, three_board_feedback_enabled=False))["enabled"]


def test_missing_exit_minutes_are_audited_not_replaced_with_daily_high():
    result = engine()
    result.config.exit_minute_data_policy = "daily_only"
    decision = result._minute_exit_decision(CODE, position(), "20261016", {"open": 13.8, "high": 15.15})
    assert decision is None
    assert any(row["evidence"] == "board_feedback_exit_minutes" for row in result._exit_evidence_failures)


def test_config_controls_expose_policy_and_reject_invalid_thresholds(monkeypatch):
    from config import config_registry as registry
    paths = {field["path"] for section in registry.build_simple_registry()["sections"]
             for group in section["groups"] for field in group["fields"]}
    assert {"three_board_feedback_enabled", "three_board_feedback_limit_fraction", "three_board_rebound_exit_gain"} <= paths
    monkeypatch.setattr(registry, "_index_fields", lambda: {
        ("risk", "three_board_feedback_limit_fraction"): {"type": "float", "default": .5}})
    monkeypatch.setattr(registry, "_reapply_live", lambda: None)
    monkeypatch.setattr(registry.ov, "set_override", lambda *a: pytest.fail("invalid values must not persist"))
    for value in (0, -1, 1.01, "nan", "inf"):
        result = registry.apply_updates([{"scope": "risk", "path": "three_board_feedback_limit_fraction", "value": value}])
        assert result["errors"]


def test_overlay_and_leader_rows_preserve_the_veto_evidence(monkeypatch):
    from core.realtime.overlay_service import RealtimeOverlayService
    from core.realtime.leader_pool_service import IntradayStrengthService
    from core.operations import evidence as evidence_module
    monkeypatch.setattr(evidence_module, "_fetch_time", lambda *a: "")
    signal = {"signal_status": "cancelled", "board_feedback": context(), "reason": context()["reason"],
              "entry_veto_reason": context()["reason"], "data_status": "three_board_feedback_veto"}
    candidate = {"code": CODE, "leader_score": 100}
    overlay = RealtimeOverlayService.__new__(RealtimeOverlayService)._build_row(
        "20261015", "20261016", candidate, quote(), signal)
    leader = IntradayStrengthService.__new__(IntradayStrengthService)._build_row(candidate, quote(), signal)
    for row in (overlay, leader):
        assert row["board_feedback"] == context()
        assert row["data_status"] == "three_board_feedback_veto"
        result = evidence_module.attach_evidence({"rows": [row]})["rows"][0]["signal_evidence"]
        assert result["veto_reason"] == context()["reason"]
        assert result["board_feedback"] == context()


def test_closed_trade_keeps_rebound_exit_evidence():
    result = engine()
    result._is_full_day_suspended = lambda *a, **kw: False
    facts = {"context": context(), "trigger": {"target_price": 14.08}, "time": "09:32:00"}
    result.current_positions[CODE] = {**position(), "stock_name": "Cuhua", "pattern_type": "weak_only",
                                      "cost_basis": 1430, "hot_resonance": False, "resonance_sectors": "",
                                      "board_feedback_exit": facts}
    result._execute_sell(CODE, 14.08, "20261016", "three_board_feedback_rebound", exit_time="09:32:00")
    assert json.loads(result.trade_history[-1].exit_evidence_json) == facts


def test_position_monitor_executes_only_the_test_account_rebound_exit(tmp_path):
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.position_monitor import PositionMonitor
    repository = HoldingRepository(tmp_path / "holdings.sqlite")
    held = repository.open_position({"code": CODE, "name": "Cuhua", "entry_date": "20261015",
                                    "entry_time": "09:40:00", "entry_price": 14.30, "shares": 100})
    monitor = PositionMonitor(repository, quote_service=SimpleNamespace(get_quotes=lambda codes: {"quotes": [quote()]}),
        market_context_provider=lambda: {"market_score": 70},
        notifier=SimpleNamespace(notify_exit_signal=lambda *a, **kw: {"sent": 0, "all_delivered": False}),
        backend=MemoryStateBackend("board-feedback" + str(tmp_path)),
        board_feedback_repository=SimpleNamespace(capture=lambda *a, **kw: inputs()))
    result = monitor.run_once(signal_date="20261016", auto_execute=True)
    assert result["ok"] and result["executed"] == 1
    assert result["decisions"][0]["decision"]["evidence"]["board_feedback"]["active"]
    assert repository.get_position(held["id"])["status"] == "closed"
