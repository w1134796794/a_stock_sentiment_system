from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from core.data.minute_clock import completed_minutes
from core.data.minute_quality import inspect_minute_bars
from core.data.providers.eltdx_provider import EltdxProvider
from test_minute_cache_quality import full_bars


def test_first_end_labeled_minute_is_available_at_0931_not_0932():
    frame = full_bars().iloc[:1].assign(bar_label='end')
    assert completed_minutes(frame, '09:30:59').empty
    assert len(completed_minutes(frame, '09:31:00')) == 1
    assert completed_minutes(frame.drop(columns='bar_label'), '09:31:00').empty


def test_lunch_does_not_require_0930_or_1300_candles():
    frame = full_bars().iloc[:120].assign(bar_label='end')
    for hour, minute in [(11, 30), (12, 0), (13, 0)]:
        report = inspect_minute_bars(frame, '20260901', require_full_day=False,
                                    now=datetime(2026, 9, 1, hour, minute))
        assert report['valid'], report
        assert report['expected_minutes'] == 120
    report = inspect_minute_bars(frame, '20260901', require_full_day=False,
                                now=datetime(2026, 9, 1, 13, 1))
    assert report['missing_times'] == ['13:01:00']


def test_delayed_morning_receipt_is_not_an_afternoon_bar_or_visible_before_receipt():
    frame = pd.DataFrame([{'time': '11:30:00', 'bar_label': 'end',
                           'received_at': '2026-09-01T13:00:00+08:00'}])
    assert completed_minutes(frame, '2026-09-01T11:30:00+08:00').empty
    assert completed_minutes(frame, '2026-09-01T13:00:00+08:00').iloc[0]['time'] == '11:30:00'


def test_eltdx_requests_end_labeled_candles_explicitly():
    calls = []
    class Client:
        bars = SimpleNamespace(get=lambda *a, **kw: (calls.append(kw) or SimpleNamespace(bars=[])))
        def __enter__(self): return self
        def __exit__(self, *a): pass
    provider = EltdxProvider()
    provider._client = Client
    provider.get_kline('000001', period='1min')
    assert calls[0]['bar_time'] == 'end'


def test_declared_end_grid_does_not_guess_start_grid_to_hide_missing_0931():
    frame = full_bars().iloc[1:].assign(bar_label='end')
    report = inspect_minute_bars(frame, '20260901')
    assert report['missing_times'] == ['09:31:00']


def test_explicit_start_grid_converts_once_without_crossing_lunch():
    from backtest.minute_entry import normalize_minute_bars
    frame = full_bars(offset=0).assign(bar_label='start')
    normalized = normalize_minute_bars(frame)
    assert normalized.iloc[0]['time'] == '09:31:00'
    assert normalized.iloc[119]['time'] == '11:30:00'
    assert normalized.iloc[120]['time'] == '13:01:00'
    assert normalized.iloc[-1]['time'] == '15:00:00'
    assert normalize_minute_bars(normalized)['time'].tolist() == normalized['time'].tolist()
