import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from core.realtime.quote_service import RealtimeQuoteService
from core.realtime.sector_service import RealtimeSectorService


def test_quote_service_force_refresh_uses_one_batch_call():
    class FakeDataManager:
        def __init__(self):
            self.calls = []

        def get_quote_snapshots(self, codes):
            self.calls.append(list(codes))
            return {
                code: {
                    "code": code,
                    "last_price": 10.0,
                    "pre_close": 9.8,
                    "date": "20260813",
                    "time": "09:35:03",
                }
                for code in codes
            }

    dm = FakeDataManager()
    service = RealtimeQuoteService(dm, ttl_seconds=30)

    payload = service.refresh_quotes(["000001", "600000", "000001"])
    cached = service.get_quotes(["600000", "000001"])

    assert payload["count"] == 2
    assert cached["count"] == 2
    assert dm.calls == [["000001", "600000"]]


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
    def get_market_concept_current_ths(index_code=None):
        return [{
            "index_code": index_code,
            "index_name": "机器人概念",
            "最新价": 1234.5,
            "涨跌幅": "2.5%",
            "成交额": 987654321,
        }]

    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(get_market_concept_current_ths=get_market_concept_current_ths),
            info=SimpleNamespace(all_concept_code_ths=lambda: [{"index_code": "885001"}]),
        )
    )

    service = RealtimeSectorService(fake_adata, ttl_seconds=30)
    result = service.get_sector_quotes(["885001"], source="ths")

    assert result["ok"] is True
    assert result["count"] == 1
    sector = result["sectors"][0]
    assert sector["code"] == "885001"
    assert sector["name"] == "机器人概念"
    assert sector["last_price"] == 1234.5
    assert sector["change_pct"] == 2.5


def test_realtime_sector_service_uses_ths_auto_list():
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
                all_concept_code_ths=lambda: [{"index_code": "886109", "name": "2026一季报预增"}],
            ),
        )
    )

    service = RealtimeSectorService(fake_adata, ttl_seconds=30)
    result = service.get_sector_quotes(codes=None, source="ths", limit=1)

    assert result["ok"] is True
    assert result["source"] == "ths"
    assert result["sectors"][0]["code"] == "886109"
    assert result["sectors"][0]["name"] == "2026一季报预增"
    assert result["sectors"][0]["time"] == "2026-06-15 09:31:00"


def test_realtime_sector_service_filters_missing_code_markers():
    assert RealtimeSectorService._row_sector_code({"index_code": "nan"}) == ""
    assert RealtimeSectorService._row_sector_code({"index_code": "None"}) == ""
    assert RealtimeSectorService._normalize_codes(["nan", "--", None, "886109"]) == ["886109"]


def test_sector_name_resolution_uses_ths_namespace_only():
    service = RealtimeSectorService(SimpleNamespace(stock=SimpleNamespace()))
    service._remember_sector_meta("886001", "机器人", "概念", source="ths")
    service._remember_sector_meta("BK0001", "错误命名空间", "概念", source="east")
    service._sector_name_sources.add("ths")

    assert service.resolve_codes_by_names(["机器人"], source="ths") == {"机器人": "886001"}
    assert service.resolve_codes_by_names(["错误命名空间"], source="east") == {}


def test_ths_resolution_rejects_unsupported_700_classification_codes():
    service = RealtimeSectorService(SimpleNamespace(stock=SimpleNamespace()))
    service._remember_sector_meta("700632", "制造业指数", "行业", source="ths")
    service._remember_sector_meta("885806", "华为概念", "概念", source="ths")
    service._sector_name_sources.add("ths")

    assert service.resolve_codes_by_names(
        ["制造业指数", "华为概念"], source="ths",
    ) == {"华为概念": "885806"}


def test_ths_quote_skips_unsupported_code_without_network_call():
    calls = []

    def get_market_concept_current_ths(index_code=None):
        calls.append(index_code)
        return []

    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(
                get_market_concept_current_ths=get_market_concept_current_ths,
            ),
            info=SimpleNamespace(),
        ),
    )
    service = RealtimeSectorService(fake_adata)

    result = service.get_sector_quotes(["700632"], source="ths")

    assert result["ok"] is False
    assert result["missing"] == ["700632"]
    assert calls == []


def test_sector_quote_failure_is_negative_cached():
    calls = []

    def get_market_concept_current_ths(index_code=None):
        calls.append(index_code)
        raise RuntimeError("temporary failure")

    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(
                get_market_concept_current_ths=get_market_concept_current_ths,
            ),
            info=SimpleNamespace(),
        ),
    )
    service = RealtimeSectorService(fake_adata, ttl_seconds=2)

    service.get_sector_quotes(["885806"], source="ths")
    service.get_sector_quotes(["885806"], source="ths")

    assert calls == ["885806"]


def test_sector_quote_uses_previous_close_when_provider_omits_change_pct(monkeypatch):
    fake_adata = SimpleNamespace(
        stock=SimpleNamespace(
            market=SimpleNamespace(
                get_market_concept_current_ths=lambda index_code=None: [{
                    "index_code": index_code,
                    "trade_date": "2026-08-18",
                    "price": 110.0,
                    "change_pct": None,
                }],
            ),
            info=SimpleNamespace(),
        ),
    )
    service = RealtimeSectorService(fake_adata, ttl_seconds=30)
    monkeypatch.setattr(service, "_previous_close", lambda *_args: 100.0)

    result = service.get_sector_quotes(["886001"], source="ths")

    assert result["ok"] is True
    assert result["sectors"][0]["pre_close"] == 100.0
    assert round(result["sectors"][0]["change_pct"], 2) == 10.0
