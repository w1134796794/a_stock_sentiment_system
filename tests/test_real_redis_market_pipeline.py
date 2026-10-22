"""Opt-in integration tests against an isolated, disposable Redis instance."""
import os
import subprocess
import sys
from datetime import timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.infrastructure.shared_state import RedisStateBackend, TaskLease
from core.realtime.snapshot_repository import RealtimeSnapshotRepository
from core.realtime.trading_session import shanghai_now


URL = os.getenv("MARKET_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Set MARKET_TEST_REDIS_URL to an isolated Redis")


@pytest.fixture
def backend():
    value = RedisStateBackend(URL, "market_test_" + uuid4().hex)
    try:
        yield value
    finally:
        keys = list(value.client.scan_iter(match=value.prefix + ":*"))
        if keys:
            value.client.delete(*keys)
        value.client.close()


def test_real_sorted_set_hot_window_and_cross_process_notification(backend):
    from core.realtime.market_events import REVISION_KEY, CHANNEL
    clock = [shanghai_now()]
    date = clock[0].strftime("%Y%m%d")
    repository = RealtimeSnapshotRepository(backend=backend, clock=lambda: clock[0])
    rows = [{"code": "000001", "date": date, "time": (clock[0] - timedelta(seconds=i * 3)).strftime("%H:%M:%S"),
             "received_at": (clock[0] - timedelta(seconds=i * 3)).isoformat(), "last_price": 10}
            for i in range(100)]
    repository.append_batch(date, rows)
    assert len(repository.read(date, "000001")) == 60
    assert backend.client.zcard(backend._key(repository._key(date, "000001"))) == 60
    assert 0 < backend.client.ttl(backend._key(repository._key(date, "000001"))) <= 180
    subscriber = backend.client.pubsub(ignore_subscribe_messages=True)
    subscriber.subscribe(backend._key(CHANNEL))
    subscriber.get_message(timeout=1)
    try:
        result = subprocess.run([sys.executable, "-X", "utf8", "-c",
            "import sys; from core.infrastructure.shared_state import RedisStateBackend; "
            "from core.realtime.market_events import publish_revision; "
            "publish_revision(RedisStateBackend(sys.argv[1],sys.argv[2]),sys.argv[3],kind='quotes')",
            URL, backend.prefix, date], capture_output=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        message, deadline = None, monotonic() + 5
        while message is None and monotonic() < deadline:
            message = subscriber.get_message(timeout=1)
        assert message and message["data"] == backend.get_json(REVISION_KEY)["id"]
    finally:
        subscriber.close()
    clock[0] += timedelta(seconds=181)
    assert repository.read(date, "000001") == []


def test_real_redis_quote_and_minute_readers_share_collector_storage(backend, tmp_path):
    import pandas as pd
    from core.data.providers.redis_quote_provider import RedisQuoteProvider
    from core.realtime.market_journal import MarketJournal
    from core.realtime.minute_cache import RealtimeMinuteCache
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.read_only_market import ReadOnlyMarketData
    now = shanghai_now()
    date = now.strftime("%Y%m%d")
    journal = MarketJournal(tmp_path)
    cache = RealtimeQuoteCache(backend=backend, journal=journal,
        snapshot_repository=RealtimeSnapshotRepository(backend=backend),
        opening_repository=SimpleNamespace(record_batch=lambda *a, **kw: None))
    cache.write_batch([{"code": "000001", "date": date, "time": now.strftime("%H:%M:%S"),
        "received_at": now.isoformat(), "last_price": 10, "pre_close": 9.9,
        "open_price": 9.95, "vol_hand": 100, "amount_yuan": 100000}],
        source="eltdx_batch", collector_id="integration")
    minute_cache = RealtimeMinuteCache(backend)
    minute_cache.write(date, "000001", pd.DataFrame([{"time": "09:31:00", "close": 10}]))
    other = RedisStateBackend(URL, backend.prefix)
    try:
        reader = ReadOnlyMarketData(cache_dir=tmp_path, provider=RedisQuoteProvider(
            cache=RealtimeQuoteCache(backend=other, snapshot_repository=RealtimeSnapshotRepository(backend=other)),
            minute_cache=RealtimeMinuteCache(other)))
        assert reader.get_quote_snapshots(["000001"])["000001"]["last_price"] == 10
        assert len(reader.get_realtime_snapshot_ticks("000001", date)) == 1
        assert len(reader.get_minute_bars_live("000001", date)) == 1
        assert journal.health()["pending"] == 1
        assert journal.archive_once() == 1
    finally:
        other.client.close()


def test_real_strategy_task_lease_has_single_owner(backend, monkeypatch):
    import core.infrastructure.shared_state as state
    monkeypatch.setattr(state, "_BACKEND", backend)
    first = TaskLease.acquire("realtime_strategy", 30)
    assert first is not None
    try:
        assert first.is_owner()
        assert TaskLease.acquire("realtime_strategy", 30) is None
    finally:
        first.release()
    restarted = TaskLease.acquire("realtime_strategy", 30)
    assert restarted is not None
    restarted.release()


def test_independent_strategy_cli_consumes_only_prepared_redis_inputs(backend, tmp_path):
    from copy import deepcopy
    from datetime import datetime
    from test_market_process_pipeline import bundle
    from core.realtime.collection_plan import DailyCollectionPlan
    from core.realtime.market_events import publish_revision
    from core.realtime.strategy_runtime import RESULT_KEY, HEALTH_KEY
    from backtest.trade_calendar import TradeCalendar
    now = shanghai_now()
    date = now.strftime("%Y%m%d")
    data = deepcopy(bundle())
    data.update(market_date=date, candidate_date=TradeCalendar().prev(date))
    plans = DailyCollectionPlan(tmp_path, backend)
    plans.freeze(data, now=now.replace(hour=0, minute=0, second=0, microsecond=0))
    publish_revision(backend, date, kind="quotes")
    env = {**os.environ, "REDIS_URL": URL, "REDIS_KEY_PREFIX": backend.prefix,
           "WEB_DATA_DIR": str(tmp_path), "CACHE_DIR": str(tmp_path / "cache"), "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-X", "utf8", "scripts/run_realtime_strategy.py", "--once"],
        env=env, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    payload = backend.get_json(RESULT_KEY + date)
    assert payload and len(payload["rows"]) == 35
    assert all(row["confirm_status"] == "observe" for row in payload["rows"])
    assert backend.get_json(HEALTH_KEY)["completed_cycles"] == 1
    # The CLI did not create a replacement screening or collection universe.
    assert plans.load(date)["plan_id"] == payload["collection_plan_id"]
