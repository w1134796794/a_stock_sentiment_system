from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.collection_plan import DailyCollectionPlan
from core.realtime.market_journal import MarketJournal
from core.realtime.snapshot_repository import RealtimeSnapshotRepository
from core.realtime.trading_session import shanghai_now
from risk.risk_config import RiskConfig


DAY = "20261023"
PREOPEN = datetime(2026, 10, 23, 9, 0)


def bundle(sector_mode="breadth_required"):
    def row(i):
        return {"code": f"{i:06d}", "name": f"stock{i}", "resonance_sectors": "board",
                "concept_tags": ["concept"]}
    return {"candidate_date": "20261022", "market_date": DAY, "bundle_id": "test-bundle",
        "health": {"ok": True}, "inputs": {
            "leader_pools": {"10": {"rows": [row(i) for i in range(1, 31)]}},
            "candidates": [row(i) for i in range(31, 36)], "profiles": [],
            "risk_config": asdict(RiskConfig(entry_sector_mode=sector_mode)), "holdings": [{"code": "600000"}],
            "artifacts": {"fetch": {"fetched_at": "2026-10-22T18:00:00+08:00"}},
            "sector_codes": {"board": "885001", "concept": "885002"},
            "sector_members": {"board": ["000001", "000002", "600001"], "concept": ["600002"]},
            "daily_rows": [], "minute_amount_profile": {"profiles": {"high": {"09:36:00": {"fraction": .2, "samples": 200}}}}}}


def make_plans(tmp_path):
    backend = MemoryStateBackend()
    plans = DailyCollectionPlan(tmp_path, backend)
    return plans, backend


def test_preopen_plan_is_exact_deduplicated_union_and_contains_concepts(tmp_path):
    plans, backend = make_plans(tmp_path)
    plan = plans.freeze(bundle(), now=PREOPEN)
    assert plan["stock_count"] == 35
    assert plan["leader_count"] == 30 and plan["decision_count"] == 5
    assert plan["sector_member_count"] == 4
    assert plan["uncollected_holdings"] == ["600000"]
    assert set(plan["rows"][0]["sectors"]) == {"board", "concept"}
    assert plans.bundle(DAY) == bundle()
    changed = bundle()
    changed["inputs"]["candidates"][0]["code"] = "000001"
    with pytest.raises(ValueError, match="already frozen"):
        plans.freeze(changed, now=PREOPEN)
    other = DailyCollectionPlan(tmp_path / "dedupe", MemoryStateBackend())
    deduped = other.freeze(changed, now=PREOPEN)
    assert deduped["stock_count"] == 34
    assert deduped["rows"][0]["sources"] == ["leader_pool", "decision_pool"]


@pytest.mark.parametrize("change", ["late", "sector", "members", "stock_metadata"])
def test_incomplete_or_late_plan_cannot_be_created(tmp_path, change):
    plans, _ = make_plans(tmp_path)
    data = bundle()
    if change == "sector":
        data["inputs"]["sector_codes"] = {}
    if change == "members":
        data["inputs"]["sector_members"]["concept"] = []
    if change == "stock_metadata":
        data["inputs"]["candidates"][0].update(resonance_sectors="", concept_tags=[])
    with pytest.raises(ValueError):
        plans.freeze(data, now=datetime(2026, 10, 23, 9, 25) if change == "late" else PREOPEN)


def test_plan_survives_process_restart_and_tamper_is_rejected(tmp_path):
    plans, _ = make_plans(tmp_path)
    plans.freeze(bundle(), now=PREOPEN)
    restarted = DailyCollectionPlan(tmp_path, MemoryStateBackend())
    assert restarted.load(DAY)["stock_count"] == 35
    path = plans.root / f"{DAY}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rows"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid"):
        restarted.load(DAY)


def test_decision_collection_is_not_truncated_by_page_display_limit(tmp_path):
    from core.signals.production_rules import production_candidates
    plans, _ = make_plans(tmp_path)
    data = bundle()
    sample = data["inputs"]["candidates"][0]
    data["inputs"]["candidates"] = [{**sample, "code": f"{i:06d}"} for i in range(31, 56)]
    plan = plans.freeze(data, now=PREOPEN)
    assert plan["decision_count"] == 25 and plan["stock_count"] == 55
    assert len(production_candidates(data["inputs"])) == 55


def test_collector_only_downloads_dated_stock_plan_not_holdings_or_watchlists(tmp_path):
    from core.realtime.eltdx_collector import EltdxQuoteCollector
    plans, _ = make_plans(tmp_path)
    plans.freeze(bundle(), now=PREOPEN)
    worker = EltdxQuoteCollector.__new__(EltdxQuoteCollector)
    worker.plans, worker.fixed_codes = plans, []
    worker.clock = lambda: PREOPEN
    assert len(worker.targets(minutes_only=True)) == 35
    assert set(worker.targets()) - set(worker.targets(minutes_only=True)) == {"600001", "600002"}
    assert "600000" not in worker.targets()
    worker.clock = lambda: PREOPEN + timedelta(days=1)
    assert worker.targets() == []


