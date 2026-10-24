from datetime import date, datetime
from types import SimpleNamespace

import pytest

from core.data.providers.eltdx_provider import EltdxProvider


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


@pytest.mark.parametrize("today", [True, False])
def test_modular_minute_calls_preserve_normalized_units(today):
    calls = []
    series = SimpleNamespace(prev_close=10, points=[SimpleNamespace(time_label="09:31:00", time=None,
        price=10.2, volume=5, avg_price=10.1)])
    client = Client()
    client.minutes = SimpleNamespace(today=lambda code: (calls.append((code,)) or series),
        history=lambda code, day: (calls.append((code, day)) or series))
    provider = EltdxProvider()
    provider._client = lambda: client
    provider._is_today = lambda _: today
    frame = provider.get_minute_bars("000001", "20261023")
    assert calls == [("sz000001",)] if today else calls == [("sz000001", "2026-10-23")]
    assert frame.iloc[0]["volume"] == 5
    assert frame.iloc[0]["amount"] == pytest.approx(5100)
    assert frame.iloc[0]["avg_price"] == 10.1


def test_new_auction_helper_uses_only_requested_snapshot():
    calls = []
    client = Client()
    def auction_data(code, day, **kwargs):
        calls.append((code, day, kwargs))
        return SimpleNamespace(snapshot_0925=SimpleNamespace(price=10, volume=100,
            trade_amount_yuan=100000, event_kind="opening_match", time_label="09:25"))
    client.helpers = SimpleNamespace(auction_data=auction_data)
    provider = EltdxProvider()
    provider._client = lambda: client
    result = provider.get_auction_0925("000001", "20261023")
    assert result["amount"] == 100000
    assert calls == [("sz000001", date(2026, 10, 23), {"include_series": False, "include_quote": False})]


def test_real_sdk_trade_tick_property_is_not_lost_by_dataclass_conversion():
    models = pytest.importorskip("eltdx.models")
    tick = models.TradeTick(index=0, absolute_index=0, time_minutes=565, time_label="09:25",
        trade_datetime=datetime(2026, 10, 23, 9, 25), price=10, price_milli=10000,
        volume=100, order_count=1, status_raw=0, side="buy", price_delta_raw=0, price_acc_raw=0,
        event_kind="opening_match")
    result = EltdxProvider()._auction_0925_to_dict(tick, "000001", "20261023")
    assert result["amount"] == 100000 and result["volume"] == 100
    from dataclasses import replace
    assert not EltdxProvider()._auction_0925_to_dict(replace(tick, event_kind="auction_snapshot"), "000001", "20261023")


def test_installed_sdk_native_dispatch_and_board_capabilities_are_offline():
    pytest.importorskip("eltdx")
    from scripts.check_eltdx_runtime import probe
    result = probe()
    assert result["ok"] and result["version"] == "3.3.0"
    assert not result["network_accessed"] and not result["live_data_verified"]


def test_real_v3_quote_model_preserves_hands_yuan_and_opaque_time():
    models = pytest.importorskip("eltdx.models")
    from dataclasses import fields
    values = {field.name: 0 for field in fields(models.QuoteSnapshot)}
    values.update(exchange="sz", market_id=0, code="000001", last_price=10.2, pre_close_price=10,
                  open_price=10.1, total_hand=123, amount=123456, open_amount_yuan=20000,
                  time_raw=12345678, buy_levels=(models.QuoteLevel(price=10.19, volume=5, price_delta_raw=0),),
                  sell_levels=(models.QuoteLevel(price=10.2, volume=6, price_delta_raw=0),), tail_raw=b"")
    snapshot = models.QuoteSnapshot(**values)
    from core.realtime.trading_session import shanghai_now
    result = EltdxProvider._normalize_quote_snapshot(snapshot, shanghai_now())
    assert result["vol_hand"] == 123 and result["amount_yuan"] == 123456
    assert result["bid_vol1"] == 5 and result["ask_vol1"] == 6
    assert result["open_amount"] == 20000 and result["source_time_raw"] == 12345678
