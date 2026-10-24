from dataclasses import fields
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.data.providers.eltdx_provider import EltdxProvider
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.board_diagnostics import board_shadow_report
from core.realtime.board_plan import BoardCollectionPlan
from core.realtime.board_quote_lane import BoardQuoteLane
from core.realtime.board_quote_store import BoardQuoteStore, aggregate_board_minutes
from core.realtime.market_journal import MarketJournal


DAY = "20261023"
PREOPEN = datetime.fromisoformat("2026-10-23T08:50:00+08:00")
OPEN = PREOPEN.replace(hour=9, minute=31)


def parent():
    return {"market_date": DAY, "plan_id": "parent", "sectors": [
        {"name": "A", "code": "800001.TI", "members": ["000001", "000002"]},
        {"name": "B", "code": "800002.TI", "members": ["000003"]}]}


class Provider:
    def __init__(self):
        self.calls = []

    def prepare_board_catalogue(self):
        self.calls.append("catalogue")
        return {"prepared_date": DAY, "rows": [{"code": "sh880001", "name": "A"}]}

    def prepare_board_members(self, code):
        self.calls.append(code)
        return {"prepared_date": DAY, "members": ["000001", "000004"],
                "excluded_members": [{"code": "000005", "reason": "absent_from_current_security_list"}]}


def make_plan(tmp_path):
    plans = BoardCollectionPlan(tmp_path)
    plan = plans.prepare(parent(), Provider(), now=PREOPEN)
    return plans, plan


def quote(stamp=OPEN, **updates):
    return {"code": "sh880001", "date": DAY, "source": "eltdx_board_snapshot",
            "received_at": stamp.isoformat(), "last_price": 102, "open_price": 101,
            "pre_close": 100, **updates}


def make_store(tmp_path):
    journal = MarketJournal(tmp_path / "archive")
    return BoardQuoteStore(backend=MemoryStateBackend(), journal=journal, archive_root=journal.root), journal


def test_same_name_is_only_a_candidate_with_explicit_membership_differences(tmp_path):
    _, plan = make_plan(tmp_path)
    first = plan["rows"][0]
    assert first["status"] == "requires_review" and not first["formal_eligible"]
    diff = first["candidates"][0]
    assert diff["jaccard"] == pytest.approx(1 / 3)
    assert diff["ths_only"] == ["000002"] and diff["tdx_only"] == ["000004"]
    assert plan["rows"][1]["status"] == "unmapped"
    assert plan["codes"] == ["sh880001"]
    assert not plan["formal_eligible"] and len(plan["warnings"]) == 2


def test_preopen_only_no_provider_calls_after_cutoff_and_immutable_mapping(tmp_path):
    plans, plan = make_plan(tmp_path)
    provider = Provider()
    assert plans.prepare(parent(), provider, now=PREOPEN) == plan
    assert not provider.calls
    for stamp in (OPEN, PREOPEN + timedelta(days=1)):
        with pytest.raises(ValueError, match="before 09:25"):
            plans.prepare(parent(), provider, now=stamp)
    assert not provider.calls
    with pytest.raises(ValueError, match="already frozen"):
        plans.prepare(parent(), provider, hints={"A": "sh880001"}, now=PREOPEN)
    with pytest.raises(ValueError, match="mismatched"):
        plans.load(DAY, parent_id="different")


def test_preparation_finishing_after_cutoff_does_not_publish(tmp_path):
    stamps = iter([PREOPEN, PREOPEN, OPEN])
    plans = BoardCollectionPlan(tmp_path)
    with pytest.raises(ValueError, match="before 09:25"):
        plans.prepare(parent(), Provider(), clock=lambda: next(stamps))
    assert plans.load(DAY) is None


