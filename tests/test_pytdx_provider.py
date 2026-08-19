from datetime import datetime
from types import SimpleNamespace

from core.data.providers.pytdx_provider import (
    PytdxProvider,
    SnapshotMinuteStore,
    SnapshotTickStore,
)


def test_snapshot_store_uses_cumulative_volume_delta_once():
    store = SnapshotMinuteStore()
    store.update(
        "000001",
        {"last_price": 10.0, "vol_hand": 1000, "amount_yuan": 1_000_000},
        datetime(2026, 8, 13, 9, 31, 1),
    )
    store.update(
        "000001",
        {"last_price": 10.1, "vol_hand": 1010, "amount_yuan": 1_010_100},
        datetime(2026, 8, 13, 9, 31, 4),
    )
    store.update(
        "000001",
        {"last_price": 10.05, "vol_hand": 1015, "amount_yuan": 1_015_125},
        datetime(2026, 8, 13, 9, 31, 7),
    )

    frame = store.frame("000001", "20260813")
    assert len(frame) == 1
    assert frame.iloc[0]["open"] == 10.0
    assert frame.iloc[0]["high"] == 10.1
    assert frame.iloc[0]["low"] == 10.0
    assert frame.iloc[0]["close"] == 10.05
    assert frame.iloc[0]["volume"] == 15
    assert frame.iloc[0]["amount"] == 15_125


def test_history_seed_does_not_replace_live_minute_or_volume_baseline():
    store = SnapshotMinuteStore()
    store.update(
        "600000",
        {"last_price": 12.0, "vol_hand": 500, "amount_yuan": 600_000},
        datetime(2026, 8, 13, 9, 31, 1),
    )
    store.seed(
        "600000",
        "20260813",
        [{
            "time": "09:30:00",
            "open": 11.8,
            "high": 11.9,
            "low": 11.8,
            "close": 11.9,
            "volume": 100,
            "amount": 119_000,
        }],
    )
    store.update(
        "600000",
        {"last_price": 12.1, "vol_hand": 510, "amount_yuan": 612_100},
        datetime(2026, 8, 13, 9, 31, 4),
    )

    frame = store.frame("600000", "20260813")
    assert list(frame["time"]) == ["09:30:00", "09:31:00"]
    assert frame.iloc[-1]["volume"] == 10


def test_pytdx_market_mapping():
    assert PytdxProvider._market("600000") == 1
    assert PytdxProvider._market("688001") == 1
    assert PytdxProvider._market("000001") == 0
    assert PytdxProvider._market("300001") == 0
    assert PytdxProvider._market("830001") == 0


def test_pytdx_normalizes_quote_and_updates_minute_store():
    provider = PytdxProvider(clock=lambda: datetime(2026, 8, 13, 9, 31, 3))
    provider._api = SimpleNamespace(
        get_security_quotes=lambda _request: [{
            "code": "000001",
            "price": 10.2,
            "last_close": 10.0,
            "open": 10.1,
            "high": 10.3,
            "low": 10.05,
            "vol": 1200,
            "amount": 1_224_000,
            "bid1": 10.19,
            "ask1": 10.2,
        }]
    )
    provider._connected_server = ("fake", 7709)

    result = provider.get_quote_snapshots(["000001.SZ"])

    assert result["000001"]["source"] == "pytdx_snapshot_3s"
    assert round(result["000001"]["change_pct"], 4) == 2.0
    assert provider.minute_store.frame("000001", "20260813").iloc[0]["close"] == 10.2


def test_pytdx_failure_cooldown_skips_repeated_connections():
    provider = PytdxProvider(failure_cooldown_seconds=60)
    calls = {"count": 0}

    def fail():
        calls["count"] += 1
        raise ConnectionError("offline")

    provider._ensure_connection = fail
    assert provider.get_quote_snapshots(["000001"]) == {}
    assert provider.get_quote_snapshots(["000001"]) == {}
    assert calls["count"] == 1


def test_snapshot_tick_store_keeps_recent_deltas():
    store = SnapshotTickStore(max_items=20)
    first = store.update(
        "000001",
        {"last_price": 10.0, "vol_hand": 100, "amount_yuan": 100_000},
        datetime(2026, 8, 13, 9, 31, 1),
    )
    second = store.update(
        "000001",
        {"last_price": 10.1, "vol_hand": 108, "amount_yuan": 108_080},
        datetime(2026, 8, 13, 9, 31, 4),
    )

    assert first["delta_volume"] == 0
    assert second["delta_volume"] == 8
    assert second["delta_amount"] == 8_080
    assert len(store.frame("000001", "20260813")) == 2


def test_snapshot_tick_store_rejects_cumulative_counter_regression():
    store = SnapshotTickStore(max_items=20)
    store.update(
        "000001",
        {"last_price": 10.0, "vol_hand": 100, "amount_yuan": 100_000},
        datetime(2026, 8, 13, 9, 31, 1),
    )
    bad = store.update(
        "000001",
        {"last_price": 10.1, "vol_hand": 90, "amount_yuan": 90_000},
        datetime(2026, 8, 13, 9, 31, 4),
    )
    recovered = store.update(
        "000001",
        {"last_price": 10.2, "vol_hand": 105, "amount_yuan": 105_000},
        datetime(2026, 8, 13, 9, 31, 7),
    )

    assert bad["quality_ok"] is False
    assert bad["delta_volume"] == 0
    assert recovered["quality_ok"] is True
    assert recovered["delta_volume"] == 5
