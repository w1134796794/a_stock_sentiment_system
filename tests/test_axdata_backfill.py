from types import SimpleNamespace

import pandas as pd
import pytest

from core.data.data_manager_stock import StockDataManager
from core.data.minute_quality import inspect_minute_bars
from core.data.providers.axdata_provider import AxDataProvider

DATE = "20261023"


def bars(date=DATE):
    times = [f"{m // 60:02d}:{m % 60:02d}:00" for start in (570, 780) for m in range(start + 1, start + 121)]
    return pd.DataFrame(
        dict(
            instrument_id="000001.SZ",
            trade_time=[f"{date[:4]}-{date[4:6]}-{date[6:]}T{t}+08:00" for t in times],
            period="1m",
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0,
            volume=100.0,
            amount=100000.0,
        )
    )


def provider(frame):
    calls = []

    def call(interface, **params):
        calls.append((interface, params))
        return frame.copy()

    return AxDataProvider(client=SimpleNamespace(call=call)), calls


def test_sdk_minute_real_ohlc_units_and_date_filter():
    raw = pd.concat([bars("20261022"), bars()], ignore_index=True)
    source, calls = provider(raw)
    result = source.get_minute_bars("000001", DATE)
    assert inspect_minute_bars(result, DATE, expected_code="000001.SZ")["valid"]
    assert result["volume_shares"].eq(10000).all() and result["amount"].eq(100000).all()
    assert not result["amount_is_estimated"].any()
    assert result["trade_date"].eq(DATE).all() and result["source"].eq("axdata").all()
    assert calls == [("stock_kline_minute_tdx", {"code": "000001.SZ", "period": "1m", "adjust": "none"})]
    assert len(raw) == 480 and "captured_at" not in raw


@pytest.mark.parametrize("fault", ["identity", "period", "point_only", "units", "nan", "ohlc", "adjustment"])
def test_bad_axdata_contract_is_rejected(fault):
    raw = bars()
    if fault == "identity":
        raw["instrument_id"] = "600000.SH"
    if fault == "period":
        raw["period"] = "5m"
    if fault == "point_only":
        raw = raw.drop(columns=["open", "high", "low"])
    if fault == "units":
        raw["amount"] /= 1000
    if fault == "nan":
        raw.loc[0, "volume"] = float("nan")
    if fault == "ohlc":
        raw["high"] = 9
    if fault == "adjustment":
        raw["adjust"] = "qfq"
    source, _ = provider(raw)
    with pytest.raises(ValueError):
        source.get_minute_bars("000001.SZ", DATE)


def test_recent_kline_not_relabelled_as_older_date_and_today_refused():
    source, calls = provider(bars())
    assert source.get_minute_bars("000001.SZ", "20261021").empty
    with pytest.raises(ValueError):
        source.get_minute_bars("000001.SZ", "20991231")
    assert len(calls) == 1


def test_daily_maps_to_existing_thousand_yuan_contract_without_inventing_prior_close():
    raw = bars().iloc[-1:].copy()
    raw["period"] = "day"
    source, calls = provider(raw)
    frame = source.get_daily_bars("000001.SZ", DATE)
    assert frame.iloc[0]["amount"] == 100 and frame.iloc[0]["amount_yuan"] == 100000
    assert frame.iloc[0]["vol"] == 100 and not frame.iloc[0]["prior_close_available"]
    assert calls[0][0] == "stock_kline_daily_tdx"


def auction():
    return pd.DataFrame(
        [
            dict(
                instrument_id="000001.SZ",
                trade_date=DATE,
                auction_datetime="2026-10-23T09:25:00+08:00",
                trade_index=0,
                price=10.0,
                volume=100.0,
                amount=100000.0,
            )
        ]
    )


@pytest.mark.parametrize("fault", [None, "date", "time", "units", "duplicate"])
def test_auction_is_dated_final_result_not_current_quote(fault):
    raw = auction()
    if fault == "date":
        raw["trade_date"] = "20261022"
    if fault == "time":
        raw["auction_datetime"] = "2026-10-23T09:26:00+08:00"
    if fault == "units":
        raw["amount"] = 100
    if fault == "duplicate":
        raw = pd.concat([raw, raw], ignore_index=True)
    source, _ = provider(raw)
    if fault:
        with pytest.raises(ValueError):
            source.get_auction_0925("000001.SZ", DATE)
    else:
        result = source.get_auction_0925("000001.SZ", DATE)
        assert result["amount"] == 100000 and result["volume"] == 100 and result["source"] == "axdata_0925"


