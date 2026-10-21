from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace

import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
from core.signals.sector_sync import constituent_minute_prices, make_constituent_sync_checker


DATE = "20261021"


def quote(code="600000", time="09:35:58", **kwargs):
    return {"code": code, "date": DATE, "time": time, "last_price": 10.2,
            "open_price": 10, "pre_close": 10, "source": "eltdx_batch",
            "received_at": f"2026-10-21T{time}+08:00", **kwargs}


def test_execution_quotes_do_not_wait_for_constituent_fetch(tmp_path, monkeypatch):
    import core.realtime.eltdx_collector as module
    started, release = Event(), Event()
    calls = []

    class SlowConstituents:
        def __init__(self, **_kwargs):
            pass

        def get_quote_snapshots(self, codes):
            started.set()
            release.wait(3)
            return {code: quote(code) for code in codes}

    collector = module.EltdxQuoteCollector()
    monkeypatch.setattr(module, "EltdxProvider", SlowConstituents)
    collector.targets = lambda *, minutes_only=False: ["002242"] if minutes_only else ["600000", "002242"]
    collector.provider = SimpleNamespace(get_quote_snapshots=lambda codes: {code: quote(code) for code in codes})
    collector.quote_cache = SimpleNamespace(write_batch=lambda rows, **kw: (
        calls.append(list(rows)) or {"ok": True, "count": 1}), backend=MemoryStateBackend())
    collector._schedule_minute_sync = lambda codes: None
    collector.sector_minutes = SectorQuoteMinuteRepository(tmp_path, backend=MemoryStateBackend())
    try:
        result = collector.collect_once()
        assert started.wait(.5)
        assert not release.is_set()
        assert result["quote_lane"] == "execution_priority"
        assert result["requested_count"] == 1
        assert calls[0][0]["code"] == "002242"
    finally:
        release.set()
        collector._constituent_thread.join(5)
    assert not collector._constituent_thread.is_alive()


def test_constituent_chunks_publish_before_slow_chunk_finishes(tmp_path, monkeypatch):
    import core.realtime.eltdx_collector as module
    slow, release, published = Event(), Event(), Event()

    class Provider:
        def __init__(self, **_kwargs):
            pass

        def get_quote_snapshots(self, codes):
            if codes[0] == "000081":
                slow.set()
                release.wait(3)
            return {code: quote(code) for code in codes}

    collector = module.EltdxQuoteCollector()
    monkeypatch.setattr(module, "EltdxProvider", Provider)
    collector._archive_sector_minutes = lambda rows: None
    collector.quote_cache = SimpleNamespace(write_batch=lambda rows, **kw: (
        published.set() or {"count": len(list(rows))}))
    collector._schedule_constituent_sync([f"{i:06d}" for i in range(1, 82)])
    try:
        assert slow.wait(.5)
        assert published.wait(.5)
        assert collector._constituent_thread.is_alive()
    finally:
        release.set()
        collector._constituent_thread.join(5)


def test_quote_minutes_are_actual_points_not_bars_or_future_prices(tmp_path):
    backend = MemoryStateBackend()
    repo = SectorQuoteMinuteRepository(tmp_path, backend=backend)
    now = datetime(2026, 10, 21, 9, 35, 59)
    assert repo.record_batch([quote()], now=now) == 0
    assert repo.read_prices(DATE, ["600000"]) == {}
    assert repo.record_batch([], now=now + timedelta(seconds=1)) == 1
    assert repo.read_prices(DATE, ["600000"], until="09:35:00") == {}
    assert repo.read_prices(DATE, ["600000"], until="09:36:00") == {"600000": {"09:36:00": 10.2}}
    # A new reader can use the durable archive after Redis expiry/restart.
    reader = SectorQuoteMinuteRepository(tmp_path, backend=MemoryStateBackend())
    assert reader.read_prices(DATE, ["600000"]) == {"600000": {"09:36:00": 10.2}}
    assert reader.read_prices("20261022", ["600000"]) == {}
    assert not (tmp_path / "stock/tick").exists()


def test_redis_archive_updates_only_one_minute_field_and_preserves_newer_point(tmp_path):
    class Redis:
        def __init__(self):
            self.hashes, self.operations = {}, []

        def pipeline(self, transaction=False):
            database = self

            class Pipeline:
                def __init__(self):
                    self.commands = []

                def __getattr__(self, name):
                    def queue(*args):
                        self.commands.append((name, args))
                        return self
                    return queue

                def execute(self):
                    results = []
                    for name, args in self.commands:
                        database.operations.append(name)
                        values = database.hashes.setdefault(args[0], {})
                        if name == "hget":
                            results.append(values.get(args[1]))
                        elif name == "hvals":
                            results.append(list(values.values()))
                        elif name == "hset":
                            values[args[1]] = args[2]
                            results.append(1)
                        elif name == "expire":
                            assert args[1] == 172800
                            results.append(True)
                        else:
                            pytest.fail(f"Unexpected growing JSON update: {name}")
                    return results

            return Pipeline()

    backend = MemoryStateBackend()
    backend.client = Redis()
    repo = SectorQuoteMinuteRepository(tmp_path, backend=backend)
    repo.record_batch([quote()], now=datetime(2026, 10, 21, 9, 36))
    repo.record_batch([quote(time="09:35:57", last_price=9)], now=datetime(2026, 10, 21, 9, 36))
    remote = SectorQuoteMinuteRepository(tmp_path / "remote-node", backend=backend)
    assert remote.read_prices(DATE, ["600000"]) == {"600000": {"09:36:00": 10.2}}
    assert backend.client.operations.count("hset") == 1


