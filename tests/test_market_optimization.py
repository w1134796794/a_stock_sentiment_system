from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from core.data.providers.eltdx_provider import EltdxProvider, QuoteFailureState
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.collection_plan import DailyCollectionPlan
from core.realtime.listing_evidence import capture_listing_evidence, listing_exclusions
from core.realtime.market_events import code_versions, publish_revision
from core.realtime.sector_service import RealtimeSectorService
from core.realtime.strategy_runtime import affected_codes
from tests.test_market_process_pipeline import DAY, PREOPEN, bundle


def listing(rows):
    return {"market_date": DAY, "captured_at": PREOPEN.isoformat(), "complete": True, "rows": rows}


def test_listing_filter_is_shared_by_collector_and_strategy_and_keeps_unknown(tmp_path):
    data = bundle()
    data["inputs"]["listing_evidence"] = listing([
        {"code": "600001", "name": "retired", "list_status": "D", "delist_date": "20261022"},
        {"code": "600002", "list_status": "D", "delist_date": "20261026"},
        {"code": "000002", "list_date": "20261026", "list_status": "L"}])
    plan = DailyCollectionPlan(tmp_path, MemoryStateBackend()).freeze(data, now=PREOPEN)
    members = RealtimeSectorService.confirmation_members(data["inputs"])
    assert members["board"] == ["000001"]
    assert members["concept"] == ["600002"]  # Future delisting cannot remove a historical member.
    assert {r["name"]: r["members"] for r in plan["sectors"]} == members
    assert {r["code"] for r in plan["listing_exclusions"]} == {"000002", "600001"}
    assert "000002" not in {r["code"] for r in plan["rows"]}


def test_suspension_st_and_missing_quotes_are_not_delisting_evidence():
    data = {"listing_evidence": listing([
        {"code": "000001", "name": "*ST example", "list_status": "L"},
        {"code": "000002", "list_status": "P"}, {"code": "000003", "missing_quote": True}])}
    assert listing_exclusions(data) == {}
    data["listing_evidence"]["captured_at"] = "2026-10-23T09:25:00+08:00"
    data["listing_evidence"]["rows"].append({"code": "000004", "list_status": "D"})
    assert listing_exclusions(data) == {}


def test_preopen_listing_capture_is_dated_cached_and_never_fetches_intraday(tmp_path):
    calls = []
    def stock_basic(**kwargs):
        calls.append(kwargs["list_status"])
        return pd.DataFrame([{ "ts_code": "600001.SH", "name": "old", "list_status": "D",
                              "list_date": "20000101", "delist_date": "20261022"}]
                            if kwargs["list_status"] == "D" else
                            [{"ts_code": "000001.SZ", "name": "live", "list_status": "L",
                              "list_date": "20000101", "delist_date": None}]
                            if kwargs["list_status"] == "L" else [],
                            columns=["ts_code", "name", "list_status", "list_date", "delist_date"])
    client = SimpleNamespace(stock_basic=stock_basic)
    result = capture_listing_evidence(DAY, root=tmp_path, client=client, now=PREOPEN)
    assert result["complete"] and calls == ["L", "D", "P", "G", "UN"]
    capture_listing_evidence(DAY, root=tmp_path, client=client, now=PREOPEN)
    assert len(calls) == 5
    for now in (PREOPEN.replace(hour=10), PREOPEN + timedelta(days=1)):
        with pytest.raises(ValueError):
            capture_listing_evidence(DAY, root=tmp_path, client=client, now=now)
    assert len(calls) == 5


def test_missing_listing_permissions_do_not_remove_unknown_members(tmp_path):
    def denied(**kwargs):
        raise RuntimeError("permission denied")
    result = capture_listing_evidence(DAY, root=tmp_path, client=SimpleNamespace(stock_basic=denied), now=PREOPEN)
    assert not result["complete"] and len(result["failed_statuses"]) == 5
    assert listing_exclusions({"listing_evidence": result}) == {}


class Client:
    def __init__(self, callback):
        self.quotes = SimpleNamespace(get_snapshots=callback)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_missing_symbol_cooldown_survives_new_provider_batches_and_recovers():
    now, calls = [0.0], []
    state = QuoteFailureState(clock=lambda: now[0])
    def request(codes):
        calls.append(list(codes))
        if "sz000004" in codes:
            raise ValueError("snapshot record marker not found: sz000004")
        return [SimpleNamespace(code="000001", last_price=10)]
    for _ in range(2):
        provider = EltdxProvider(failure_state=state)
        provider._client = lambda: Client(request)
        assert set(provider.get_quote_snapshots(["000001", "000004"])) == {"000001"}
    assert calls == [["sz000001", "sz000004"], ["sz000001"], ["sz000001"]]
    assert provider.last_quote_diagnostics["failure_categories"] == {"symbol_record_missing": 1}
    now[0] = 301
    provider.get_quote_snapshots(["000001", "000004"])
    assert calls[-2] == ["sz000001", "sz000004"]


@pytest.mark.parametrize("error,category,delay", [
    (TimeoutError("timeout"), "timeout", 2),
    (ValueError("7709 request failed: 0x054c"), "protocol_request_failed", 5)])
