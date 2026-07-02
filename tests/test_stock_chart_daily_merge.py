import pandas as pd

from web.app import _df_to_daily_candles, _merge_intraday_daily_candle


def test_intraday_bar_is_added_as_selected_days_last_daily_candle():
    daily = pd.DataFrame([
        {"trade_date": "20260701", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.3},
    ])
    minute = pd.DataFrame([
        {"time": "09:31:00", "open": 10.4, "high": 10.6, "low": 10.3, "close": 10.5, "volume": 100},
        {"time": "09:32:00", "open": 10.5, "high": 10.8, "low": 10.4, "close": 10.7, "volume": 200},
    ])

    merged = _merge_intraday_daily_candle(daily, minute, "20260702", limit=120)

    assert merged["trade_date"].tolist() == ["20260701", "20260702"]
    current = merged.iloc[-1]
    assert current["open"] == 10.4
    assert current["high"] == 10.8
    assert current["low"] == 10.3
    assert current["close"] == 10.7
    assert current["source"] == "intraday_aggregate"
    assert len(_df_to_daily_candles(merged)) == 2


def test_intraday_bar_replaces_stale_same_day_candle_without_duplicate():
    daily = pd.DataFrame([
        {"trade_date": "20260702", "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0},
        {"trade_date": "20260703", "open": 20.0, "high": 20.0, "low": 20.0, "close": 20.0},
    ])
    minute = pd.DataFrame([
        {"time": "09:31:00", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1},
    ])

    merged = _merge_intraday_daily_candle(daily, minute, "20260702", limit=120)

    assert merged["trade_date"].tolist() == ["20260702"]
    assert merged.iloc[0]["close"] == 10.1