@pytest.mark.parametrize("fault", [None, "half_day", "wrong_day", "timeout"])
def test_backfill_prioritizes_axdata_but_only_full_minutes_reach_cache(tmp_path, monkeypatch, fault):
    source, _ = provider(
        bars("20261022" if fault == "wrong_day" else DATE).iloc[:120]
        if fault == "half_day"
        else bars("20261022" if fault == "wrong_day" else DATE)
    )
    if fault == "timeout":

        def failed(*a, **kw):
            raise TimeoutError("test")

        source.client.call = failed
    dm = StockDataManager("", tmp_path)
    dm.prefer_axdata_history = True
    monkeypatch.setattr(dm, "_get_axdata_provider", lambda: source)
    fallback = []
    valid, _ = provider(bars())

    def fetch(code, date):
        fallback.append(code)
        return valid.get_minute_bars(code, date)

    monkeypatch.setattr(dm, "_get_eltdx_provider", lambda: SimpleNamespace(get_minute_bars=fetch))
    monkeypatch.setattr(dm, "_get_ashare_provider", lambda: None)
    assert len(dm.get_stock_tick("000001.SZ", DATE, require_full_day=True)) == 240
    assert fallback == ([] if fault is None else ["000001.SZ"])
    expected = "axdata" if fault is None else "eltdx"
    assert dm.get_minute_quality_report("000001.SZ", DATE)["source"] == expected
    before = len(fallback)
    dm.get_stock_tick("000001.SZ", DATE)
    assert len(fallback) == before


def test_axdata_never_initialized_by_default_or_cache_only_manager(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Read-only/default use must not initialize AxData")

    monkeypatch.setattr(AxDataProvider, "__init__", forbidden)
    dm = StockDataManager("", tmp_path)
    assert dm._get_axdata_provider() is None
    dm.prefer_axdata_history, dm.allow_remote_history = True, False
    assert dm._get_axdata_provider() is None
    assert dm.get_stock_tick("000001.SZ", DATE).empty


def test_provider_failure_cooldown_does_not_retry_each_stock():
    calls = []

    def failed(*a, **kw):
        calls.append(True)
        raise TimeoutError("test")

    source = AxDataProvider(client=SimpleNamespace(call=failed))
    with pytest.raises(TimeoutError):
        source.get_minute_bars("000001.SZ", DATE)
    with pytest.raises(RuntimeError, match="cooldown"):
        source.get_minute_bars("000002.SZ", DATE)
    assert calls == [True]


def test_local_sdk_forced_even_with_remote_environment(tmp_path, monkeypatch):
    pytest.importorskip("axdata")
    monkeypatch.setenv("AXDATA_API_BASE", "http://not-used.invalid:8666")
    source = AxDataProvider(data_root=tmp_path)
    assert source.client.mode == "local"


def test_daily_and_auction_acquisition_then_cache_reuse(tmp_path, monkeypatch):
    dm = StockDataManager("", tmp_path)
    dm.prefer_axdata_history = True
    daily = bars().iloc[-1:].copy()
    daily["period"] = "day"
    calls = []

    def call(interface, **params):
        calls.append(interface)
        return (auction() if interface == "stock_auction_result_history_tdx" else daily).copy()

    source = AxDataProvider(client=SimpleNamespace(call=call))
    monkeypatch.setattr(dm, "_get_axdata_provider", lambda: source)
    result = dm.get_stock_daily_data("000001.SZ", DATE)
    assert result["close"] == 10 and result["amount"] == 100 and result["vol"] == 100
    dm.get_stock_daily_data("000001.SZ", DATE)
    result = dm.get_auction_data("000001.SZ", DATE)
    assert result["数据源"] == "axdata_0925" and result["竞价成交量"] == 100
    dm.get_auction_data("000001.SZ", DATE)
    assert calls == ["stock_kline_daily_tdx", "stock_auction_result_history_tdx"]


def test_server_role_cannot_construct_axdata_even_if_flag_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.MARKET_DATA_NODE_ROLE", "server")

    def forbidden(*a, **kw):
        pytest.fail("Strategy/server roles may not initialize AxData")

    monkeypatch.setattr(AxDataProvider, "__init__", forbidden)
    dm = StockDataManager("", tmp_path)
    dm.prefer_axdata_history = True
    assert dm._get_axdata_provider() is None
