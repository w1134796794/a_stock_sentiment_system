"""Offline contracts + durable end-to-end evidence collection/replay tests."""

import json
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from core.data.providers.eltdx_evidence import trade_row
from core.data.providers.eltdx_provider import EltdxProvider
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.evidence_lane import EvidenceLane
from core.realtime.evidence_replay import load_evidence_events, replay_evidence
from core.realtime.evidence_store import EvidenceStore
from core.realtime.market_evidence import EvidenceResult, normalize_depth
from core.realtime.market_journal import MarketJournal
from core.realtime.trading_session import shanghai_now

DATE = "20261023"
STAMP = "2026-10-23T09:36:00+08:00"


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def provider(**parts):
    client = Client()
    for key, value in parts.items():
        setattr(client, key, value)
    api = EltdxProvider()
    api._client = lambda: client
    api._is_today = lambda _: False
    return api


def tick(price=10, volume=1, *, time="09:36", side="buy", kind="trade"):
    from eltdx.models import TradeTick

    return TradeTick(
        index=0,
        absolute_index=0,
        time_minutes=576,
        time_label=time,
        trade_datetime=datetime(2026, 10, 23, 9, 36),
        price=price,
        price_milli=int(price * 1000),
        volume=volume,
        order_count=1,
        status_raw=0,
        side=side,
        price_delta_raw=0,
        price_acc_raw=0,
        event_kind=kind,
    )


def pages(groups):
    calls = []

    def history(code, date, **kwargs):
        calls.append((code, date, kwargs))
        group = groups[len(calls) - 1]
        return NS(code="000001", trading_date=datetime.strptime(DATE, "%Y%m%d").date(), ticks=group)

    return provider(trades=NS(history=history)), calls


def result(kind="auction_series", rows=None, **kwargs):
    return EvidenceResult(kind, "000001", DATE, rows=rows or [], received_at=STAMP, **kwargs)


def event(value, kind=None, receipt=None):
    return dict(
        event_id="id",
        code=value["code"],
        kind=kind or value["kind"],
        received_at=receipt or value["received_at"],
        payload=value,
    )


def test_history_auction_date_not_silently_today():
    calls = []
    point = NS(
        price=10, matched_volume=5, time_seconds=9 * 3600 + 24 * 60 + 3, unmatched_volume=2, unmatched_direction_raw=1
    )

    def series(code, date):
        calls.append((code, date))
        return NS(code="000001", trading_date=datetime(2026, 10, 23).date(), points=[point])

    api = provider(auctions=NS(series=series))
    data = api.get_auction_evidence("000001", DATE)
    assert calls == [("sz000001", "2026-10-23")]
    row = data.rows[0]
    assert row["event_time"].endswith("09:24:03+08:00") and row["time_precision"] == "second"
    assert row["matched_amount_estimated_yuan"] == 5000
    assert row["event_kind"] == "auction_snapshot"
    frame = api.get_auction_series("000001", DATE)
    assert len(frame) == 1 and calls[-1][1] == "2026-10-23"


def test_legacy_history_never_calls_today():
    called = []
    api = provider(get_call_auction=lambda code: called.append(code))
    assert api.get_auction_series("000001", DATE).empty
    assert called == []


@pytest.mark.parametrize(
    "exception,status",
    [
        (TimeoutError("timeout"), "network_error"),
        (NotImplementedError("no history"), "unsupported"),
        (ValueError("bad data"), "parse_error"),
        (RuntimeError("protocol failed"), "request_error"),
    ],
)
def test_errors_have_explicit_quality(exception, status):
    def fail(*args):
        raise exception

    api = provider(auctions=NS(series=fail))
    response = api.get_auction_evidence("000001", DATE)
    assert response.status == status and not response.rows and response.reasons


def test_auction_mismatched_date_rejected():
    api = provider(
        auctions=NS(series=lambda *args: NS(code="000001", trading_date=datetime(2026, 10, 22).date(), points=[]))
    )
    assert api.get_auction_evidence("000001", DATE).status == "parse_error"


@pytest.mark.parametrize(
    "kind,time,status",
    [
        ("opening_match", "09:25", "ok"),
        ("auction_snapshot", "09:25", "parse_error"),
        ("trade", "09:25", "parse_error"),
        ("opening_match", "09:26", "parse_error"),
    ],
)
def test_formal_opening_is_not_auction_curve(kind, time, status):
    api = provider(trades=NS(opening_match_history=lambda *args, **kwargs: tick(kind=kind, time=time)))
    data = api.get_opening_match_evidence("000001", DATE)
    assert data.status == status
    if status == "ok":
        assert data.rows[0]["amount_yuan"] == 1000


