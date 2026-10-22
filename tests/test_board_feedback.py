from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import EntryDecision
from backtest.trade_calendar import TradeCalendar
from core.portfolio.exit_decision_service import ExitDecisionService
from core.realtime.board_feedback_repository import BoardFeedbackRepository, feedback_inputs
from core.realtime.day_replay import _ReplayEntryService, replay_minutes, replay_saved_inputs
from core.signals.board_feedback import assess_feedback, feedback_policy, feedback_score_adjustment, history_dates, rebound_exit
from core.signals.production_rules import candidate_strength, minute_evaluator
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
def test_cuhua_negative_factor_is_d5_d6_not_d4_or_d7(day, active, board_day):
    result = context(day)
    assert result["assessed"]
    assert result["active"] is active
    assert result.get("board_day") == board_day
    assert result["factor_value"] == int(active)
    assert not result["entry_veto"]
    if active:
        assert result["third_board_date"] == "20261014"
        assert result["feedback_date"] == "20261015"
        assert result["feedback_change_pct"] == -10


def test_future_d4_close_cannot_change_d4_factor():
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
    assert result["factor_value"] is None
    assert result["score_adjustment"] == 0


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
@pytest.mark.parametrize("known", [True, False])
@pytest.mark.parametrize("source", ["minute", "snapshot"])
def test_optional_factor_never_replaces_minute_or_snapshot_confirmation(gap, known, source):
    evaluator = minute_evaluator(RiskConfig())
    calls = []
    def confirmed(**kwargs):
        calls.append(source)
        return EntryDecision("confirmed", reason=f"{source} confirmed", confirm_time="09:39:00")
    evaluator.evaluate = confirmed
    service = _ReplayEntryService({}, {}, {}, {}, profiles(), evaluator=evaluator,
                                  board_feedback_inputs=inputs() if known else {})
    service.snapshot_primary_enabled = source == "snapshot"
    service._snapshot_frame = lambda *a: pd.DataFrame([{"time": "09:39:58"}])
    service.snapshot_signals = SimpleNamespace(evaluate_primary=lambda **kw: (confirmed(**kw), ""))
    signal = service._evaluate_one({"code": CODE, "name": "Cuhua", "score": 100},
        quote(open_price=14.22 * (1 + gap)), pd.DataFrame(), {}, {}, {"close": 14.22},
        "20261016", [], {})
    assert calls == [source]
    assert signal["signal_status"] == "confirmed"
    assert signal["reason"] == f"{source} confirmed"
    assert signal["data_status"] == "complete"
    assert signal["board_feedback"]["active"] is known
    assert signal["board_feedback"]["assessed"] is known
    assert not signal.get("entry_veto_reason")
    assert signal["confirm_time"] == "09:39:00"


def test_missing_history_retains_the_real_entry_reason():
    service = _ReplayEntryService({}, {}, {}, {}, profiles(), evaluator=minute_evaluator(RiskConfig()))
    signal = service._evaluate_one({"code": CODE}, quote(), pd.DataFrame(), {}, {},
                                   {"close": 14.22}, "20261016", [], {})
    assert signal["data_status"] != "missing_board_feedback"
    assert signal["signal_status"] == "observe"
    assert signal["reason"] == "等待当日一分钟行情"
    assert signal["board_feedback"]["factor_status"] == "unassessed"


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
    assert report["stocks"][0]["status"] == "observe"
    assert report["stocks"][0]["board_feedback"]["active"]
    assert report["confirmed_stocks"] == 0
    assert report["complete"]
    monkeypatch.setattr(BoardFeedbackRepository, "capture", lambda *a, **kw: pytest.fail("saved inputs must not read live data"))
    monkeypatch.setattr(RiskConfig, "load", lambda: pytest.fail("saved policy must not use current config"))
    replayed = replay_saved_inputs(report["replay_inputs"])
    assert replayed["input_hash"] == report["input_hash"]
    assert replayed["stocks"][0]["reason"] == report["stocks"][0]["reason"]
    missing = replay_minutes(**kwargs, board_feedback_inputs={})
    assert missing["complete"]
    assert missing["failures"] == []
    assert missing["factor_warnings"][0]["status"] == "unassessed"
    assert missing["stocks"][0]["reason"] == report["stocks"][0]["reason"]


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


