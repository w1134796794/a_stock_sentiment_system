"""Event anchors and live/backtest causal confirmation contracts."""
import json

import duckdb
import pandas as pd
import pytest

from backtest.minute_entry import MinuteEntryEvaluator, normalize_minute_bars
from core.factors.jobs.reversal_factor_job import build_reversal_factors, run_reversal_factors
from core.screening.strategy_profiles import _execution_config


def bars():
    prices = [10.02, 10.03, 10.05, 10.07, 10.09, 10.15, 10.16]
    return pd.DataFrame([{
        "time": f"13:{i:02d}:00", "open": p, "close": p,
        "high": p+0.01, "low": p-0.02, "volume": 2000 if i == 5 else 1000,
    } for i, p in enumerate(prices)])


def structure():
    return {"support": 10.0, "support_low": 9.9, "support_high": 10.1,
            "target": 10.0, "protection": 9.8, "reference_close": 10.0,
            "atr": 0.5, "resistance": 11.0}


def evaluate(mode, frame=None, **kwargs):
    args = dict(mode=mode, bars=bars() if frame is None else frame,
                prev_close=10.0, open_gap=0.002, structure=structure(),
                confirmation_deadline="14:30:00", sector_sync=lambda _: True)
    args.update(kwargs)
    return MinuteEntryEvaluator().evaluate(**args)


@pytest.mark.parametrize("mode", ["limit_pullback", "limit_reversal"])
def test_afternoon_confirmation_has_no_future_fill(mode):
    pending = evaluate(mode, bars().iloc[:6], live=True)
    assert pending.status == "confirmed"
    assert pending.confirm_time == "13:05:00"
    assert pending.entry_price == 0
    filled = evaluate(mode)
    assert filled.filled and filled.entry_time == "13:06:00"
    assert filled.entry_price == 10.16


@pytest.mark.parametrize("mode", ["limit_pullback", "limit_reversal"])
def test_structure_failures_do_not_create_buy(mode):
    assert not evaluate(mode, sector_sync=lambda _: None).filled
    assert not evaluate(mode, structure={}).filled
    assert not evaluate(mode, prev_close=8.0).filled
    assert not evaluate(mode, confirmation_deadline="13:04:00").filled
    frame = bars()
    frame.loc[1, "close"] = 9.5
    assert evaluate(mode, frame).status == "cancelled"
    frame = bars()
    frame.loc[6, "time"] = "13:08:00"
    assert evaluate(mode, frame).status == "signal_unfilled"
    assert evaluate(mode, limit_price=10.16).status == "signal_unfilled"


def test_minute_sessions_and_deadline_config():
    frame = bars()
    frame.loc[0, "time"] = "12:00:00"
    assert "12:00:00" not in normalize_minute_bars(frame).time.tolist()
    assert _execution_config({"allowed_entry_modes": ["limit_pullback"],
                              "confirmation_deadline": "14:30"})["confirmation_deadline"] == "14:30:00"
    with pytest.raises(ValueError):
        _execution_config({"confirmation_deadline": "12:00"})


def daily():
    dates = pd.bdate_range("2026-07-01", periods=30).strftime("%Y%m%d")
    rows = []
    previous = 10.0
    for i, date in enumerate(dates):
        close = 11.0 if i == 25 else (10.1 if i > 25 else 10.0)
        rows.append(dict(trade_date=date, code="600001", name="测试股份", open=previous,
                         high=max(previous, close)+0.02, low=min(previous, close)-0.02,
                         close=close, pre_close=previous, vol_hand=2000 if i == 25 else 800))
        previous = close
    return pd.DataFrame(rows)


