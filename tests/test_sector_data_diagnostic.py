from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.diagnose_sector_data import classify_quote, summarize_members


NOW = datetime(2026, 10, 21, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def quote(**changes):
    return {"code": "000001", "date": "20261021", "time": "09:39:59",
            "received_at": "2026-10-21T09:39:59+08:00", "last_price": 11,
            "pre_close": 10, "source": "eltdx_batch", **changes}


def classify(raw):
    return classify_quote(raw, "20261021", now=NOW, stale_seconds=12)


def test_distinguishes_missing_wrong_session_stale_and_invalid_quotes():
    assert classify(None) == "cache_missing"
    assert classify(quote(date="20261020")) == "wrong_session"
    assert classify(quote(time="09:39:00", received_at="2026-10-21T09:39:00+08:00")) == "cache_stale"
    assert classify(quote(last_price=0)) == "invalid_price_or_reference"
    assert classify(quote(pre_close=0)) == "invalid_price_or_reference"
    assert classify(quote(received_at="")) == "missing_receipt"
    assert classify(quote()) == "usable"


def test_subscription_gap_is_independent_of_cache_availability():
    result = summarize_members(["000001", "000002", "000001"], {"000001": quote()},
                               {"000002"}, "20261021", now=NOW, stale_seconds=12)
    assert result["member_count"] == 2
    assert result["quote_states"] == {"usable": 1, "cache_missing": 1}
    assert result["not_subscribed_codes"] == ["000001"]
    assert result["fresh_coverage"] == 0.5


def test_empty_members_never_report_full_coverage():
    result = summarize_members([], {}, set(), "20261021", now=NOW, stale_seconds=12)
    assert result["fresh_coverage"] == 0
    assert result["quote_states"] == {}


def test_future_receipt_and_source_stale_flag_remain_unusable():
    assert classify(quote(received_at="2026-10-21T09:40:01+08:00")) == "cache_stale"
    assert classify(quote(is_stale=True)) == "cache_stale"


def test_report_only_reads_shared_state_and_reports_binding_and_archive_gaps(monkeypatch, tmp_path):
    from config import settings
    from core.infrastructure import shared_state
    from core.operations.preopen_evidence import PreopenEvidenceStore
    from scripts import diagnose_sector_data as diagnostic

    class ReadOnlyBackend:
        is_shared = True

        def get_json(self, key):
            return {"realtime:watchlists:index": ["members"],
                    "realtime:watchlists:members": {"codes": ["000002"], "minutes": False}}.get(key)

        def get_many_json(self, keys):
            key = "realtime:quotes:latest:000002"
            return {key: quote(code="000002")} if key in keys else {}

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(shared_state, "get_shared_state_backend", lambda: ReadOnlyBackend())
    monkeypatch.setattr(diagnostic, "shanghai_now", lambda: NOW)
    monkeypatch.setattr(PreopenEvidenceStore, "load", lambda *a: {"bundle_id": "test", "inputs": {
        "candidates": [{"code": "000001", "sector_names": ["bound", "unbound"]}],
        "sector_codes": {"bound": "885001"},
        "sector_members": {"bound": ["000002", "000003"]},
        "daily_rows": [{"code": "000002", "close": 10}]}})
    report = diagnostic.build_report("20261021", codes=["000001"])
    assert report["preopen_bundle_id"] == "test"
    assert report["groups"]["bound"]["fresh_coverage"] == 0.5
    assert report["groups"]["bound"]["not_subscribed_codes"] == ["000003"]
    assert report["groups"]["bound"]["archived_quote_minute_count"] == 0
    assert report["groups"]["unbound"]["verified_binding"] is False
    assert "source_probe" not in report
    assert report["execution_watchlist_count"] == 0
    assert list(tmp_path.iterdir()) == []
