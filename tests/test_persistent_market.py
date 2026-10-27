import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.day_lifecycle import RealtimeDayLifecycle
from core.realtime.market_journal import MarketJournal
from core.realtime.persistent_market import PersistentMarketReader
from core.realtime.trading_session import shanghai_now


DAY = "20261026"


def at(time, day=DAY):
    return shanghai_now(datetime.strptime(day + time, "%Y%m%d%H:%M:%S"))


def snapshot(time, *, code="300369", received=None):
    return dict(code=code, date=DAY, time=time, received_at=(received or at(time)).isoformat(),
                last_price=10.1, open_price=10, pre_close=9.9, source="eltdx_batch", quality_ok=True,
                vol_hand=1000, amount_yuan=1010000, delta_volume=10, ask1=10.11)


def test_persistent_query_survives_hot_expiry_and_archive_move(tmp_path):
    clock = [at("09:36:00")]
    journal = MarketJournal(tmp_path, clock=lambda: clock[0])
    journal.append("snapshot", DAY, [snapshot("09:36:00")])
    clock[0] += timedelta(seconds=3)
    journal.append("snapshot", DAY, [snapshot("09:36:03")])
    reader = PersistentMarketReader(tmp_path)
    view = reader.view(DAY, asof=at("15:00:00"))
    before = view.snapshots(["300369"], start="09:35:00", end="09:37:00")
    assert len(before) == 2
    journal.archive_once()
    after = view.snapshots(["300369"], start="09:35:00", end="09:37:00")
    assert [r["_event_id"] for r in before] == [r["_event_id"] for r in after]
    assert after[0]["received_at"] == at("09:36:00").isoformat()


def test_fixed_watermark_excludes_late_writes_even_with_old_source_time(tmp_path):
    clock = [at("09:36:00")]
    journal = MarketJournal(tmp_path, clock=lambda: clock[0])
    journal.append("snapshot", DAY, [snapshot("09:36:00")])
    reader = PersistentMarketReader(tmp_path)
    fixed = reader.view(DAY, asof=at("09:36:05"))
    clock[0] = at("09:36:02")
    journal.append("snapshot", DAY, [snapshot("09:36:02")])
    assert len(fixed.snapshots(["300369"])) == 1
    assert len(reader.view(DAY, asof=at("09:36:05")).snapshots(["300369"])) == 2
    # Backdated source/receipt does not bypass actual persistence time.
    clock[0] = at("09:40:00")
    journal.append("snapshot", DAY, [snapshot("09:36:04")])
    assert len(reader.view(DAY, asof=at("09:38:00")).snapshots(["300369"])) == 2


def test_minute_versions_are_deduplicated_and_visible_as_received(tmp_path):
    clock = [at("09:36:00")]
    journal = MarketJournal(tmp_path, clock=lambda: clock[0])
    bar = dict(time="09:35:00", open=10, high=10.1, low=9.9, close=10, volume=1000, amount=10000)
    def write(value):
        journal.append("minutes", DAY, [dict(code="300369", received_at=clock[0].isoformat(), bars=[value])])
    write(bar)
    clock[0] = at("09:37:00")
    write(bar)
    assert journal.health()["pending"] == 1
    clock[0] = at("09:40:00")
    write({**bar, "close": 10.05})
    journal.archive_once()
    reader = PersistentMarketReader(tmp_path)
    assert reader.view(DAY, asof=at("09:38:00")).minutes("300369").iloc[0]["close"] == 10
    assert reader.view(DAY, asof=at("09:41:00")).minutes("300369").iloc[0]["close"] == 10.05


def test_spool_archive_duplicate_is_read_once(tmp_path):
    journal = MarketJournal(tmp_path, clock=lambda: at("09:36:00"))
    journal.append("snapshot", DAY, [snapshot("09:36:00")])
    with sqlite3.connect(journal.path) as con:
        row = con.execute("SELECT * FROM market_events").fetchone()
        columns = [r[1] for r in con.execute("PRAGMA table_info(market_events)")]
    journal.archive_once()
    # Simulate crash after archive commit but before spool deletion.
    with sqlite3.connect(journal.path) as con:
        con.execute("INSERT INTO market_events("+",".join(columns)+") VALUES ("+",".join("?" for _ in row)+")", row)
    assert len(PersistentMarketReader(tmp_path).view(DAY, asof=at("10:00:00")).snapshots(["300369"])) == 1