def test_preparation_rejects_wrong_sdk_date_unknown_hints_and_stale_members(tmp_path):
    plans = BoardCollectionPlan(tmp_path)
    provider = Provider()
    with pytest.raises(ValueError, match="outside"):
        plans.prepare(parent(), provider, hints={"unknown": "sh880001"}, now=PREOPEN)
    assert not provider.calls
    with pytest.raises(ValueError, match="Unknown"):
        plans.prepare(parent(), provider, hints={"B": "sh880099"}, now=PREOPEN)
    provider.prepare_board_catalogue = lambda: {"prepared_date": "20261022", "rows": []}
    with pytest.raises(ValueError, match="preparation date"):
        plans.prepare(parent(), provider, now=PREOPEN)
    provider = Provider()
    provider.prepare_board_members = lambda code: {"prepared_date": "20261022", "members": [], "excluded_members": []}
    with pytest.raises(ValueError, match="membership date"):
        plans.prepare(parent(), provider, now=PREOPEN)


def test_store_is_separate_monotonic_and_freshness_checked_on_read(tmp_path):
    _, plan = make_plan(tmp_path)
    store, _ = make_store(tmp_path)
    assert store.write(plan, [quote()], now=OPEN)["count"] == 1
    later = OPEN + timedelta(seconds=10)
    assert store.write(plan, [quote(later, last_price=103)], now=later)["count"] == 1
    assert store.write(plan, [quote()], now=later)["rejected_count"] == 1
    current = store.read(plan, now=later)["sh880001"]
    assert current["status"] == "fresh_receipt" and current["continuity_ok"]
    assert current["change_pct"] == pytest.approx(3)
    assert current["open_gain_pct"] == pytest.approx((103 / 101 - 1) * 100)
    assert not current["formal_eligible"] and not current["exchange_timestamp_verified"]
    assert store.read(plan, now=later + timedelta(seconds=21))["sh880001"]["status"] == "stale"
    assert store.read(plan, now=OPEN - timedelta(seconds=1))["sh880001"]["status"] == "missing"
    assert store.backend.get_json("realtime:quotes:latest:880001") is None
    assert store.backend.get_json("realtime:market:revision") is None


@pytest.mark.parametrize("updates", [
    {"last_price": float("nan")}, {"last_price": float("inf")}, {"last_price": 0},
    {"code": "sh880099"}, {"date": "20261022"}, {"source": "other"},
    {"received_at": "bad"}, {"received_at": (OPEN + timedelta(seconds=1)).isoformat()},
    {"received_at": (OPEN - timedelta(seconds=21)).isoformat()}])
def test_store_rejects_missing_invalid_future_or_out_of_plan_evidence(tmp_path, updates):
    _, plan = make_plan(tmp_path)
    store, journal = make_store(tmp_path)
    assert store.write(plan, [quote(**updates)], now=OPEN)["rejected_count"] == 1
    assert journal.health()["pending"] == 0


def test_hot_expiration_does_not_control_archive_and_readers_do_not_create_files(tmp_path):
    _, plan = make_plan(tmp_path)
    store, journal = make_store(tmp_path)
    for seconds in range(0, 201, 10):
        stamp = OPEN + timedelta(seconds=seconds)
        store.write(plan, [quote(stamp)], now=stamp)
    assert journal.health()["pending"] == 21
    assert len(store.backend.get_json(store.key(DAY, "sh880001"))) == 19
    assert journal.archive_once() == 21
    assert journal.health()["pending"] == 0
    assert len(store.archived_points(DAY, until=OPEN + timedelta(seconds=200))) == 21
    assert len(store.archived_points(DAY, until=OPEN)) == 1
    assert store.archived_points("20261022", until=OPEN) == []
    empty_root = tmp_path / "absent"
    reader = BoardQuoteStore(backend=MemoryStateBackend(), archive_root=empty_root)
    assert reader.archived_points(DAY, until=OPEN) == [] and not empty_root.exists()


def test_durable_append_precedes_cache_and_cache_failure_preserves_evidence(tmp_path, monkeypatch):
    _, plan = make_plan(tmp_path)
    store, journal = make_store(tmp_path)
    def fail(*args):
        assert journal.health()["pending"] == 1
        raise RuntimeError("Redis unavailable")
    monkeypatch.setattr(store.backend, "set_many_json", fail)
    with pytest.raises(RuntimeError):
        store.write(plan, [quote()], now=OPEN)
    assert len(store.archived_points(DAY, until=OPEN)) == 1


