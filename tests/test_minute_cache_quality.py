from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from core.data.data_manager_stock import StockDataManager
from core.data.minute_quality import inspect_minute_bars
from scripts.prefetch_strategy_minutes import _minute_cached


def full_bars(date="20260901", *, offset=1):
    times = [f"{m // 60:02d}:{m % 60:02d}:00"
             for start in (570, 780) for m in range(start + offset, start + 120 + offset)]
    return pd.DataFrame({"trade_date": date, "time": times, "close": 10.0, "volume": 100.0})


@pytest.mark.parametrize("offset", [0, 1])
def test_complete_start_and_end_stamped_minutes(offset):
    assert inspect_minute_bars(full_bars(offset=offset), "20260901")["valid"]


def test_eltdx_optional_empty_datetime_with_explicit_date():
    frame = full_bars().rename(columns={"trade_date": "date"})
    frame["datetime"] = float("nan")
    assert inspect_minute_bars(frame, "20260901")["valid"]


def test_wrong_stock_and_impossible_ohlc_are_rejected():
    frame = full_bars()
    frame["ts_code"] = "000002.SZ"
    assert not inspect_minute_bars(frame, "20260901", expected_code="000001.SZ")["valid"]
    frame["open"], frame["high"], frame["low"] = 10, 9, 8
    assert not inspect_minute_bars(frame, "20260901")["valid"]


@pytest.mark.parametrize("kind", ["half_day", "internal_gap", "wrong_date", "nan_price", "duplicate", "no_volume"])
def test_invalid_minute_evidence(kind):
    frame = full_bars()
    if kind == "half_day": frame = frame.iloc[:120]
    if kind == "internal_gap": frame = frame.drop(42)
    if kind == "wrong_date": frame["trade_date"] = "20260831"
    if kind == "nan_price": frame.loc[42, "close"] = float("nan")
    if kind == "duplicate": frame = pd.concat([frame, frame.iloc[[42]]])
    if kind == "no_volume": frame = frame.drop(columns="volume")
    report = inspect_minute_bars(frame, "20260901")
    assert not report["valid"] and report["reason"]


def test_lunch_is_not_a_gap_and_intraday_only_requires_closed_minutes():
    frame = full_bars().iloc[:60]
    report = inspect_minute_bars(frame, "20260901", require_full_day=False,
                                 now=datetime(2026, 9, 1, 10, 32))
    assert not report["valid"]  # 10:31 was not received.
    assert inspect_minute_bars(full_bars().iloc[:61], "20260901", require_full_day=False,
                               now=datetime(2026, 9, 1, 10, 32))["valid"]
    assert inspect_minute_bars(full_bars().iloc[:120], "20260901", require_full_day=False,
                               now=datetime(2026, 9, 1, 12, 30))["valid"]


def manager(tmp_path, monkeypatch, returned):
    dm = StockDataManager("", tmp_path)
    calls = []
    def fetch(code, date):
        calls.append((code, date))
        return returned
    monkeypatch.setattr(dm, "_get_eltdx_provider", lambda: SimpleNamespace(get_minute_bars=fetch))
    monkeypatch.setattr(dm, "_get_ashare_provider", lambda: None)
    return dm, calls


def test_partial_cache_is_repaired_and_then_reused(tmp_path, monkeypatch):
    dm, calls = manager(tmp_path, monkeypatch, full_bars())
    path = dm.stock_dir / "tick/000001.SZ_20260901.csv"
    full_bars().iloc[:120].to_csv(path, index=False)
    assert not _minute_cached(tmp_path, "20260901", "000001.SZ")
    assert len(dm.get_stock_tick("000001.SZ", "20260901")) == 240
    assert _minute_cached(tmp_path, "20260901", "000001.SZ")
    dm.get_stock_tick("000001.SZ", "20260901")
    assert calls == [("000001.SZ", "20260901")]


def test_wrong_date_remote_does_not_replace_partial_cache(tmp_path, monkeypatch):
    dm, calls = manager(tmp_path, monkeypatch, full_bars("20260831"))
    path = dm.stock_dir / "tick/000001.SZ_20260901.csv"
    full_bars().iloc[:120].to_csv(path, index=False)
    before = path.read_bytes()
    assert dm.get_stock_tick("000001.SZ", "20260901").empty
    assert path.read_bytes() == before
    report = dm.get_minute_quality_report("000001.SZ", "20260901")
    assert "日期不一致" in report["reason"]
    assert report["attempts"]


def test_cache_only_never_calls_remote_and_force_refresh_is_explicit(tmp_path, monkeypatch):
    dm, calls = manager(tmp_path, monkeypatch, full_bars())
    dm.allow_remote_history = False
    assert dm.get_stock_tick("000001.SZ", "20260901").empty
    assert calls == []
    dm.allow_remote_history = True
    dm.get_stock_tick("000001.SZ", "20260901")
    dm.get_stock_tick("000001.SZ", "20260901", force_refresh=True)
    assert len(calls) == 2


def test_paper_rebuild_can_allow_only_remote_minutes(tmp_path, monkeypatch):
    dm, calls = manager(tmp_path, monkeypatch, full_bars())
    dm.allow_remote_history = False
    dm.allow_remote_minutes = True
    assert len(dm.get_stock_tick("000001.SZ", "20260901")) == 240
    assert len(calls) == 1