@pytest.mark.parametrize("fields", [
    {"is_stale": True}, {"quality_ok": False}, {"date": "20261020"},
    {"source": "synthetic"}, {"received_at": "2026-10-21T09:30:00+08:00"},
    {"last_price": float("nan")}, {"last_price": -1},
])
def test_bad_quote_cannot_become_sector_history(tmp_path, fields):
    repo = SectorQuoteMinuteRepository(tmp_path, backend=MemoryStateBackend())
    repo.record_batch([quote(**fields)], now=datetime(2026, 10, 21, 9, 36))
    assert repo.read_prices(DATE, ["600000"]) == {}


def test_sector_quotes_only_fill_observed_minutes_and_use_quote_reference():
    from core.signals.opening import sector_reference_rows
    points = {c: {"09:33:00": 10, "09:36:00": 10.2} for c in ["600000", "000001"]}
    minute_prices = constituent_minute_prices({}, points)
    checker, detail = make_constituent_sync_checker(code="002242", names=["sector"],
        members={"sector": list(points)}, frames={}, minute_prices=minute_prices,
        previous=sector_reference_rows({}, {c: {"pre_close": 10} for c in points}),
        opening_prices={c: 10 for c in points})
    assert checker("09:36:00") is True
    assert detail["coverage"] == 1
    assert detail["missing_previous_close_codes"] == []
    assert checker("09:35:00") is None
    assert detail["observed_members"] == 0
    assert points == {c: {"09:33:00": 10, "09:36:00": 10.2} for c in points}
    assert detail["missing_minute_codes"] == sorted(points)

    from core.realtime.day_replay import _ReplayEntryService
    service = _ReplayEntryService({}, {}, {"sector": list(points)}, {}, SimpleNamespace(),
        {c: 10 for c in points}, opening_evidence={c: {"pre_close": 10} for c in points}, sector_quote_minutes=points)
    replay_checker, replay_detail = service._sector_checker({"sector_names": ["sector"]}, "002242", [], {}, {}, DATE)
    assert replay_checker("09:36:00") is True
    assert replay_detail["missing_previous_close_codes"] == []


def test_sector_points_do_not_supply_missing_candidate_bars():
    from core.realtime.day_replay import replay_minutes, replay_saved_inputs
    report = replay_minutes(request={"market_date": DATE, "until": "09:40:00", "profile": "decision_pool"},
        candidate_date="20261020", rows=[{"code": "002242"}], frames={}, previous={"002242": {"close": 10}},
        opening_prices={"002242": 10}, sector_quote_minutes={"002242": {"09:36:00": 10.2}},
        risk_config={"three_board_feedback_enabled": False})
    assert report["stocks"][0]["status"] == "missing_evidence"
    assert not report["complete"]
    saved = replay_saved_inputs(report["replay_inputs"])
    assert saved["input_hash"] == report["input_hash"]
    assert saved["replay_inputs"]["sector_quote_minutes"] == {"002242": {"09:36:00": 10.2}}


def test_observation_audit_is_deduplicated_and_not_a_buy(tmp_path):
    from core.operations.evidence import record_runtime_observations
    from core.operations.ledger import TradingLedger
    from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
    from scripts.diagnose_intraday_signal import runtime_evidence
    ledger = TradingLedger(tmp_path / "holding.db")
    row = {"signal_id": "test-signal", "signal_evidence": {"code": "002242", "market_date": DATE,
        "profile": "leader_pool", "status": "observe", "data_status": "quote_stale",
        "trigger_reason": "实时行情已过期，拒绝确认买点", "quote_stale_seconds": 18,
        "quote_received_at": "2026-10-21T09:35:40+08:00"}}
    payload = {"market_date": DATE, "rows": [row]}
    now = datetime(2026, 10, 21, 9, 36)
    assert record_runtime_observations(ledger, payload, now=now) == 1
    assert record_runtime_observations(ledger, payload, now=now + timedelta(seconds=3)) == 0
    assert record_runtime_observations(ledger, payload, now=now + timedelta(minutes=1)) == 1
    assert record_runtime_observations(ledger, payload, now=now + timedelta(days=1)) == 0
    recorded = runtime_evidence(ledger.path, "signal_evidence", DATE, ["002242"])
    assert len(recorded["rows"]) == 2
    report = attach_runtime_diagnostics({"market_date": DATE, "until": "10:00:00", "stocks": [{"code": "002242"}],
                                         "recorded_runtime": {"ledger": recorded}})
    diagnostic = report["stocks"][0]["runtime_diagnostic"]
    assert diagnostic["state"] == "recorded_without_confirmation"
    assert diagnostic["last_observation"]["data_status"] == "quote_stale"
    assert diagnostic["last_observation"]["quote_stale_seconds"] == 18
    assert not diagnostic["confirmed"] and not diagnostic["notification_sent"]
    assert ledger.timeline("test-signal")["evidence"] == {}


def test_late_chunk_cannot_overwrite_newer_cached_quote(tmp_path):
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.session_open_repository import SessionOpenRepository
    backend = MemoryStateBackend()
    cache = RealtimeQuoteCache(backend=backend, snapshot_repository=SimpleNamespace(append_batch=lambda *a: None),
                              opening_repository=SessionOpenRepository(tmp_path, backend=backend))
    latest = quote(time="09:36:02")
    assert cache.write_batch([latest], source="eltdx_batch", collector_id="test")["ok"]
    result = cache.write_batch([quote()], source="eltdx_batch", collector_id="test")
    assert not result["ok"]
    assert cache.read_one("600000")["received_at"] == latest["received_at"]