def test_afternoon_sector_checker_reads_actual_morning_window(tmp_path):
    from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
    clock = [at("09:57:01")]
    journal = MarketJournal(tmp_path, clock=lambda: clock[0])
    for minute, price in (("09:57:00", 10.1), ("10:00:00", 10.2)):
        clock[0] = at(minute)+timedelta(seconds=1)
        for code in ("000001", "000002"):
            point = dict(code=code, time=minute, price=price, open_price=10, pre_close=10,
                         received_at=(at(minute)-timedelta(seconds=1)).isoformat(), source="eltdx_batch")
            journal.append("sector_minutes", DAY, [dict(code=code, received_at=clock[0].isoformat(),
                date=DAY, source="collector_closed_quote_minutes", points=[point])])
    provider = RealtimeSectorBreadthProvider()
    provider.market_view = PersistentMarketReader(tmp_path).view(DAY, asof=at("15:00:00"))
    provider._batch_frozen_context = (DAY, {"inputs": {"sector_members": {"board": ["000001", "000002"]},
                                                          "sector_codes": {"board": "885001"},
                                                          "daily_rows": []}})
    checker, detail = provider.minute_checker(["board"], DAY, exclude_code="300369")
    assert checker("10:00:00") is True
    assert detail["coverage"] == 1
    assert detail["requested_minutes"] == ["09:57:00", "10:00:00"]
    assert detail["sampled_at"] == at("15:00:00").isoformat()
    assert checker("10:10:00") is None
    assert detail["coverage"] == 0  # Never carry 10:00 points into an unobserved minute.


def test_day_rollover_deletes_only_prior_raw_data_and_preserves_today(tmp_path):
    root, cache = tmp_path / "webdata", tmp_path / "cache"
    journal = MarketJournal(root / "market_archive", clock=lambda: at("09:36:00"))
    journal.append("snapshot", "20261023", [{**snapshot("09:36:00"), "date": "20261023"}])
    journal.archive_once()
    journal.append("snapshot", DAY, [snapshot("09:36:00")])
    journal.append("minutes", "20261023", [dict(code="300369", received_at=at("09:36:00").isoformat(), bars=[])])
    folder = cache / "stock" / "sector_quote_minutes"
    folder.mkdir(parents=True)
    for date in ("20261023", DAY):
        with sqlite3.connect(folder / f"{date}.sqlite3") as con:
            con.execute("CREATE TABLE test(x)")
    protected = [root / "collection_plans" / "20261023.json", root / "logs" / "intraday_debug" / "20261023.sqlite3",
                 cache / "stock" / "tick" / "300369.SZ_20261023.csv"]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve")
    backend = MemoryStateBackend()
    for date in ("20261023", DAY):
        backend.set_json(f"realtime:minutes:{date}:300369", [])
    backend.set_json("realtime:quotes:latest:300369", {"date": "20261023"})
    backend.set_json("realtime:strategy:result:20261023", {"preserve": True})
    lifecycle = RealtimeDayLifecycle(journal, cache_dir=cache, backend=backend, clock=lambda: at("09:40:00"))
    result = lifecycle.start_day(DAY)
    assert len(result["removed_files"]) == 2
    assert journal.health()["pending"] == 1
    assert not (folder / "20261023.sqlite3").exists()
    assert (folder / f"{DAY}.sqlite3").exists()
    assert all(p.read_text() == "preserve" for p in protected)
    assert backend.get_json("realtime:minutes:20261023:300369") is None
    assert backend.get_json(f"realtime:minutes:{DAY}:300369") == []
    assert backend.get_json("realtime:strategy:result:20261023") == {"preserve": True}
    assert lifecycle.start_day(DAY)["same_day_restart"]
    assert journal.health()["pending"] == 1
    with pytest.raises(ValueError, match="旧交易日"):
        journal.append("snapshot", "20261023", [snapshot("09:36:00")])
    with pytest.raises(ValueError, match="当前交易日"):
        lifecycle.start_day("20261023")
    from core.realtime.persistent_market import RetiredMarketDay
    with pytest.raises(RetiredMarketDay, match="已按保留规则清理"):
        PersistentMarketReader(journal.root).view("20261023").snapshots(["300369"])


def test_missing_store_read_creates_no_database(tmp_path):
    root = tmp_path / "missing"
    assert PersistentMarketReader(root).view(DAY, asof=at("10:00:00")).snapshots(["300369"]) == []
    assert not root.exists()


