"""Read-only sector evidence audit; never publishes quotes or changes accounts."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.realtime.models import QuoteSnapshot, normalize_stock_code
from core.realtime.trading_session import shanghai_now


def classify_quote(raw, date, *, now, stale_seconds):
    if not isinstance(raw, dict):
        return "cache_missing"
    if str(raw.get("date") or "").replace("-", "") != date:
        return "wrong_session"
    quote = QuoteSnapshot.from_raw(raw, stale_after_seconds=stale_seconds)
    quote = quote.with_freshness(stale_after_seconds=stale_seconds, now=now)
    if quote.last_price <= 0 or quote.pre_close <= 0:
        return "invalid_price_or_reference"
    if not quote.received_at:
        return "missing_receipt"
    if quote.is_stale:
        return "cache_stale"
    return "usable"


def summarize_members(codes, raw_quotes, subscriptions, date, *, now, stale_seconds):
    codes = sorted(set(codes))
    states = {code: classify_quote(raw_quotes.get(code), date, now=now,
                                  stale_seconds=stale_seconds) for code in codes}
    counts = Counter(states.values())
    return {
        "member_count": len(codes), "quote_states": dict(counts),
        "fresh_coverage": round(counts["usable"] / len(codes), 4) if codes else 0,
        "not_subscribed_codes": [code for code in codes if code not in subscriptions],
        "unusable_codes": {code: state for code, state in states.items() if state != "usable"},
    }


def build_report(date, *, codes=(), minute="09:36:00", probe_source=False):
    from config.settings import (CACHE_DIR, REALTIME_QUOTE_STALE_SECONDS,
                                 REDIS_URL, WEB_DATA_DIR)
    from backtest.trade_calendar import TradeCalendar
    from core.infrastructure.shared_state import get_shared_state_backend
    from core.operations.preopen_evidence import PreopenEvidenceStore
    from core.realtime.quote_cache import RealtimeQuoteCache
    from core.realtime.sector_quote_minutes import SectorQuoteMinuteRepository
    from core.realtime.sector_service import RealtimeSectorService
    from core.realtime.watchlist_repository import RealtimeWatchlistRepository
    from core.signals.sector_confirmation import sector_names_from_row

    now = shanghai_now()
    backend = get_shared_state_backend()
    prior = TradeCalendar().prev(date)
    bundle = PreopenEvidenceStore().load(prior, date)
    inputs = bundle["inputs"] if bundle else {}
    members = RealtimeSectorService.confirmation_members(inputs)
    pool = [*(inputs.get("leader_pools", {}).get("10", {}).get("rows") or [])[:30],
            *(inputs.get("candidates") or [])[:20]]
    wanted = {normalize_stock_code(code, add_suffix=False) for code in codes}
    rows = {normalize_stock_code(row.get("code"), add_suffix=False): row for row in pool
            if not wanted or normalize_stock_code(row.get("code"), add_suffix=False) in wanted}
    names = set().union(*(sector_names_from_row(row) for row in rows.values())) if rows else set()
    universe = sorted({code for name in names for code in members.get(name, [])})
    keys = [RealtimeQuoteCache._quote_key(code) for code in universe]
    values = backend.get_many_json(keys)
    raw_quotes = {code: values.get(key) for code, key in zip(universe, keys, strict=True)}
    watchlists = RealtimeWatchlistRepository(backend=backend)
    subscribed = set(watchlists.read_all())
    execution = set(watchlists.read_all(minutes_only=True))
    quote_minutes = SectorQuoteMinuteRepository(backend=backend).read_prices(date, universe, until=minute)
    previous = {normalize_stock_code(row.get("code") or row.get("ts_code"), add_suffix=False): row.get("close")
                for row in inputs.get("daily_rows") or []}
    groups = {}
    for name in sorted(names):
        group = members.get(name, [])
        summary = summarize_members(group, raw_quotes, subscribed, date, now=now,
                                    stale_seconds=REALTIME_QUOTE_STALE_SECONDS)
        missing_minutes = [code for code in group if minute not in quote_minutes.get(code, {})]
        groups[name] = {
            "sector_code": (inputs.get("sector_codes") or {}).get(name),
            "verified_binding": bool(group), **summary,
            "archived_quote_minute": minute,
            "archived_quote_minute_count": len(group) - len(missing_minutes),
            "missing_archived_quote_minute_codes": missing_minutes,
            "missing_frozen_previous_close_codes": [code for code in group if not previous.get(code)],
        }
    issues = []
    if not bundle:
        issues.append("missing_preopen_bundle: cannot reconstruct server candidate and constituent universe")
    if wanted - set(rows):
        issues.append("requested_codes_absent_from_local_frozen_pools")
    if universe and set(universe) - subscribed:
        issues.append("constituents_not_in_collector_watchlist")
    if not backend.is_shared:
        issues.append("process_local_memory: separate Web and collector processes cannot share quotes")
    try:
        version = importlib.metadata.version("eltdx")
    except importlib.metadata.PackageNotFoundError:
        version = "not_installed_in_this_process"
    report = {
        "classification": "read_only_sector_data_diagnostic", "generated_at": now.isoformat(),
        "market_date": date, "candidate_date": prior, "eltdx_version": version,
        "redis_configured": bool(REDIS_URL), "cache_storage": "redis" if backend.is_shared else "memory",
        "preopen_bundle_id": bundle["bundle_id"] if bundle else None,
        "cache_directory": str(CACHE_DIR), "web_data_directory": str(WEB_DATA_DIR),
        "quote_stale_seconds": REALTIME_QUOTE_STALE_SECONDS,
        "current_quote_audit_is_historical_proof": False,
        "outside_continuous_session": not (now.strftime("%Y%m%d") == date and
            ("09:30:00" <= now.strftime("%H:%M:%S") <= "11:30:00" or
             "13:00:00" <= now.strftime("%H:%M:%S") <= "15:00:00")),
        "collector_health": backend.get_json(RealtimeQuoteCache.COLLECTOR_HEALTH_KEY) or {},
        "quote_cache_meta": backend.get_json(RealtimeQuoteCache.META_KEY) or {},
        "watchlist_count": len(subscribed), "execution_watchlist_count": len(execution),
        "constituent_universe_count": len(universe), "issues": issues,
        "stocks": [{"code": code, "sector_names": sorted(sector_names_from_row(row))}
                   for code, row in rows.items()],
        "groups": groups,
        "limitations": [
            "Freshness describes the current cache only; after-hours stale quotes do not prove an intraday outage.",
            "Archived quote points are separate from full minute bars; absent points may be covered by historical CSV/minute cache.",
            "Frozen previous closes are audited separately; replay may also read prior-day local daily data.",
            "A source probe is a new receipt, never proof of timely historical delivery.",
        ],
    }
    if probe_source:
        import time
        from core.data.providers.eltdx_provider import EltdxProvider
        from config.settings import ELTDX_HOST, ELTDX_TIMEOUT_SECONDS
        sample = list(dict.fromkeys([*sorted(wanted or rows), *universe[:80]]))[:80]
        provider = EltdxProvider(timeout=ELTDX_TIMEOUT_SECONDS, host=ELTDX_HOST or None)
        start = time.monotonic()
        quotes = provider.get_quote_snapshots(sample)
        report["source_probe"] = {
            "receipt_scope": "current_only_not_historical", "requested": sample,
            "returned_count": len(quotes), "duration_seconds": round(time.monotonic() - start, 3),
            "diagnostics": provider.last_quote_diagnostics,
        }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--codes", default="")
    parser.add_argument("--minute", default="09:36:00")
    parser.add_argument("--probe-source", action="store_true", help="Read at most 80 current source quotes; never publish them")
    args = parser.parse_args()
    datetime.strptime(args.date, "%Y%m%d")
    datetime.strptime(args.minute, "%H:%M:%S")
    report = build_report(args.date, codes=[c.strip() for c in args.codes.split(",") if c.strip()],
                          minute=args.minute, probe_source=args.probe_source)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
