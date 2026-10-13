from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backtest.backtest_engine import BacktestConfig
from backtest.evidence_policy import ReplayEvidenceBlocked
from core.operations.replay_research import FrozenCalendar, ResearchSpec, account_metrics, research_arms
from core.operations.sizing_research import SizingParameters, SizingResearchEngine, matured_tier, tail_metrics


def trade(date="20260901", **kwargs):
    return {"date": date, "entry_date": "20260831", "action": "SELL", "strategy_id": "weak_to_strong",
            "pnl_pct": .1, **kwargs}


def tier(rows, **kwargs):
    return matured_tier(rows, strategy="weak_to_strong", regime="weak / retreat",
        regimes={"20260831": "weak / retreat", "20260901": "strong / active"},
        train_start="20260801", cutoff="20260903", params=SizingParameters(), **kwargs)


def test_insufficient_samples_never_authorize_high_risk():
    result = tier([trade()] * 49)
    assert result["tier"] == "low" and result["risk_budget"] == .0025


def test_future_and_embargo_outcomes_cannot_change_tier():
    before = tier([trade()] * 20)
    assert tier([trade()] * 20 + [trade("20260904")] * 1000) == before
    assert tier([trade()] * 20 + [trade(entry_date="20260701")] * 1000) == before
    assert tier([trade()] * 20 + [trade(strategy_id="mainline_leader")] * 1000) == before
    assert tier([trade()] * 20 + [trade(entry_date="20260901")] * 1000) == before


def test_positive_lower_bound_required_for_heavy_tier():
    high = tier([trade()] * 80 + [trade(pnl_pct=-.02)] * 20)
    assert high["tier"] == "high" and high["edge_lower"] > .15
    low = tier([trade(pnl_pct=.02)] * 20 + [trade(pnl_pct=-.10)] * 80)
    assert low["tier"] == "low" and low["reason"] == "no_conservative_positive_edge"
    assert tier([trade()] * 100)["tier"] == "low"


@pytest.mark.parametrize("kwargs", [{"low_risk": .01}, {"gap_floor": float("nan")},
    {"gap_min_samples": 21}, {"sector_risk_budget": .02}, {"min_samples": True}, {"high_edge_min": -1}])
def test_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        SizingParameters(**kwargs)


def test_prior_gap_history_uses_no_current_or_future_open():
    engine = object.__new__(SizingResearchEngine)
    engine.parameters = SizingParameters(gap_lookback=2, gap_min_samples=2, gap_floor=.01)
    engine.calendar = FrozenCalendar(["20261008", "20261009", "20261012"])
    reads = []
    def daily(code, day):
        reads.append(day)
        return {"open": 9 if day == "20261009" else 10, "pre_close": 10}
    engine.dm = SimpleNamespace(get_stock_daily_data=daily)
    engine._gap_cache = {}
    assert engine._gap_risk("000001", "20261012") == pytest.approx((.1, 2))
    assert reads == ["20261008", "20261009"]
    engine.dm = SimpleNamespace(get_stock_daily_data=lambda *args: {})
    engine._gap_cache = {}
    with pytest.raises(ReplayEvidenceBlocked):
        engine._gap_risk("000001", "20261012")


def sizing_engine(positions=None):
    engine = object.__new__(SizingResearchEngine)
    engine.arm = "fixed_risk"
    engine.config = BacktestConfig(account_position_pct=.34)
    engine.parameters = SizingParameters()
    engine.spec = ResearchSpec(suite="position_risk")
    engine.cash = 1000000.
    engine.current_positions = positions or {}
    engine.total_capital = engine.cash + sum(p["market_value"] for p in engine.current_positions.values())
    engine.research_entry_prices = {"000001": 10.}
    engine._last_entry_meta = {}
    engine._active_sizing_date = "20261009"
    engine._gap_risk = lambda *args: (.08, 20)
    engine._plan_execution = lambda p: {}
    engine._execution_exit_config = lambda e: {"hard_stop_loss": .05}
    engine.sizing_decisions = []
    return engine


def test_fixed_risk_bypasses_34_percent_and_includes_two_sided_slippage():
    engine = sizing_engine()
    plan = {"代码": "000001", "策略ID": "weak_to_strong", "共振板块": "电池"}
    expected = 5000 / (.08 + .0006 + .0005 + .004)
    assert engine._calculate_position_size(plan) == pytest.approx(expected)
    assert engine._last_sizing_meta["stress_distance"] == pytest.approx(.0851)
    assert engine._last_sizing_meta["position_pct"] < .10


def test_sector_and_portfolio_risk_reduce_cash_even_when_account_has_room():
    engine = sizing_engine({"000002": {"market_value": 80000., "stress_risk_distance": .0851,
                                       "resonance_sectors": "电池"}})
    size = engine._calculate_position_size({"代码": "000001", "策略ID": "weak_to_strong", "共振板块": "电池"})
    equity = 1080000.
    assert size == pytest.approx((equity * .0075 - 80000 * .0851) / .0851)
    engine.parameters = SizingParameters(portfolio_risk_budget=.0075)
    assert engine._calculate_position_size({"代码": "000001", "策略ID": "weak_to_strong", "共振板块": "汽车"}) == pytest.approx(size)


