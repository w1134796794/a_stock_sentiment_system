"""Research adapters preserve source, time, prices and append-only provenance."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

from core.data.providers.eltdx_provider import EltdxProvider
from core.realtime.research_jobs import audit_prices, run_research
from core.realtime.research_store import ResearchStore


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def provider(**apis):
    client = Client()
    for key, value in apis.items():
        setattr(client, key, value)
    value = EltdxProvider()
    value._client = lambda: client
    return value


def bar(day="2026-10-22", close=10):
    return NS(time=datetime.fromisoformat(day), open=10, high=11, low=9, close=close, volume_lots=20, amount=20000)


def research(kind="bars_raw", captured="2026-10-24T15:00:00+08:00", status="ok"):
    return dict(
        schema_version=1,
        source="eltdx",
        sdk_version="3.3.0",
        code="000001",
        kind=kind,
        start="20261021",
        end="20261023",
        period="day",
        adjust="none" if kind == "bars_raw" else None,
        rows=[dict(date="20261022", event_time="2026-10-22T00:00:00+08:00", open=10, high=11, low=9, close=10)],
        coverage={},
        quality_status=status,
        quality_reasons=[],
        captured_at=captured,
        formal_eligible=False,
        research_only=True,
        historical_eligible=False,
        availability_basis="captured_at_only",
    )


def test_bars_range_order_unit_and_adjustment_are_explicit():
    calls = []

    def get(code, **kwargs):
        calls.append(kwargs)
        return NS(code="000001", bars=[bar("2026-10-23"), bar("2026-10-22"), bar("2026-10-20")])

    api = provider(bars=NS(get=get))
    value = api.get_research("bars_raw", "000001", "20261021", "20261023")
    assert value["quality_status"] == "ok" and len(value["rows"]) == 2
    assert [r["date"] for r in value["rows"]] == ["20261022", "20261023"]
    assert value["rows"][0]["volume_hand"] == 20 and value["rows"][0]["amount_yuan"] == 20000
    assert calls[0]["adjust"] == "none" and "bar_time" not in calls[0] and calls[0]["all_pages"]
    assert value["coverage"]["first_date"] == "20261022"
    assert not value["historical_eligible"]


def test_research_minute_keeps_native_0931():
    calls = []
    candle = bar("2026-10-23")
    candle.time = datetime(2026, 10, 23, 9, 31)
    def get(*args, **kwargs):
        calls.append(kwargs)
        return NS(code="000001", bars=[candle])
    value = provider(bars=NS(get=get)).get_research(
        "bars_raw", "000001", "20261023", "20261023", period="1m")
    assert value["quality_status"] == "ok"
    assert "bar_time" not in calls[0]
    assert "T09:31:00" in value["rows"][0]["event_time"]
    assert value["rows"][0]["bar_label"] == "end"


@pytest.mark.parametrize("kind,mode", [("bars_raw", "none"), ("bars_qfq", "qfq"), ("bars_hfq", "hfq")])
def test_adjustment_modes_never_mix(kind, mode):
    api = provider(bars=NS(get=lambda *a, **kw: NS(code="000001", bars=[bar()])))
    assert api.get_research(kind, "000001", "20261021", "20261023")["adjust"] == mode


def test_invalid_ohlc_is_not_silently_repaired():
    api = provider(bars=NS(get=lambda *a, **kw: NS(code="000001", bars=[bar(close=20)])))
    value = api.get_research("bars_raw", "000001", "20261021", "20261023")
    assert value["quality_status"] == "parse_error" and value["rows"] == []


def test_missing_security_is_unknown_not_delisted():
    api = provider(helpers=NS(latest_stock_list=lambda m: []))
    value = api.get_research("security_status", "000001", "20261021", "20261023")
    assert value["quality_status"] == "empty" and value["coverage"]["absence_means"] == "unknown"


def test_real_money_flow_dataclass_keeps_vendor_units():
    from eltdx.models import MoneyFlowBlock, MoneyFlowDaily

    row = MoneyFlowDaily(20261022, date(2026, 10, 22), 100000, (), 1200, 1.2, ())
    response = MoneyFlowBlock("sz", 0, "000001", (row,))
    api = provider(money_flow=NS(daily=lambda c: response))
    value = api.get_research("money_flow", "000001", "20261021", "20261023")
    assert value["rows"][0]["total_amount"] == 100000 and value["rows"][0]["main_net"] == 1200
    assert not value["coverage"]["interchangeable_with_tushare"]


def test_f10_real_response_has_no_invented_historical_publish_time():
    from eltdx.f10.models import F10Response, F10ResultSet

    response = F10Response(
        "example", {}, 0, (F10ResultSet("a", ("公司名称",), ({"公司名称": "测试<script>"},), (), {}),), {}
    )
    api = provider()
    api._f10_client = lambda: NS(company_profile=lambda c: response)
    value = api.get_research("company_profile", "000001", "20261021", "20261023")
    assert value["rows"][0]["values"]["公司名称"] == "测试<script>"
    assert value["rows"][0]["published_at"] is None
    assert not value["coverage"]["publication_time_verified"]


def test_f10_error_is_not_valid_empty_response():
    api = provider()
    api._f10_client = lambda: NS(announcements=lambda c: NS(ok=False, error_code=5))
    assert api.get_research("announcements", "000001", "20261021", "20261023")["quality_status"] == "request_error"


def test_research_store_reader_does_not_create_files(tmp_path):
    store = ResearchStore(tmp_path)
    assert store.read("000001", "bars_raw") == []
    assert store.coverage() == [] and store.jobs() == []
    assert list(tmp_path.iterdir()) == []


def test_store_versions_asof_and_idempotence(tmp_path):
    store = ResearchStore(tmp_path)
    old = research()
    first = store.write(old)
    assert store.write(old) == first
    fresh = research(captured="2026-10-25T15:00:00+08:00")
    second = store.write(fresh)
    assert second != first and len(store.read("000001", "bars_raw", limit=10)) == 2
    assert store.read("000001", "bars_raw", as_of="2026-10-24T08:00:00+00:00")[0]["version_id"] == first
    assert store.read("000001", "bars_raw", as_of="2026-10-23T15:00:00+08:00") == []
    assert store.coverage(date="20261024")[0]["versions"] == 1


def test_runner_resume_retries_failures_and_retains_versions(tmp_path):
    calls = []

    def get(kind, code, start, end, **kwargs):
        calls.append(kind)
        return research(kind=kind, status="ok" if kind == "bars_raw" or len(calls) > 2 else "network_error")

    api = NS(get_research=get)
    options = dict(codes=["000001"], kinds=["bars_raw", "money_flow"], start="20261021", end="20261023")
    assert run_research(tmp_path, api, **options)["state"] == "partial"
    assert run_research(tmp_path, api, **options)["state"] == "done"
    assert calls == ["bars_raw", "money_flow", "money_flow"]
    assert len(ResearchStore(tmp_path).read("000001", "money_flow", limit=10)) == 2


def test_source_audit_requires_same_unadjusted_identity():
    a = research()
    b = research()
    b["rows"] = [{**b["rows"][0], "close": 10.1}]
    report = audit_prices(a, b)
    assert len(report["differences"]) == 1 and not report["source_data_modified"]
    b["adjust"] = "qfq"
    with pytest.raises(ValueError):
        audit_prices(a, b)


def test_prefetch_research_dryrun_has_no_side_effects(tmp_path, monkeypatch, capsys):
    from scripts.prefetch_eltdx_research import main

    monkeypatch.setattr(
        sys,
        "argv",
        ["capture", "--start", "20261021", "--end", "20261023", "--codes", "000001", "--data-root", str(tmp_path)],
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert list(tmp_path.iterdir()) == []


def test_research_automation_disabled_and_postclose_only(monkeypatch):
    from core.realtime.research_automation import run_optional_research

    monkeypatch.delenv("ELTDX_RESEARCH_ENABLED", raising=False)
    assert run_optional_research("20261023")["status"] == "disabled"
    monkeypatch.setenv("ELTDX_RESEARCH_ENABLED", "true")
    assert run_optional_research("20261023", now=datetime(2026, 10, 23, 10))["status"] == "skipped"


def test_postclose_failure_is_separate_from_production(monkeypatch):
    import core.realtime.research_automation as module

    monkeypatch.setenv("ELTDX_RESEARCH_ENABLED", "true")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: NS(returncode=1))
    report = module.run_optional_research("20261023", now=datetime(2026, 10, 23, 20))
    assert report["status"] == "partial" and not report["production_pipeline_affected"]


def test_price_audit_cli_matches_only_requested_unadjusted_stock(tmp_path, monkeypatch, capsys):
    from scripts.audit_eltdx_research import main

    store = ResearchStore(tmp_path)
    store.write(research())
    reference = tmp_path / "reference.csv"
    content = "ts_code,trade_date,open,high,low,close\n000001.SZ,20261022,10,11,9,10\n600000.SH,20261022,1,2,1,1\n"
    reference.write_text(content)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit",
            "--code",
            "000001",
            "--reference",
            str(reference),
            "--reference-adjust",
            "none",
            "--data-root",
            str(tmp_path),
        ],
    )
    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["compared_count"] == 1 and not output["differences"]
    assert reference.read_text() == content