def test_sdk_model_properties_and_unknown_direction():
    row = trade_row(tick(volume=5, side="neutral"), DATE)
    assert row["amount_yuan"] == 5000 and row["volume_shares"] == 500
    assert row["time_precision"] == "minute" and row["side"] == "unknown"
    assert row["event_time"].endswith("09:36+08:00")
    assert trade_row(tick(kind="auction_snapshot"), DATE)["amount_yuan"] is None


def test_pagination_preserves_same_minute_duplicates_and_reverses_pages():
    api, calls = pages([[tick(11), tick(11)], [tick(10)], []])
    data = api.get_trade_evidence("000001", DATE)
    assert data.status == "ok" and data.coverage["complete"]
    assert [r["price"] for r in data.rows] == [10, 11, 11]
    assert len({r["trade_id"] for r in data.rows}) == 3
    assert [call[2]["start"] for call in calls] == [0, 2, 3]
    assert data.coverage["next_sequence"] == 3


def test_incremental_overlap_assigns_only_new_sequence():
    api, _ = pages([[tick(10), tick(11)], []])
    first = api.get_trade_evidence("000001", DATE)
    api, _ = pages([[tick(11), tick(12)], [tick(10)]])
    second = api.get_trade_evidence("000001", DATE, anchor=first.coverage["anchor"], next_sequence=2)
    assert second.status == "ok" and second.coverage["overlap_verified"]
    assert [r["price"] for r in second.rows] == [12]
    assert second.rows[0]["sequence"] == 2
    assert second.coverage["next_sequence"] == 3


@pytest.mark.parametrize("anchor,reason", [(None, "pagination_limit"), (["missing"], "overlap_missing")])
def test_partial_pages_do_not_advance_cursor(anchor, reason):
    api, _ = pages([[tick(10)], [tick(11)], [tick(12)]])
    data = api.get_trade_evidence("000001", DATE, anchor=anchor, next_sequence=7)
    assert data.status == "partial" and reason in data.reasons
    assert data.coverage["next_sequence"] == 7 and not data.coverage["sequence_verified"]
    assert all("trade_id" not in row for row in data.rows)


def test_ambiguous_overlap_is_not_guessed():
    row = trade_row(tick(), DATE)
    api, _ = pages([[tick(), tick()]])
    data = api.get_trade_evidence("000001", DATE, anchor=[row["fingerprint"]])
    assert data.status == "partial" and data.reasons == ["ambiguous_overlap"]


def test_depth_units_one_sided_and_missing_are_separate():
    bids = [NS(price=10 - i * 0.01, volume=2) for i in range(5)]
    asks = [NS(price=10.1 + i * 0.01, volume=3) for i in range(5)]
    book = normalize_depth(NS(buy_levels=bids, sell_levels=asks))
    assert book["quality_status"] == "ok" and book["spread_yuan"] == pytest.approx(0.1)
    assert book["buy_levels"][0]["volume_shares"] == 200
    assert book["imbalance"] == pytest.approx(-0.2)
    one = normalize_depth(NS(buy_levels=bids, sell_levels=[NS(price=0, volume=0)] * 5))
    assert one["quality_status"] == "ok" and one["book_state"] == "one_sided" and one["spread_yuan"] is None
    bad = normalize_depth(NS(buy_levels=[NS(price=float("nan"), volume=None)], sell_levels=[]))
    assert bad["quality_status"] == "partial" and bad["buy_levels"][0]["price"] is None


def test_history_depth_explicitly_unsupported():
    assert provider().get_depth_evidence("000001", DATE).status == "unsupported"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.realtime.evidence_store.shanghai_now", lambda now=None: shanghai_now(now or datetime.fromisoformat(STAMP))
    )
    return EvidenceStore(MarketJournal(tmp_path / "market_archive"), MemoryStateBackend("evidence-test"))


def test_durable_before_cache_and_cursor_recovery(store):
    api, _ = pages([[tick()], []])
    data = api.get_trade_evidence("000001", DATE)
    data.received_at = STAMP
    original = store.backend.set_json

    def check(key, value, ttl):
        with store.journal.connect() as con:
            assert con.execute("SELECT count(*) FROM market_events WHERE kind='trade_batch'").fetchone()[0] == 1
        original(key, value, ttl)

    store.backend.set_json = check
    store.write(data, plan_id="frozen")
    cursor = store.cursor(DATE, "000001", "frozen")
    assert cursor["next_sequence"] == 1
    store.backend = MemoryStateBackend("fresh-empty-redis")
    assert store.cursor(DATE, "000001", "frozen") == cursor
    store.journal.archive_once()
    assert store.cursor(DATE, "000001", "frozen") == cursor