def test_read_only_provider_uses_durable_data_without_redis_market_cache(tmp_path):
    from core.realtime.persistent_market import PersistentQuoteProvider
    from core.realtime.read_only_market import ReadOnlyMarketData
    journal = MarketJournal(tmp_path, clock=lambda: at("09:36:03"))
    journal.append("snapshot", DAY, [snapshot("09:36:00"), snapshot("09:36:03")])
    bar = dict(time="09:35:00", open=10, high=10.1, low=9.9, close=10, volume=1000, amount=10000)
    journal.append("minutes", DAY, [dict(code="300369", received_at=at("09:36:00").isoformat(), bars=[bar])])
    journal.archive_once()
    provider = PersistentQuoteProvider(backend=MemoryStateBackend(), reader=PersistentMarketReader(tmp_path))
    provider.begin(DAY, asof=at("09:36:05"))
    dm = ReadOnlyMarketData(provider=provider)
    assert dm.get_quote_snapshot("300369")["last_price"] == 10.1
    assert not dm.get_quote_snapshot("300369")["is_stale"]
    assert len(dm.get_realtime_snapshot_ticks("300369", DAY)) == 2
    assert len(dm.get_minute_bars_live("300369", DAY)) == 1
    assert dm.get_auction_data("300369", DAY) == {}


def test_readiness_uses_durable_inputs_when_hot_cache_is_empty(tmp_path, monkeypatch):
    from test_market_readiness import healthy_inputs, run
    from core.realtime.market_readiness import RedisAuditReader, PersistentAuditReader
    _, now, ledger, _, health, *_ = healthy_inputs(tmp_path)
    monkeypatch.setattr(RedisAuditReader, "__init__", lambda *a: None)
    monkeypatch.setattr(RedisAuditReader, "many", lambda self, keys: {k: health.get(k) for k in keys if k.endswith(":health")})
    reader = PersistentAuditReader("unused", "unused", root=tmp_path, date="20261022", now=now)
    report = run(tmp_path, now, ledger, reader)
    assert report["live_ready"] and report["replay_ready"]
    assert report["input_storage"] == "durable_spool_and_archive"


def test_legacy_schema_is_readonly_and_does_not_claim_visibility(tmp_path):
    path = tmp_path / f"market_{DAY}.sqlite3"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE market_events(event_id TEXT,kind TEXT,code TEXT,received_at TEXT,payload TEXT)")
        row = snapshot("09:36:00")
        con.execute("INSERT INTO market_events VALUES (?,?,?,?,?)", ("legacy", "snapshot", "300369", row["received_at"], json.dumps(row)))
    before = path.read_bytes()
    rows = PersistentMarketReader(tmp_path).view(DAY, asof=at("09:36:05")).snapshots(["300369"])
    assert len(rows) == 1 and rows[0]["_legacy_visibility_unknown"]
    assert rows[0]["quality_ok"] is False
    assert path.read_bytes() == before


def test_serial_candidate_batch_captures_fresh_boundary_for_each_stock(monkeypatch):
    from types import SimpleNamespace
    import pandas as pd
    from backtest.minute_entry import MinuteEntryEvaluator
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    clock = [at("09:36:00")]
    captured = []

    class Provider:
        def begin(self, date, *, asof):
            captured.append(asof)
            return SimpleNamespace(asof=asof)

    service = RealtimeEntrySignalService(data_manager=SimpleNamespace(provider=Provider()),
        evaluator=MinuteEntryEvaluator(), sector_breadth_provider=SimpleNamespace(),
        quote_service=SimpleNamespace(get_quote=lambda code: {"received_at": clock[0].isoformat()}))
    service.persistent_evaluation_enabled = True
    service.evaluation_clock = lambda: clock[0]
    monkeypatch.setattr(service, "_minute_frame", lambda *a: pd.DataFrame())
    monkeypatch.setattr(service, "_previous_daily_map", lambda *a: {})
    used = []

    def evaluate(row, quote, *args):
        used.append(quote["received_at"])
        clock[0] += timedelta(seconds=10)  # Simulate a slow first stock.
        return {}

    monkeypatch.setattr(service, "_evaluate_one", evaluate)
    service.evaluate([{"code": "300369"}, {"code": "000001"}], {}, market_date=DAY)
    assert captured == [at("09:36:00"), at("09:36:10")]
    assert used == [d.isoformat() for d in captured]