@pytest.mark.parametrize("known", [True, False])
def test_backtest_factor_does_not_veto_a_valid_buy(known):
    result = engine()
    result._board_feedback_repository = SimpleNamespace(capture=lambda *a: inputs() if known else {})
    result.dm = SimpleNamespace(get_stock_daily_data=lambda *a: {"open": 13.8, "pre_close": 14.22})
    result._is_full_day_suspended = lambda *a, **kw: False
    result._get_stock_daily_bar = lambda *a: {"close": 14.22, "amount": 1000}
    result._entry_mode_for_plan = lambda *a: "weak_only"
    result.minute_evaluator.evaluate = lambda **kw: EntryDecision("filled", reason="minute confirmed",
        signal="弱转强", confirm_time="09:39:00", entry_time="09:40:00", entry_price=14.08)
    allowed, price = result._check_buy_conditions(pd.Series({"目标价": 0, "综合评分": 100}), "20261016", CODE, "Cuhua")
    assert allowed and price > 0
    attempt = result.entry_attempts[-1]
    assert attempt["board_feedback"]["active"] is known
    assert attempt["board_feedback"]["assessed"] is known
    assert attempt["reason_code"] not in {"three_board_feedback_veto", "missing_board_feedback"}


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
    risk = RiskConfig(three_board_feedback_limit_fraction=.7, three_board_rebound_exit_gain=.03,
                      three_board_feedback_score_penalty=20)
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
    assert {"three_board_feedback_enabled", "three_board_feedback_limit_fraction", "three_board_rebound_exit_gain",
            "three_board_feedback_score_penalty"} <= paths
    monkeypatch.setattr(registry, "_index_fields", lambda: {
        ("risk", "three_board_feedback_limit_fraction"): {"type": "float", "default": .5}})
    monkeypatch.setattr(registry, "_reapply_live", lambda: None)
    monkeypatch.setattr(registry.ov, "set_override", lambda *a: pytest.fail("invalid values must not persist"))
    for value in (0, -1, 1.01, "nan", "inf"):
        result = registry.apply_updates([{"scope": "risk", "path": "three_board_feedback_limit_fraction", "value": value}])
        assert result["errors"]


def test_overlay_and_leader_rows_keep_factor_separate_from_entry_evidence(monkeypatch):
    from core.realtime.overlay_service import RealtimeOverlayService
    from core.realtime.leader_pool_service import IntradayStrengthService
    from core.operations import evidence as evidence_module
    monkeypatch.setattr(evidence_module, "_fetch_time", lambda *a: "")
    signal = {"signal_status": "confirmed", "board_feedback": context(), "reason": "minute confirmed",
              "data_status": "complete"}
    candidate = {"code": CODE, "leader_score": 100}
    overlay = RealtimeOverlayService.__new__(RealtimeOverlayService)._build_row(
        "20261015", "20261016", candidate, quote(), signal)
    leader = IntradayStrengthService.__new__(IntradayStrengthService)._build_row(candidate, quote(), signal)
    for row in (overlay, leader):
        assert row["board_feedback"] == context()
        assert row["data_status"] == "complete"
        result = evidence_module.attach_evidence({"rows": [row]})["rows"][0]["signal_evidence"]
        assert result["veto_reason"] == ""
        assert result["board_feedback"] == context()


def test_feedback_penalty_is_shared_and_not_applied_twice():
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.realtime.leader_pool_service import IntradayStrengthService
    policy = feedback_policy(RiskConfig(three_board_feedback_score_penalty=20))
    factor = assess_feedback(inputs(), CODE, "20261016", policy)
    assert feedback_score_adjustment(factor) == -20
    assert factor["score_adjustment"] == -20
    assert candidate_strength({"score": 80, "board_feedback": factor}) == 60
    unknown = assess_feedback({}, CODE, "20261016", policy)
    assert candidate_strength({"score": 80, "board_feedback": unknown}) == 80
    service = IntradayStrengthService.__new__(IntradayStrengthService)
    plain = service._build_row({"code": CODE, "leader_score": 80}, quote(), {"signal_status": "confirmed"})
    ranked = service._build_row({"code": CODE, "leader_score": 80}, quote(),
                               {"signal_status": "confirmed", "board_feedback": factor})
    assert ranked["turn_score"] == pytest.approx(plain["turn_score"] - 20)
    assert candidate_strength(ranked) == pytest.approx(candidate_strength(plain) - 20)
    assert PaperTradingService._candidate_strength(ranked) == candidate_strength(ranked)
    assert candidate_strength({"score": 10, "board_feedback": factor}) == 0
    assert ranked["confirm_status"] == "confirmed"


