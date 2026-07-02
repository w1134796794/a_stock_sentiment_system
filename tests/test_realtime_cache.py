import threading
import time
from concurrent.futures import ThreadPoolExecutor

from web.realtime_cache import RealtimePayloadCache
from core.infrastructure.shared_state import MemoryStateBackend


def test_realtime_cache_collapses_concurrent_loads():
    cache = RealtimePayloadCache(ttl_seconds=5, wait_timeout_seconds=2)
    barrier = threading.Barrier(8)
    calls = 0
    calls_lock = threading.Lock()

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.08)
        return {"price": 10.5}

    def read():
        barrier.wait()
        return cache.get_or_load(("quote", "000001"), loader)

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: read(), range(8)))

    assert calls == 1
    assert rows == [{"price": 10.5}] * 8


def test_realtime_cache_keeps_last_value_when_refresh_fails():
    cache = RealtimePayloadCache(ttl_seconds=5)
    cache.refresh("overlay", lambda: {"rows": [1]})

    value = cache.refresh("overlay", lambda: (_ for _ in ()).throw(RuntimeError("source down")))

    assert value == {"rows": [1]}
    assert cache.stats()["last_error"] == "source down"


def test_realtime_cache_is_shared_across_worker_instances():
    backend = MemoryStateBackend("test")
    backend.is_shared = True
    caches = [
        RealtimePayloadCache(ttl_seconds=5, wait_timeout_seconds=2, backend=backend)
        for _ in range(2)
    ]
    barrier = threading.Barrier(8)
    calls = 0
    calls_lock = threading.Lock()

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.08)
        return {"price": 12.34}

    def read(index):
        barrier.wait()
        return caches[index % 2].get_or_load(("quote", "600000"), loader)

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(read, range(8)))

    assert calls == 1
    assert rows == [{"price": 12.34}] * 8