def snapshot(at, code="000001"):
    return {"code": code, "date": DAY, "time": at.strftime("%H:%M:%S"), "received_at": at.isoformat(),
            "last_price": 10, "open_price": 10, "pre_close": 9.8, "delta_volume": 10,
            "vol_hand": 1000, "amount_yuan": 1000000, "source": "eltdx_batch", "quality_ok": True}


def test_hot_window_is_time_based_not_number_of_rows_and_expires_when_idle():
    now = [datetime(2026, 10, 23, 10, 0)]
    backend = MemoryStateBackend()
    repository = RealtimeSnapshotRepository(backend=backend, clock=lambda: now[0], max_items=20)
    points = [snapshot(now[0] - timedelta(seconds=i)) for i in range(200)]
    repository.append_batch(DAY, points)
    assert len(repository.read(DAY, "000001")) == 180  # even at a faster-than-3s arrival rate
    now[0] += timedelta(seconds=181)
    assert repository.read(DAY, "000001") == []


def test_hot_cache_eviction_does_not_lose_spooled_data_and_archive_is_idempotent(tmp_path):
    journal = MarketJournal(tmp_path)
    rows = [snapshot(datetime(2026, 10, 23, 9, 30) + timedelta(seconds=i * 3)) for i in range(80)]
    journal.append("snapshot", DAY, rows)
    journal.append("snapshot", DAY, rows)
    assert journal.health()["pending"] == 80
    restarted = MarketJournal(tmp_path)
    assert restarted.archive_once(limit=40) == 40
    assert restarted.health()["pending"] == 40
    assert restarted.archive_once() == 40
    assert restarted.archive_once() == 0
    # Replaying a committed event after a crash between archive and spool commits.
    restarted.append("snapshot", DAY, rows)
    restarted.archive_once()
    with sqlite3.connect(tmp_path / f"market_{DAY}.sqlite3") as con:
        assert con.execute("SELECT count(*) FROM market_events").fetchone()[0] == 80