def test_board_sample_minutes_are_not_shifted_or_filled_or_future_leaked():
    rows = [quote(OPEN - timedelta(seconds=10), last_price=101), quote(OPEN, last_price=103),
            quote(OPEN + timedelta(minutes=2), last_price=104)]
    minutes = aggregate_board_minutes(rows, until=OPEN + timedelta(minutes=2))
    assert [row["time"] for row in minutes] == ["09:31:00", "09:33:00"]
    assert minutes[0]["open"] == 101 and minutes[0]["close"] == 103 and minutes[0]["sample_count"] == 2
    assert minutes[0]["closed"] and minutes[0]["source"] == "sampled_board_snapshots"
    assert not minutes[0]["formal_eligible"]
    assert len(aggregate_board_minutes(rows, until=OPEN)) == 1


def test_lane_disabled_missing_plan_and_ownership_loss_never_fetch_or_write(tmp_path):
    plans, plan = make_plan(tmp_path)
    store, journal = make_store(tmp_path)
    calls = []
    provider = SimpleNamespace(get_board_snapshots=lambda codes: calls.append(codes))
    lane = BoardQuoteLane(plans, store, provider, clock=lambda: OPEN)
    assert lane.collect_once(parent())["status"] == "inactive"
    lane.enabled = True
    assert lane.collect_once(None)["status"] == "missing_collection_plan"
    assert not calls
    def fetch(codes):
        lane.owner = lambda: False
        return {"rows": [quote()], "missing_codes": [], "batches": []}
    lane.provider = SimpleNamespace(get_board_snapshots=fetch)
    assert lane.collect_once(parent())["status"] == "ownership_or_session_lost"
    assert journal.health()["pending"] == 0


def test_lane_uses_only_frozen_codes_and_preserves_last_good_on_failure(tmp_path):
    plans, _ = make_plan(tmp_path)
    store, _ = make_store(tmp_path)
    calls = []
    def fetch(codes):
        calls.append(codes)
        return {"rows": [quote()], "missing_codes": [], "batches": [{"duration_ms": 1}]}
    lane = BoardQuoteLane(plans, store, SimpleNamespace(get_board_snapshots=fetch), enabled=True, clock=lambda: OPEN)
    assert lane.collect_once(parent())["status"] == "ok"
    assert calls == [["sh880001"]]
    lane.provider.get_board_snapshots = lambda codes: (_ for _ in ()).throw(TimeoutError())
    assert lane.collect_once(parent())["category"] == "TimeoutError"
    assert store.backend.get_json(store.key(DAY, "sh880001"))


def test_sdk_board_identity_is_not_treated_as_a_stock_or_prepared_intraday(monkeypatch):
    models = pytest.importorskip("eltdx.models")
    values = {field.name: 0 for field in fields(models.QuoteSnapshot)}
    values.update(exchange="sh", code="880001", last_price=102, pre_close_price=100,
                  open_price=101, buy_levels=(), sell_levels=(), tail_raw=b"")
    snapshot = models.QuoteSnapshot(**values)
    class Client:
        quotes = SimpleNamespace(get_snapshots=lambda codes: [snapshot])
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    provider = EltdxProvider()
    monkeypatch.setattr(provider, "_client", Client)
    row = provider.get_board_snapshots(["sh880001"])["rows"][0]
    assert row["code"] == "sh880001" and row["source"] == "eltdx_board_snapshot"
    assert "ts_code" not in row
    assert row["pre_close"] == 100 and not row["exchange_timestamp_verified"]
    with pytest.raises(ValueError, match="market-prefixed"):
        provider.get_board_snapshots(["880001"])