def test_production_backtest_uses_the_same_factor_deduction_as_live():
    result = BacktestEngine(None, BacktestConfig(production_rules=True, three_board_feedback_score_penalty=20))
    result._production_inputs = {"board_feedback_inputs": inputs(), "opening_prices": {CODE: 13.8},
        "opening_evidence": {CODE: {"pre_close": 14.22}}, "previous": {CODE: {"close": 14.22}},
        "members": {}, "frames": {}}
    result._production_rows = {CODE: {"code": CODE, "score": 80}}
    result._is_full_day_suspended = lambda *a, **kw: False
    result._entry_mode_for_plan = lambda *a: "weak_only"
    result.minute_evaluator.evaluate = lambda **kw: EntryDecision("filled", reason="minute confirmed",
        signal="弱转强", confirm_time="09:39:00", entry_time="09:40:00", entry_price=14.08)
    allowed, _ = result._check_buy_conditions(pd.Series({"目标价": 0, "综合评分": 80}), "20261016", CODE, "Cuhua")
    assert allowed
    assert result._last_entry_meta[CODE]["entry_strength_score"] == 60


def test_legacy_frozen_config_does_not_invent_a_new_factor_weight():
    assert RiskConfig().three_board_feedback_score_penalty == 20
    old = RiskConfig.from_dict({"three_board_feedback_enabled": True})
    assert old.three_board_feedback_score_penalty == 0


def test_zero_factor_penalty_is_editable_without_turning_off_the_factor(monkeypatch):
    from config import config_registry as registry
    saved = []
    monkeypatch.setattr(registry, "_index_fields", lambda: {
        ("risk", "three_board_feedback_score_penalty"): {"type": "float", "default": 20.0}})
    monkeypatch.setattr(registry, "_reapply_live", lambda: None)
    monkeypatch.setattr(registry.ov, "set_override", lambda scope, path, value: saved.append(value))
    result = registry.apply_updates([{"scope": "risk", "path": "three_board_feedback_score_penalty", "value": 0}])
    assert result["applied"] and not result["errors"]
    assert saved == [0.0]


@pytest.mark.parametrize("penalty", [-1, 100.1, "nan", "inf", True, False])
def test_factor_weight_rejects_nonfinite_and_out_of_range_values(penalty):
    with pytest.raises(ValueError, match="扣分"):
        feedback_policy({"three_board_feedback_score_penalty": penalty})


def test_optional_history_failure_cannot_become_an_entry_gate(tmp_path):
    broken = SimpleNamespace(prev=lambda day: (_ for _ in ()).throw(OSError("calendar unavailable")))
    captured = BoardFeedbackRepository(tmp_path, calendar=broken).capture("20261016", [CODE])
    factor = assess_feedback(captured, CODE, "20261016", POLICY)
    assert factor["factor_status"] == "unassessed"
    assert factor["factor_value"] is None
    assert feedback_score_adjustment(factor) == 0


@pytest.mark.parametrize("bad", ["bad", [1], {"dates": [[1]]}, {"stocks": "bad"}])
def test_malformed_optional_factor_inputs_stay_unknown_and_neutral(bad):
    result = assess_feedback(bad, CODE, "20261016", POLICY)
    assert result["factor_value"] is None
    assert not result["entry_veto"]
    assert feedback_score_adjustment(result) == 0
    assert feedback_score_adjustment(bad) == 0


def test_factor_never_bypasses_real_quote_freshness():
    service = _ReplayEntryService({}, {}, {}, {}, profiles(), evaluator=minute_evaluator(RiskConfig()),
                                  board_feedback_inputs=inputs())
    signal = service._evaluate_one({"code": CODE}, quote(is_stale=True), pd.DataFrame(), {}, {},
                                   {"close": 14.22}, "20261016", [], {})
    assert signal["data_status"] == "quote_stale"
    assert signal["board_feedback"]["active"]
    assert signal["signal_status"] == "observe"


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
