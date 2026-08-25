from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from core.data.providers.eltdx_provider import EltdxProvider
from core.infrastructure.shared_state import MemoryStateBackend
from core.realtime.quote_cache import RealtimeQuoteCache
from core.realtime.sector_service import RealtimeSectorService


class FakeSnapshotRepository:
    def __init__(self):
        self.rows = []

    def append_batch(self, trade_date, rows):
        self.rows.extend(rows)
        return len(rows)

    def read(self, _trade_date, code):
        return [row for row in self.rows if row.get("code") == code]


class FakeQuotes:
    def __init__(self):
        self.calls = []

    def get_snapshots(self, codes):
        self.calls.append(list(codes))
        return [
            SimpleNamespace(
                code="000001",
                last_price=10.5,
                pre_close_price=10.0,
                open_price=10.1,
                high_price=10.6,
                low_price=9.9,
                time_raw=9310200,
                total_hand=123,
                current_hand=5,
                amount=128_000,
                inside_dish=50,
                outer_disc=73,
                open_amount_yuan=20_000,
                buy_levels=(SimpleNamespace(price=10.49, volume=8),),
                sell_levels=(SimpleNamespace(price=10.5, volume=12),),
            )
        ]


class FakeClient:
    def __init__(self):
        self.quotes = FakeQuotes()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeSectorQuoteService:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_quotes(self, codes, **_kwargs):
        self.calls.append(list(codes))
        return {"ok": True, "quotes": [self.rows[code] for code in codes if code in self.rows]}

    @staticmethod
    def health():
        return {"available": True, "provider": "fake-redis"}


def _write_ths_fixture(tmp_path):
    index_dir = tmp_path / "sector" / "ths_index"
    member_dir = tmp_path / "sector" / "ths_member"
    index_dir.mkdir(parents=True)
    member_dir.mkdir(parents=True)
    (index_dir / "adata_concept_ths.csv").write_text(
        "index_code,index_name,sector_type\n885001,机器人概念,概念\n", encoding="utf-8"
    )
    (member_dir / "885001.TI.csv").write_text(
        "con_code\n000001.SZ\n600000.SH\n", encoding="utf-8"
    )


def test_quote_cache_computes_snapshot_deltas():
    backend = MemoryStateBackend("test_eltdx")
    snapshots = FakeSnapshotRepository()
    cache = RealtimeQuoteCache(backend=backend, snapshot_repository=snapshots)
    now = datetime.now()
    base = {
        "code": "000001", "last_price": 10.0, "pre_close": 9.5,
        "vol_hand": 100, "amount_yuan": 100_000, "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H:%M:%S"), "received_at": now.isoformat(),
    }
    cache.write_batch([base], source="eltdx_batch", collector_id="test")
    cache.write_batch(
        [{**base, "vol_hand": 125, "amount_yuan": 126_000}],
        source="eltdx_batch", collector_id="test",
    )

    assert snapshots.rows[-1]["delta_volume"] == 25
    assert snapshots.rows[-1]["delta_amount"] == 26_000
    assert snapshots.rows[-1]["quality_ok"] is True


def test_eltdx_provider_uses_one_connection_and_normalizes_source_fields():
    client = FakeClient()
    provider = EltdxProvider()
    provider._client = lambda: client

    result = provider.get_quote_snapshots(["000001", "600000"])

    assert client.quotes.calls == [["sz000001", "sh600000"]]
    row = result["000001"]
    assert row["last_price"] == 10.5
    assert row["vol_hand"] == 123
    assert row["amount_yuan"] == 128_000
    assert row["bid1"] == 10.49
    assert row["ask1"] == 10.5
    assert row["time"] == "09:31:02"
    assert row["source"] == "eltdx_batch"


def test_sector_strength_uses_one_constituent_quote_batch(tmp_path):
    _write_ths_fixture(tmp_path)
    quotes = FakeSectorQuoteService({
        "000001": {"code": "000001", "change_pct": 2.0, "amount_yuan": 100,
                   "vol_hand": 10, "date": "20260825", "time": "09:31:00", "is_stale": False},
        "600000": {"code": "600000", "change_pct": 4.0, "amount_yuan": 200,
                   "vol_hand": 20, "date": "20260825", "time": "09:31:03", "is_stale": False},
    })
    service = RealtimeSectorService(quote_service=quotes, cache_dir=tmp_path)

    result = service.get_sector_quotes(["885001"], include_raw=True)

    assert quotes.calls == [["000001", "600000"]]
    assert result["source"] == "ths_constituent_aggregation"
    assert result["sectors"][0]["change_pct"] == 3.0
    assert result["sectors"][0]["raw"]["coverage"] == 1.0


def test_sector_strength_excludes_stale_members(tmp_path):
    _write_ths_fixture(tmp_path)
    quotes = FakeSectorQuoteService({
        "000001": {"code": "000001", "change_pct": 2.0, "is_stale": False},
        "600000": {"code": "600000", "change_pct": 9.0, "is_stale": True},
    })
    service = RealtimeSectorService(quote_service=quotes, cache_dir=tmp_path)

    result = service.get_sector_quotes(["885001"], include_raw=True)

    assert result["sectors"][0]["change_pct"] == 2.0
    assert result["sectors"][0]["raw"]["coverage"] == 0.5
    assert result["sectors"][0]["raw"]["stale_excluded"] == 1


def test_sector_service_rejects_non_ths_700_codes():
    service = RealtimeSectorService(quote_service=FakeSectorQuoteService({}))
    result = service.get_sector_quotes(["700632"])

    assert result["ok"] is False
    assert result["missing"] == ["700632"]