def test_event_factor_is_causal_and_persists():
    h = daily()
    date = h.iloc[-1].trade_date
    frame = build_reversal_factors(h, date)
    assert frame.iloc[0].stk_limit_pullback == 100
    anchor = json.loads(frame.iloc[0].reversal_structures)["limit_pullback"]
    assert anchor["event_date"] == h.iloc[25].trade_date
    tr = pd.concat([h.high - h.low, (h.high - h.pre_close).abs(),
                    (h.low - h.pre_close).abs()], axis=1).max(axis=1)
    assert anchor["atr"] == pytest.approx(tr.iloc[12:26].mean())
    future = h.iloc[-1:].assign(trade_date="20990101", close=100)
    pd.testing.assert_frame_equal(frame, build_reversal_factors(pd.concat([h, future]), date))
    with duckdb.connect() as con:
        con.register("daily_input", h)
        con.execute("CREATE TABLE stock_daily_silver AS SELECT * FROM daily_input")
        assert run_reversal_factors(con, date) == 1
        assert run_reversal_factors(con, date) == 1
        assert con.execute("SELECT COUNT(*) FROM factor_reversal_stock_wide").fetchone()[0] == 1


def test_limit_down_target_and_corporate_action_guard():
    h = daily().iloc[:25].copy()
    h.loc[h.index[-1], ["open", "high", "low", "close"]] = [10, 10, 9, 9]
    date = h.iloc[-1].trade_date
    frame = build_reversal_factors(h, date)
    assert frame.iloc[0].stk_limit_reversal == 100
    assert json.loads(frame.iloc[0].reversal_structures)["limit_reversal"]["target"] == 10
    h.loc[h.index[-1], "pre_close"] = 5
    assert build_reversal_factors(h, date).empty


def test_old_intraday_signal_is_not_backfilled_as_a_new_buy():
    frame = bars()
    later = frame.iloc[-1:].copy()
    later["time"] = "14:00:00"
    later["volume"] = 500
    assert not evaluate("limit_pullback", pd.concat([frame, later]), live=True).filled


def test_multiple_structures_do_not_hide_a_valid_reversal():
    bad_pullback = {**structure(), "support_low": 8, "support": 8.1, "support_high": 8.2, "protection": 7.9}
    execution = {"allowed_entry_modes": ["limit_pullback", "limit_reversal"],
                 "confirmation_deadline": "14:30:00",
                 "structures": {"limit_pullback": bad_pullback, "limit_reversal": structure()}}
    mode, decision = MinuteEntryEvaluator().evaluate_strategy(
        execution=execution, mode="limit_pullback", bars=bars(), prev_close=10.0,
        open_gap=0.002, sector_sync=lambda _: True)
    assert mode == "limit_reversal" and decision.filled


def test_structural_candidates_keep_anchors_and_overflow_observation():
    from core.portfolio.decision_pool_service import DecisionPoolService
    from core.screening.strategy_profiles import StrategyProfileRepository

    profile = StrategyProfileRepository().get_profile("limit_pullback")
    candidates = [{"code": f"600{i:03d}", "name": f"候选{i}", "score": 80,
                   "resonance_sectors": f"主题{i}", "context": {"sector_resonance_score": 65},
                   "metrics": {"stk_limit_pullback": 100, "stk_pullback_contraction": 80},
                   "reversal_structures": {"limit_pullback": structure()},
                   "strategy_execution": profile["execution"]} for i in range(12)]
    pool = DecisionPoolService().build({"limit_pullback": {"final": candidates}},
                                     {"limit_pullback": profile}, market_score=75,
                                     market_state={"phase": "active"})
    assert sum(bool(row["execution_eligible"]) for row in pool["rows"]) == 12
    for row in pool["rows"]:
        assert row["strategy_execution"]["structures"]["limit_pullback"]["support"] == 10
        assert row["strategy_execution"]["mode_deadlines"]["limit_pullback"] == "14:30:00"


def test_screening_reads_persisted_structures(tmp_path):
    from core.screening.screening_engine import ScreeningEngine

    path = tmp_path / "factors.duckdb"
    h = daily()
    date = h.iloc[-1].trade_date
    with duckdb.connect(str(path)) as con:
        con.register("daily_input", h)
        con.execute("CREATE TABLE stock_daily_silver AS SELECT * FROM daily_input")
        con.execute("CREATE TABLE factor_stock_wide AS SELECT * FROM daily_input WHERE trade_date=?", [date])
        run_reversal_factors(con, date)
    loaded = ScreeningEngine(duckdb_path=path).load_candidates(date)
    assert loaded.iloc[0]["stk_limit_pullback"] == 100
    assert json.loads(loaded.iloc[0]["reversal_structures"])["limit_pullback"]["event_date"] == h.iloc[25].trade_date
