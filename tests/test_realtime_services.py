import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from core.realtime.quote_service import RealtimeQuoteService
from core.realtime.sector_service import RealtimeSectorService


def test_realtime_package_does_not_eagerly_import_service_modules():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import core.realtime

service_modules = {
    'core.realtime.overlay_service',
    'core.realtime.quote_service',
    'core.realtime.sector_service',
}
loaded = service_modules.intersection(sys.modules)
assert not loaded, f'eager realtime imports: {sorted(loaded)}'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


class FakeQuoteDataManager:
    def __init__(self):
        self.calls = 0

    def get_quote_snapshots(self, codes):
        self.calls += 1
        return {
            "000001": {
                "code": "000001",
                "name": "平安银行",
                "open_price": 10.1,
                "pre_close": 10.0,
                "last_price": 10.35,
                "high_price": 10.5,
                "low_price": 10.05,
                "vol_hand": 12345,
                "amount_yuan": 4567890,
                "date": "20260615",
                "time": "09:31:00",
                "source": "fake",
            }
        }


def test_realtime_quote_service_normalizes_and_caches():
    dm = FakeQuoteDataManager()
    service = RealtimeQuoteService(dm, ttl_seconds=30)

    first = service.get_quotes(["000001.SZ"])
    second = service.get_quotes(["000001"])

    assert first["ok"] is True
    assert first["count"] == 1
    assert first["quotes"][0]["code"] == "000001"
    assert first["quotes"][0]["ts_code"] == "000001.SZ"
    assert round(first["quotes"][0]["change_pct"], 2) == 3.5
    assert second["ok"] is True
    assert dm.calls == 1


def test_realtime_sector_service_normalizes_adata_sector_quote():
    def get_market_concept_current_east(index_code=None):
        return [{
            "index_code": index_code,
            "index_name": "机器人概念",
            "最新价": 1234.5,
            "涨跌幅": "2.5%",
            "成交额": 987654321,
        }]

    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(get_market_concept_current_east=get_market_concept_current_east),
            info=SimpleNamespace(all_concept_code_east=lambda: [{"index_code": "BK0001"}]),
        )
    )

    service = RealtimeSectorService(fake_adata, ttl_seconds=30)
    result = service.get_sector_quotes(["BK0001"], source="east")

    assert result["ok"] is True
    assert result["count"] == 1
    sector = result["sectors"][0]
    assert sector["code"] == "BK0001"
    assert sector["name"] == "机器人概念"
    assert sector["last_price"] == 1234.5
    assert sector["change_pct"] == 2.5


def test_realtime_sector_service_falls_back_to_ths_auto_list():
    def all_concept_code_east():
        raise FileNotFoundError("missing east cache")

    def get_market_concept_current_ths(index_code=None):
        return [{
            "index_code": index_code,
            "trade_time": "2026-06-15 09:31:00",
            "price": 1265.38,
            "amount": 200369000000,
        }]

    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(get_market_concept_current_ths=get_market_concept_current_ths),
            info=SimpleNamespace(
                all_concept_code_east=all_concept_code_east,
                all_concept_code_ths=lambda: [{"index_code": "886109", "name": "2026一季报预增"}],
            ),
        )
    )

    service = RealtimeSectorService(fake_adata, ttl_seconds=30)
    result = service.get_sector_quotes(codes=None, source="east", limit=1)

    assert result["ok"] is True
    assert result["source"] == "ths"
    assert result["sectors"][0]["code"] == "886109"
    assert result["sectors"][0]["name"] == "2026一季报预增"
    assert result["sectors"][0]["time"] == "2026-06-15 09:31:00"