def test_report_is_read_only_and_breadth_denominator_is_the_frozen_ths_cohort(tmp_path):
    _, plan = make_plan(tmp_path)
    store, _ = make_store(tmp_path)
    store.write(plan, [quote()], now=OPEN)
    store.backend.set_json("realtime:quotes:latest:000001", {"date": DAY, "source": "eltdx_batch",
        "last_price": 11, "pre_close": 10, "received_at": OPEN.isoformat()})
    store.backend.set_json("realtime:quotes:latest:000002", {"date": DAY, "source": "eltdx_batch",
        "last_price": 11, "pre_close": 10, "received_at": (OPEN - timedelta(seconds=13)).isoformat()})
    result = board_shadow_report(parent(), plan, store, now=OPEN)
    breadth = result["rows"][0]["ths_breadth"]
    assert breadth["coverage"] == .5 and breadth["member_count"] == 2
    assert breadth["advancing_ratio"] == 1
    assert not result["formal_signal"] and not result["notification_sent"]
    assert result["rows"][0]["tdx_quotes"][0]["status"] == "fresh_receipt"
    assert "not_formal" in result["rows"][0]["time_alignment"]


def test_real_sdk_board_tables_keep_definitions_with_missing_quotes(monkeypatch):
    pytest.importorskip("eltdx")
    from eltdx.helpers.boards import BoardQuoteRow, BoardQuoteTable, BoardMemberQuoteTable
    from datetime import date
    row = BoardQuoteRow(**{field.name: None for field in fields(BoardQuoteRow)} | {
        "board_code": "880001", "board_name": "A", "full_code": "sh880001", "exchange": "sh", "market_id": 1})
    class Client:
        helpers = SimpleNamespace(
            board_quotes=lambda **kwargs: BoardQuoteTable(rows=(row,), prepared_date=date(2026, 10, 23)),
            board_member_quotes=lambda code: BoardMemberQuoteTable(board_code="880001", board_name="A",
                raw_members=(), display_members=({"market": 0, "code": "000001"},),
                excluded_members=({"code": "000002", "reason": "absent_from_current_security_list"},),
                rows=(), prepared_date=date(2026, 10, 23)))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    provider = EltdxProvider()
    monkeypatch.setattr(provider, "_client", Client)
    assert provider.prepare_board_catalogue()["rows"] == [{"code": "sh880001", "name": "A"}]
    assert provider.prepare_board_members("sh880001")["members"] == ["000001"]


def test_board_requests_are_bounded_and_one_failed_batch_does_not_hide_other_batches(monkeypatch):
    calls = []
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def fetch(self, codes):
            calls.append(codes)
            if len(codes) == 80:
                raise TimeoutError("timeout")
            return [SimpleNamespace(full_code=codes[0], code=codes[0][2:], last_price=100)]
        def __init__(self):
            self.quotes = SimpleNamespace(get_snapshots=self.fetch)
    provider = EltdxProvider()
    monkeypatch.setattr(provider, "_client", Client)
    codes = [f"sh{880000 + i}" for i in range(81)]
    result = provider.get_board_snapshots(codes)
    assert [len(batch) for batch in calls] == [80, 1]
    assert len(result["rows"]) == 1 and len(result["missing_codes"]) == 80
    assert result["batches"][0]["category"] == "timeout"


def test_slow_board_requests_do_not_block_scheduler_and_lunch_does_not_collect(tmp_path):
    from threading import Event
    plans, _ = make_plan(tmp_path)
    store, _ = make_store(tmp_path)
    entered, release = Event(), Event()
    def fetch(codes):
        entered.set()
        assert release.wait(5)
        return {"rows": [quote()], "missing_codes": [], "batches": []}
    lane = BoardQuoteLane(plans, store, SimpleNamespace(get_board_snapshots=fetch), enabled=True, clock=lambda: OPEN)
    lane.schedule(parent())
    try:
        assert entered.wait(3)
        first = lane._thread
        lane.schedule(parent())
        assert first is lane._thread and first.is_alive()
    finally:
        release.set()
        lane._thread.join(5)
    assert not lane._thread.is_alive() and lane.health["status"] == "ok"
    lane.clock = lambda: OPEN.replace(hour=12)
    assert lane.collect_once(parent())["status"] == "inactive"
