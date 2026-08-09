from types import SimpleNamespace

import pandas as pd

from backtest.backtest_engine import BacktestEngine


def test_aggressive_account_uses_three_way_position_size():
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = SimpleNamespace(
        account_position_pct=0.34,
        max_position_per_stock=0.34,
    )
    engine.total_capital = 1_000_000.0
    engine._last_sizing_meta = {}

    amount = engine._calculate_position_size(pd.Series(dtype=object))

    assert amount == 340_000.0
    assert engine._last_sizing_meta["method"] == "aggressive_three_position_account"


def test_historical_rotation_replaces_weaker_t1_holding(monkeypatch):
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = SimpleNamespace(rotation_min_edge=6.0)
    engine.current_positions = {
        "000001": {
            "stock_name": "旧持仓",
            "entry_date": "20260806",
            "entry_price": 10.0,
            "plan_score": 60.0,
        }
    }
    engine._last_entry_meta = {"000002": {"entry_time": "09:38:00"}}
    sold = []
    attempts = []

    monkeypatch.setattr(
        engine,
        "_load_exit_minute_bars",
        lambda _code, _date: pd.DataFrame(
            [{"time": "09:38:00", "open": 9.8, "close": 9.9}]
        ),
    )
    monkeypatch.setattr(engine, "_normalize_full_minute_bars", lambda frame: frame)
    monkeypatch.setattr(
        engine,
        "_execute_sell",
        lambda code, price, date, reason: sold.append((code, price, date, reason)),
    )
    monkeypatch.setattr(
        engine,
        "_record_gate_attempt",
        lambda *args, **kwargs: attempts.append((args, kwargs)),
    )

    rotated = engine._rotate_to_stronger_candidate(
        pd.Series({"综合评分": 80.0}),
        "20260807",
        "000002",
        "新信号",
    )

    assert rotated is True
    assert sold == [("000001", 9.8, "20260807", "rotation_to_stronger")]
    assert attempts[0][1]["status"] == "rotated"


def test_historical_rotation_cannot_sell_same_day_position(monkeypatch):
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = SimpleNamespace(rotation_min_edge=6.0)
    engine.current_positions = {
        "000001": {
            "stock_name": "当日新仓",
            "entry_date": "20260807",
            "entry_price": 10.0,
            "plan_score": 20.0,
        }
    }
    engine._last_entry_meta = {"000002": {"entry_time": "09:38:00"}}
    monkeypatch.setattr(engine, "_execute_sell", lambda *_args: None)

    assert engine._rotate_to_stronger_candidate(
        pd.Series({"综合评分": 95.0}),
        "20260807",
        "000002",
        "新信号",
    ) is False