def test_chunk_failures_have_bounded_cooldown_not_permanent_symbol_exclusions(error, category, delay):
    now, calls = [0.0], []
    state = QuoteFailureState(clock=lambda: now[0])
    def request(codes):
        calls.append(codes)
        raise error
    for _ in range(2):
        provider = EltdxProvider(failure_state=state)
        provider._client = lambda: Client(request)
        assert not provider.get_quote_snapshots(["000001", "000002"])
    assert len(calls) == 1
    assert provider.last_quote_diagnostics["failure_categories"] == {category: 2}
    assert provider.last_quote_diagnostics["batches"][0]["duration_ms"] >= 0
    assert not any(key[0] == "symbol" for key in state.entries)
    now[0] = delay + .1
    provider.get_quote_snapshots(["000001", "000002"])
    assert len(calls) == 2


def test_merged_events_keep_all_changed_codes_even_if_wakeup_is_lost():
    backend = MemoryStateBackend()
    publish_revision(backend, DAY, kind="quotes", codes=["000001"])
    publish_revision(backend, DAY, kind="quotes", codes=["600001"])
    assert set(code_versions(backend, DAY)) == {"000001", "600001"}
    assert code_versions(backend, "20261026") == {}


def test_actual_sample_interval_survives_read_only_quote_normalization():
    from core.realtime.models import QuoteSnapshot
    raw = {"code": "000001", "last_price": 10, "sample_interval_seconds": 4.2}
    first = QuoteSnapshot.from_raw(raw).to_dict()
    second = QuoteSnapshot.from_raw(first).to_dict()
    assert second["sample_interval_seconds"] == 4.2


def test_second_level_assessment_cannot_emit_formal_signals_or_notifications():
    from core.realtime.shadow_assessment import assess_snapshot_sequence
    now = PREOPEN.replace(hour=10)
    rows = [{"received_at": (now - timedelta(seconds=i)).isoformat(), "date": DAY,
             "source": "eltdx_batch", "last_price": 10} for i in (0, 3, 6)]
    report = assess_snapshot_sequence(rows, DAY, now=now)
    assert report["snapshot_continuity_ready"]
    assert not report["formal_signal"] and not report["notification_sent"]
    assert not report["seconds_board_confirmation_ready"]
    assert not assess_snapshot_sequence(rows, DAY, now=now + timedelta(seconds=9))["snapshot_continuity_ready"]
    rows[1]["received_at"] = (now + timedelta(minutes=1)).isoformat()
    assert assess_snapshot_sequence(rows, DAY, now=now)["invalid_samples"] == 1


def test_affected_rows_preserve_priority_peer_dependencies():
    rows = [{"code": "000001", "sector_names": ["A"]}, {"code": "000002", "sector_names": ["B"]}]
    plan = {"sectors": [{"name": "A", "members": ["600001"]}, {"name": "B", "members": ["600002"]}]}
    assert affected_codes(rows, plan, {"600001"}) == {"000001"}
    assert affected_codes(rows, plan, {"600001", "600002"}) == {"000001", "000002"}
    assert affected_codes(rows, plan, {"000001"}) == {"000001", "000002"}
    assert affected_codes(rows, plan, {"600003"}) == set()
    assert affected_codes(rows, plan, set(), full=True) == {"000001", "000002"}


def test_runtime_reuses_unaffected_rows_without_relabelling_them_as_new_confirmations(tmp_path, monkeypatch):
    from core.operations.ledger import TradingLedger
    from core.realtime.signal_event_repository import RealtimeSignalEventRepository
    from core.realtime.strategy_runtime import StrategyRuntime
    data = bundle()
    for row in data["inputs"]["leader_pools"]["10"]["rows"] + data["inputs"]["candidates"]:
        row.update(resonance_sectors="concept", concept_tags=[])
    data["inputs"]["leader_pools"]["10"]["rows"][0]["resonance_sectors"] = "board"
    backend = MemoryStateBackend()
    plans = DailyCollectionPlan(tmp_path, backend)
    plans.freeze(data, now=PREOPEN)
    monkeypatch.setattr(RealtimeSignalEventRepository, "record_payload", lambda *a: 0)
    monkeypatch.setattr(RealtimeSignalEventRepository, "merge_history", lambda self, value: value)
    now = [datetime(2026, 10, 23, 9, 36)]
    worker = StrategyRuntime(backend=backend, plans=plans, ledger=TradingLedger(tmp_path / "ledger.sqlite3"),
                             clock=lambda: now[0])
    publish_revision(backend, DAY, kind="quotes", codes=["000001"])
    first = worker.run_once()
    assert not any(row["evaluation_reused"] for row in first["rows"])
    recorded = []
    worker._record = lambda payload: recorded.append([r["code"] for r in payload["rows"]])
    publish_revision(backend, DAY, kind="quotes", codes=["600001"])
    second = worker.run_once()
    assert [r["code"] for r in second["rows"] if not r["evaluation_reused"]] == ["000001"]
    assert second["runtime_cycle"]["reused_stocks"] == 34
    assert recorded == [["000001"]]
    assert not any(r["confirm_status"] == "confirmed" for r in second["rows"])
    now[0] += timedelta(minutes=1)
    third = worker.run_once()
    assert not any(row["evaluation_reused"] for row in third["rows"])


@pytest.mark.parametrize("at,expected", [("11:31:00", False), ("12:31:00", False), ("09:15:00", True), ("13:00:00", True)])
def test_collector_avoids_lunch_request_storm(at, expected):
    from core.realtime.eltdx_collector import EltdxQuoteCollector
    worker = EltdxQuoteCollector.__new__(EltdxQuoteCollector)
    worker.clock = lambda: datetime.fromisoformat("2026-10-23T" + at)
    worker.calendar = SimpleNamespace(is_trade_date=lambda _: True)
    assert worker._is_collection_session() == expected