def test_disk_failure_does_not_publish_or_advance(store, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store.journal, "append", fail)
    with pytest.raises(OSError):
        store.write(result(), plan_id="frozen")
    assert store.backend.get_json(store.key(DATE, "000001", "auction_series")) is None


def test_cache_failure_keeps_durable_evidence_without_publication(store, monkeypatch):
    def fail(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(store.backend, "set_json", fail)
    with pytest.raises(ConnectionError):
        store.write(result(), plan_id="frozen")
    events, errors = load_evidence_events(store.journal.root.parent, DATE)
    assert not errors and len(events) == 1
    replay = replay_evidence(events, date=DATE, until=STAMP, plan_id="frozen")
    assert replay["missing_publication_count"] == 1 and not replay["evidence"]


def test_replay_publication_cutoff_scope_and_archive_idempotence(store, monkeypatch):
    monkeypatch.setattr(
        "core.realtime.evidence_store.shanghai_now",
        lambda *args: shanghai_now(datetime.fromisoformat("2026-10-23T09:36:03+08:00")),
    )
    store.write(result(rows=[{"event_time": "2026-10-23T09:24:00+08:00"}]), plan_id="frozen")
    events, _ = load_evidence_events(store.journal.root.parent, DATE)
    assert not replay_evidence(events, date=DATE, until=STAMP, plan_id="frozen")["evidence"]
    end = "2026-10-23T09:36:03+08:00"
    report = replay_evidence(events, date=DATE, until=end, plan_id="frozen")
    assert report["event_counts"] == {"auction_series": 1}
    assert report["evidence"][0]["published_at"] == end
    assert not report["strategy_affected"] and not report["remote_fetch_performed"]
    assert not replay_evidence(events, date=DATE, until=end, plan_id="different")["evidence"]
    assert not replay_evidence(events, date=DATE, until=end, plan_id="frozen", codes=["600000"])["evidence"]
    assert store.journal.archive_once() == 2 and store.journal.archive_once() == 0
    archived, _ = load_evidence_events(store.journal.root.parent, DATE)
    assert replay_evidence(archived, date=DATE, until=end, plan_id="frozen") == report


def test_backfill_isolated_and_checkpointed(tmp_path):
    store = EvidenceStore(MarketJournal(tmp_path / "market_archive" / "backfill"))
    value = result(rows=[dict(event_time="2026-10-23T09:24:00+08:00")])
    value.received_at = "2026-10-24T12:00:00+08:00"
    store.write(value, capture_mode="backfill")
    assert store.journal.evidence_cursor(f"backfill-complete:{DATE}:000001:auction_series")["complete"]
    live, _ = load_evidence_events(tmp_path, DATE)
    assert live == []
    research, _ = load_evidence_events(tmp_path, DATE, research_backfill=True)
    assert not replay_evidence(research, date=DATE, until=STAMP, plan_id="frozen")["evidence"]
    report = replay_evidence(research, date=DATE, until=STAMP, research_backfill=True)
    assert report["research_only"] and len(report["evidence"]) == 1
    assert not replay_evidence(research, date=DATE, until="2026-10-23T09:20:00+08:00", research_backfill=True)[
        "evidence"
    ][0]["rows"]


def test_read_only_loader_never_creates_files(tmp_path):
    assert load_evidence_events(tmp_path, DATE) == ([], [])
    assert list(tmp_path.iterdir()) == []


def test_store_requires_live_plan_and_disallows_backfill_redis(store):
    with pytest.raises(ValueError):
        store.write(result())
    with pytest.raises(ValueError):
        store.write(result(), capture_mode="backfill")


def test_read_age_preserves_partial_quality(store):
    store.write(result(status="partial", reasons=["missing"]), plan_id="frozen")
    row = store.read(DATE, "000001", "auction_series", now=datetime.fromisoformat("2026-10-23T09:37:00+08:00"))
    assert row["is_stale"] and row["quality_status"] == "partial"


def lane(store, api, **kwargs):
    return EvidenceLane(
        store, lambda: api, clock=lambda: datetime.fromisoformat(STAMP), owner=lambda: True, min_free_mb=0, **kwargs
    )


def test_disabled_lane_performs_no_requests(store):
    calls = []
    instance = lane(store, NS(get_trade_evidence=lambda *a, **k: calls.append(a)))
    instance.collect_once(dict(market_date=DATE, plan_id="frozen", rows=[{"code": "000001"}]))
    assert calls == []


def test_enabled_lane_integrates_durable_archive_and_replay(store):
    api, _ = pages([[tick(10), tick(11)], []])
    original = api.get_trade_evidence

    def fixed(*args, **kwargs):
        response = original(*args, **kwargs)
        response.received_at = STAMP
        return response

    api.get_trade_evidence = fixed
    instance = lane(store, api, trades=True)
    report = instance.collect_once(dict(market_date=DATE, plan_id="frozen", rows=[{"code": "000001"}]))
    assert report["status"] == "ok"
    events, errors = load_evidence_events(store.journal.root.parent, DATE)
    report = replay_evidence(events, date=DATE, until=STAMP, plan_id="frozen")
    assert not errors and report["verified_trade_summary"]["count"] == 2
    assert report["verified_trade_summary"]["amount_estimated_yuan"] == 2100


def test_lease_lost_during_network_does_not_publish(store):
    owner = [True]

    def query(*args, **kwargs):
        owner[0] = False
        return result("trade_batch")

    instance = lane(store, NS(get_trade_evidence=query), trades=True)
    instance.owner = lambda: owner[0]
    instance.collect_once(dict(market_date=DATE, plan_id="frozen", rows=[{"code": "000001"}]))
    assert store.journal.health()["pending"] == 0


def test_backpressure_prevents_network(store):
    calls = []
    instance = lane(
        store, NS(get_trade_evidence=lambda *args, **kwargs: calls.append(args)), trades=True, max_pending=0
    )
    report = instance.collect_once(dict(market_date=DATE, plan_id="frozen", rows=[{"code": "000001"}]))
    assert report["status"] == "backpressure" and not calls


def test_live_envelope_rejects_wrong_day_and_naive_time():
    with pytest.raises(ValueError):
        EvidenceResult("depth", "000001", DATE, received_at="2026-10-24T09:36:00+08:00").envelope()
    with pytest.raises(ValueError):
        EvidenceResult("depth", "000001", DATE, received_at="2026-10-23T09:36:00").envelope()


def test_prefetch_dry_run_creates_nothing(tmp_path, monkeypatch, capsys):
    from scripts.prefetch_eltdx_evidence import main

    monkeypatch.setattr(
        sys, "argv", ["prefetch", "--start", DATE, "--end", DATE, "--codes", "000001", "--data-root", str(tmp_path)]
    )
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] and report["task_count"] == 3
    assert list(tmp_path.iterdir()) == []