def test_tier_window_excludes_five_sessions():
    engine = sizing_engine()
    engine.calendar = FrozenCalendar([f"202609{i:02d}" for i in range(1, 11)])
    engine._active_sizing_date = "20260910"
    engine.reference_trades = [trade("20260905", entry_date="20260903")] * 100
    engine.regimes = {day: "weak / retreat" for day in engine.calendar.dates}
    result = engine._tier({"策略ID": "weak_to_strong"})
    assert result["outcome_cutoff"] == "20260904" and result["sample_size"] == 0


def test_reference_adjusted_fractional_quantity_fee_is_exact():
    state = {"trade_history": [{"action": "SELL", "date": "20261009", "entry_date": "20261008",
        "entry_price": 10., "exit_price": 8., "shares": 140.25, "position_size": 1000.3,
        "pnl": 120.2, "pnl_pct": .12016395}]}
    assert account_metrics(state, 100000.)["fees"] == pytest.approx(1.5)


def test_tail_metrics_do_not_call_trade_loss_percent_account_drawdown():
    state = {"daily_nav": [{"total_value": 900.}, {"total_value": 990.}],
        "trade_history": [{"exit_reason": "stop_loss_gap_minute", "pnl": -100}],
        "sizing_decisions": [{"tier": "low"}, {"tier": "low"}]}
    metrics = tail_metrics(state, 1000.)
    assert metrics["daily_expected_shortfall_95_pct"] == pytest.approx(-10)
    assert metrics["gap_stop_net_pnl"] == -100 and metrics["sizing_tier_counts"] == {"low": 2}
    assert not metrics["stress_budget_is_loss_guarantee"]


def test_three_registered_arms_and_parameters():
    spec = ResearchSpec(suite="position_risk", optimization=asdict(SizingParameters()))
    assert list(research_arms(spec)) == ["baseline", "fixed_risk", "tiered_risk"]


@pytest.mark.parametrize("changed_price", [False, True])
def test_independent_audit_reconciles_adjusted_quantity_and_rejects_changed_signal(tmp_path, changed_price):
    from core.operations.replay_research import atomic_json, digest
    from scripts.audit_sizing_risk import audit

    results = {}
    for arm in ("baseline", "fixed_risk", "tiered_risk"):
        (tmp_path / "plans" / arm).mkdir(parents=True)
        (tmp_path / "plans" / arm / "same.csv").write_text("same candidate", encoding="utf-8")
        state = {"initial_capital": 100000., "cash": 100120.2, "total_capital": 100120.2,
            "current_positions": {}, "daily_nav": [{"position_value": 0., "total_value": 100120.2}],
            "entry_attempts": [{"date": "20261008", "stock_code": "000001", "status": "filled",
                "entry_price": 11 if changed_price and arm == "tiered_risk" else 10,
                "entry_time": "09:39:00", "confirm_time": "09:38:00", "entry_mode": "weak_to_strong"}],
            "trade_history": [
                {"date": "20261008", "action": "BUY", "stock_code": "000001", "entry_price": 10.,
                 "position_size": 1000.3, "shares": 100},
                {"date": "20261009", "entry_date": "20261008", "action": "SELL", "stock_code": "000001",
                 "exit_price": 8., "shares": 140.25, "position_size": 1000.3, "pnl": 120.2,
                 "strategy_id": "weak_to_strong"}]}
        path = f"states/{arm}.json"
        atomic_json(tmp_path / path, {"state": state, "state_hash": digest(state)})
        results[arm] = {"state_path": path, "summary": {"fees": 1.8}}
    if changed_price:
        with pytest.raises(ValueError, match="signal timing or price"):
            audit(tmp_path, {"results": results})
    else:
        output = audit(tmp_path, {"results": results})
        assert output["identical_candidate_plans"]
        assert output["results"]["fixed_risk"]["fees"] == pytest.approx(1.8)


def test_independent_marks_reject_entry_cost_nav_and_check_adjustment():
    from scripts.audit_sizing_risk import reconcile_daily_marks

    class Evidence:
        def read(self, kind, code, date):
            if kind == 'suspension':
                return {'state': 'full_day'} if date == '20260702' else {}
            return {'close': 4, 'pre_close': 5} if date == '20260701' else {'close': 3, 'pre_close': 2}

    state = {'initial_capital': 1000, 'trade_history': [
        {'date': '20260701', 'stock_code': '000001', 'action': 'BUY',
         'shares': 100, 'entry_price': 5, 'position_size': 500.15},
        {'date': '20260703', 'stock_code': '000001', 'action': 'SELL',
         'shares': 200, 'position_size': 500.15, 'pnl': 99.37}],
        'daily_nav': [{'date': '20260701', 'cash': 499.85, 'position_value': 400,
                       'total_value': 899.85, 'position_count': 1},
                      {'date': '20260702', 'cash': 499.85, 'position_value': 400,
                       'total_value': 899.85, 'position_count': 1},
                      {'date': '20260703', 'cash': 1099.37, 'position_value': 0,
                       'total_value': 1099.37, 'position_count': 0}]}
    assert reconcile_daily_marks(state, Evidence()) == 3
    state['daily_nav'][0].update(position_value=500, total_value=999.85)
    with pytest.raises(ValueError, match='daily close NAV mismatch'):
        reconcile_daily_marks(state, Evidence())
