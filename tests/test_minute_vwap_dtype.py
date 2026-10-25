import warnings

import pandas as pd
import pytest

from backtest.minute_entry import normalize_minute_bars


@pytest.mark.parametrize(
    "values,expected,sources",
    [
        ({"volume": [0, 0, 0]}, [10, 11, 12], ["estimated_close_weighted"] * 3),
        ({"volume": [0, 100, 100]}, [10, 11, 11.5], ["estimated_close_weighted"] * 3),
        (
            {"volume": [0, 100, 100], "amount": [0, 1050, 1150]},
            [10, 11, 11.5],
            ["estimated_close_weighted"] * 3,
        ),
        (
            {"volume": [100, 100, 100], "amount": [1050, 1150, 1250]},
            [10.5, 11, 11.5],
            ["amount_volume"] * 3,
        ),
        (
            {"volume": [0, 100, 100], "avg_price": ["10.2", None, "bad"]},
            [10.2, 11, 11.5],
            ["provider_average", "estimated_close_weighted", "estimated_close_weighted"],
        ),
        (
            {"volume": [0, 100, 100], "volume_unit": ["unknown"] * 3},
            [10, 11, 11.5],
            ["estimated_close_weighted"] * 3,
        ),
        (
            {"volume": pd.Series([0, 100, 100], dtype="Float64")},
            [10, 11, 11.5],
            ["estimated_close_weighted"] * 3,
        ),
    ],
)
def test_vwap_stays_numeric_without_implicit_downcasting(values, expected, sources):
    frame = pd.DataFrame({"time": ["09:31:00", "09:32:00", "09:33:00"], "close": [10, 11, 12], **values})
    before = frame.copy(deep=True)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        actual = normalize_minute_bars(frame)
    assert pd.api.types.is_numeric_dtype(actual["vwap"])
    assert actual["vwap"].tolist() == pytest.approx(expected)
    assert actual["vwap_source"].tolist() == sources
    pd.testing.assert_frame_equal(frame, before)


def test_vwap_prefix_does_not_use_future_turnover():
    frame = pd.DataFrame(
        {
            "time": ["09:31:00", "09:32:00", "09:33:00"],
            "close": [10, 11, 500],
            "volume": [0, 100, 100000],
        }
    )
    prefix = normalize_minute_bars(frame.iloc[:2])
    complete = normalize_minute_bars(frame)
    pd.testing.assert_series_equal(prefix["vwap"], complete["vwap"].iloc[:2])