def test_prefetch_resume_skips_completed_and_keeps_live_untouched(tmp_path, monkeypatch, capsys):
    from scripts.prefetch_eltdx_evidence import main

    calls = []

    def fetch(self, code, date, **kwargs):
        calls.append((code, date))
        return result("opening_match", status="empty")

    monkeypatch.setattr(EltdxProvider, "get_opening_match_evidence", fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prefetch",
            "--start",
            DATE,
            "--end",
            DATE,
            "--codes",
            "000001",
            "--kinds",
            "opening_match",
            "--apply",
            "--data-root",
            str(tmp_path),
        ],
    )
    assert main() == 0
    assert main() == 0 and len(calls) == 1
    assert not (tmp_path / "collection_plans").exists()
    assert not (tmp_path / "market_archive" / "ingestion.sqlite3").exists()
    capsys.readouterr()


def test_live_pagination_shift_is_partial():
    api, _ = pages([[tick(10)], [], [tick(10), tick(11)]])
    # In this fixture both live and historical methods use the controlled pages.
    client = api._client()
    client.trades.today = lambda code, **kwargs: client.trades.history(code, DATE, **kwargs)
    api._is_today = lambda _: True
    response = api.get_trade_evidence("000001", DATE)
    assert response.status == "partial"
    assert response.reasons == ["pagination_shifted_during_read"]
    assert not response.coverage["sequence_verified"]


def test_opening_reference_conflict_never_overwrites_legacy_reference(store):
    key = f"realtime:auction:{DATE}:000001"
    reference = {"开盘价": 9.5, "竞价成交额": 1000}
    store.backend.set_json(key, reference)
    data = result("opening_match", rows=[trade_row(tick(time="09:25", kind="opening_match"), DATE)])
    saved = store.write(data, plan_id="frozen")
    assert saved["quality_status"] == "partial" and "opening_reference_conflict" in saved["quality_reasons"]
    assert store.backend.get_json(key) == reference