def test_failed_archive_keeps_durable_pending_rows(tmp_path, monkeypatch):
    journal = MarketJournal(tmp_path)
    journal.append("snapshot", DAY, [snapshot(datetime(2026, 10, 23, 9, 30))])
    original = sqlite3.connect
    def fail_archive(path, **kwargs):
        if str(path).endswith(f"market_{DAY}.sqlite3"):
            raise sqlite3.OperationalError("disk unavailable")
        return original(path, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", fail_archive)
    with pytest.raises(sqlite3.OperationalError):
        journal.archive_once()
    assert journal.health()["pending"] == 1


def test_spool_is_readable_by_an_independent_process(tmp_path):
    journal = MarketJournal(tmp_path)
    journal.append("snapshot", DAY, [snapshot(datetime(2026, 10, 23, 9, 30))])
    result = subprocess.run([sys.executable, "-X", "utf8", "-c",
        "from core.realtime.market_journal import MarketJournal; import sys; print(MarketJournal(sys.argv[1]).archive_once())",
        str(tmp_path)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("1")
    assert journal.health()["pending"] == 0


def test_quote_is_durable_before_hot_publication_and_event_follows_publication(tmp_path):
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.market_events import REVISION_KEY
    backend = MemoryStateBackend()
    order = []
    def append(*args):
        assert not backend.get_json(REVISION_KEY)
        assert not backend.get_json(RealtimeQuoteCache._quote_key("000001"))
        order.append("journal")
    def hot(*args):
        assert order == ["journal"]
        order.append("hot")
    cache = RealtimeQuoteCache(backend=backend, journal=SimpleNamespace(append=append),
        snapshot_repository=SimpleNamespace(append_batch=hot), opening_repository=SimpleNamespace(record_batch=lambda rows, **kw: None))
    cache.write_batch([snapshot(datetime.now())], source="eltdx_batch", collector_id="test")
    assert order == ["journal", "hot"] and backend.get_json(REVISION_KEY)


def test_read_only_boundary_never_calls_external_provider_on_cache_miss(monkeypatch, tmp_path):
    from core.realtime.read_only_market import ReadOnlyMarketData
    from core.data.providers.redis_quote_provider import RedisQuoteProvider
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.minute_cache import RealtimeMinuteCache
    from core.data.data_manager_stock import StockDataManager
    backend = MemoryStateBackend()
    provider = RedisQuoteProvider(cache=RealtimeQuoteCache(backend=backend), minute_cache=RealtimeMinuteCache(backend))
    reader = ReadOnlyMarketData(provider=provider, cache_dir=tmp_path)
    assert reader.get_quote_snapshots(["000001"]) == {}
    assert reader.get_minute_bars_live("000001", DAY).empty
    assert reader.get_auction_data("000001", DAY) == {}
    assert reader.get_all_stocks_daily("20261022").empty
    dm = StockDataManager.__new__(StockDataManager)
    dm._get_realtime_quote_provider = lambda: provider
    dm._get_eltdx_provider = lambda: pytest.fail("A cache miss must never open eltdx")
    assert dm.get_minute_bars_live("000001", DAY).empty


def test_default_quote_and_signal_services_use_storage_only_reader():
    from core.realtime.read_only_market import ReadOnlyMarketData
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    from core.realtime.quote_service import RealtimeQuoteService
    for service in (RealtimeQuoteService(), RealtimeEntrySignalService()):
        assert isinstance(service._ensure_data_manager(), ReadOnlyMarketData)


def test_strategy_coalesces_revisions_and_does_not_prepare_inputs(tmp_path):
    from core.realtime.strategy_runtime import StrategyRuntime
    from core.realtime.market_events import REVISION_KEY
    plans, backend = make_plans(tmp_path)
    # Missing preparation fails closed, it does not create a pool or call a provider.
    from core.operations.ledger import TradingLedger
    worker = StrategyRuntime(backend=backend, plans=plans, ledger=TradingLedger(tmp_path / "ledger.sqlite3"),
                             clock=lambda: datetime(2026, 10, 23, 9, 36))
    with pytest.raises(ValueError, match="盘前"):
        worker.run_once()
    worker._date, worker._version = DAY, "v1"
    backend.set_json(REVISION_KEY, {"market_date": DAY, "id": "v1"})
    worker._load = lambda *args: pytest.fail("No duplicate evaluation without a new data revision")
    assert worker.run_once() is None
    with pytest.raises(RuntimeError, match="Redis"):
        worker.run(once=True)


def test_real_worker_reads_frozen_plan_and_publishes_missing_data_without_network(tmp_path, monkeypatch):
    import core.realtime.strategy_runtime as runtime
    from core.operations.ledger import TradingLedger
    from core.data.providers.eltdx_provider import EltdxProvider
    from core.realtime.market_events import REVISION_KEY
    from core.realtime.signal_event_repository import RealtimeSignalEventRepository
    plans, backend = make_plans(tmp_path)
    plans.freeze(bundle(), now=PREOPEN)
    monkeypatch.setattr(EltdxProvider, "get_quote_snapshots", lambda *a: pytest.fail("Strategy may not request quotes"))
    monkeypatch.setattr(EltdxProvider, "get_minute_bars", lambda *a: pytest.fail("Strategy may not request minutes"))
    monkeypatch.setattr(RealtimeSignalEventRepository, "record_payload", lambda *a: 0)
    monkeypatch.setattr(RealtimeSignalEventRepository, "merge_history", lambda self, value: value)
    now = datetime(2026, 10, 23, 9, 36)
    monkeypatch.setattr(runtime, "shanghai_now", lambda value=None: shanghai_now(value or now))
    worker = runtime.StrategyRuntime(backend=backend, plans=plans, ledger=TradingLedger(tmp_path / "ledger.sqlite3"), clock=lambda: now)
    backend.set_json(REVISION_KEY, {"market_date": DAY, "id": "v1"})
    payload = worker.run_once()
    assert len(payload["rows"]) == 35
    assert all(row["confirm_status"] == "observe" for row in payload["rows"])
    assert worker.run_once() is None
    leader = runtime.stored_strategy_view(DAY, profile="leader_pool", backend=backend)
    decisions = runtime.stored_strategy_view(DAY, profile="decision_pool", backend=backend)
    assert len(leader["rows"]) == 30 and len(decisions["rows"]) == 5
    backend.set_json(REVISION_KEY, {"market_date": DAY, "id": "v2"})
    assert worker.run_once()["market_revision"] == "v2"
    assert worker._completed == 2


@pytest.mark.parametrize("at,expected", [("08:59:00", False), ("09:00:00", True), ("09:24:59", True),
                                          ("09:25:00", False), ("10:00:00", False)])
def test_preopen_scheduler_only_prepares_before_cutoff(monkeypatch, at, expected):
    from core.automation.internal_scheduler import InternalScheduler
    scheduler = InternalScheduler()
    scheduler.calendar = SimpleNamespace(is_real=True, is_trade_date=lambda date: True)
    scheduler._now = lambda: datetime.fromisoformat("2026-10-23T" + at)
    scheduler.preopen_time = "09:00"
    dispatched = []
    scheduler._dispatch_job = lambda name, target: (dispatched.append(name) or True)
    monkeypatch.setattr(DailyCollectionPlan, "load", lambda *a: None)
    assert scheduler._recover_due_preopen_job() is expected
    assert dispatched == (["preopen"] if expected else [])