def test_optional_depth_survives_quote_cache_hot_window_and_archive(tmp_path, monkeypatch):
    from config import settings
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.snapshot_repository import RealtimeSnapshotRepository

    monkeypatch.setattr(settings, "ELTDX_DEPTH_EVIDENCE_ENABLED", True)
    backend = MemoryStateBackend("depth-preserve")
    now = shanghai_now()
    snapshots = RealtimeSnapshotRepository(backend=backend)
    journal = MarketJournal(tmp_path / "market_archive")
    cache = RealtimeQuoteCache(backend=backend, snapshot_repository=snapshots, journal=journal)
    book = normalize_depth(NS(buy_levels=[NS(price=10, volume=1)] * 5, sell_levels=[NS(price=10.1, volume=1)] * 5))
    row = dict(
        code="000001",
        last_price=10,
        pre_close=9,
        open_price=9.5,
        vol_hand=10,
        amount_yuan=10000,
        date=now.strftime("%Y%m%d"),
        time=now.strftime("%H:%M:%S"),
        received_at=now.isoformat(),
        depth=book,
    )
    assert cache.write_batch([row], source="eltdx_batch", collector_id="test")["ok"]
    assert cache.read_one("000001")["depth"] == book
    assert cache.read_ticks("000001", row["date"])[-1]["depth"] == book
    with journal.connect() as con:
        raw = json.loads(con.execute("SELECT payload FROM market_events WHERE kind='snapshot'").fetchone()[0])
        assert raw["depth"] == book


def test_partial_does_not_replace_durable_trade_cursor(store):
    api, _ = pages([[tick()], []])
    data = api.get_trade_evidence("000001", DATE)
    data.received_at = STAMP
    store.write(data, plan_id="frozen")
    before = store.cursor(DATE, "000001", "frozen")
    store.write(result("trade_batch", status="partial", coverage={"complete": False}), plan_id="frozen")
    assert store.cursor(DATE, "000001", "frozen") == before


def test_replay_rejects_tampered_observation(store):
    store.write(result(), plan_id="frozen")
    events, _ = load_evidence_events(store.journal.root.parent, DATE)
    for item in events:
        if item["kind"] == "auction_series":
            item["payload"]["quality_status"] = "made-up"
    report = replay_evidence(events, date=DATE, until=STAMP, plan_id="frozen")
    assert report["failures"] and not report["evidence"]


def test_async_lane_never_waits_for_blocked_provider(store):
    import time
    from threading import Event

    entered, release = Event(), Event()

    def query(*args, **kwargs):
        entered.set()
        release.wait(5)
        return result("trade_batch", status="empty")

    instance = lane(store, NS(get_trade_evidence=query), trades=True)
    plan = dict(market_date=DATE, plan_id="frozen", rows=[{"code": "000001"}])
    try:
        started = time.monotonic()
        instance.schedule(plan)
        assert time.monotonic() - started < 0.5
        assert entered.wait(2)
        active = instance._thread
        instance.schedule(plan)
        assert instance._thread is active
    finally:
        release.set()
        instance._thread.join(3)
    assert not instance._thread.is_alive()


def test_research_summary_excludes_future_trades(tmp_path):
    store = EvidenceStore(MarketJournal(tmp_path / "market_archive" / "backfill"))
    api, _ = pages([[tick(time="09:36"), tick(price=20, time="10:30")], []])
    data = api.get_trade_evidence("000001", DATE)
    store.write(data, capture_mode="backfill")
    events, _ = load_evidence_events(tmp_path, DATE, research_backfill=True)
    report = replay_evidence(events, date=DATE, until=STAMP, research_backfill=True)
    assert len(report["evidence"][0]["rows"]) == 1
    assert report["evidence"][0]["summary"]["count"] == 1
    assert report["evidence"][0]["summary"]["amount_estimated_yuan"] == 1000


def test_depth_replay_from_snapshot_is_explicitly_receipt_only():
    from core.realtime.market_evidence import normalize_depth

    book = normalize_depth(NS(buy_levels=[], sell_levels=[]))
    snapshot = dict(
        event_id="snapshot",
        kind="snapshot",
        code="000001",
        received_at=STAMP,
        payload=dict(code="000001", date=DATE, depth=book, received_at=STAMP),
    )
    report = replay_evidence(
        [], date=DATE, until=STAMP, plan_id="frozen", codes=["000001"], embedded_snapshots=[snapshot]
    )
    assert report["snapshot_depth"][0]["depth"] == book
    assert not report["snapshot_depth"][0]["publication_verified"]
    assert report["event_counts"] == {}
