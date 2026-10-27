"""Live strategy data boundary: storage reads only, no market providers."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from core.realtime.persistent_market import PersistentQuoteProvider
from core.realtime.models import normalize_stock_code


class ReadOnlyMarketData:
    def __init__(self, *, provider=None, cache_dir=None, frozen=None):
        from config.settings import CACHE_DIR
        self.provider = provider or PersistentQuoteProvider()
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.frozen = frozen

    def get_quote_snapshots(self, codes):
        return self.provider.get_quote_snapshots(codes)

    def get_quote_snapshot(self, code):
        return self.provider.get_quote_snapshot(code)

    def get_minute_bars_live(self, code, date):
        return self.provider.get_minute_bars(code, date)

    def get_realtime_snapshot_ticks(self, code, date):
        return self.provider.get_snapshot_ticks(code, date)

    def get_all_stocks_daily(self, date):
        if self.frozen and self.frozen["candidate_date"] == date:
            return pd.DataFrame(self.frozen["inputs"].get("daily_rows") or [])
        path = self.cache_dir / "stock" / "all_daily" / f"{date}.csv"
        return pd.read_csv(path, dtype={"code": str, "ts_code": str}) if path.exists() else pd.DataFrame()

    def get_market_evidence(self, code, date, kind, *, now=None, stale_seconds=15):
        """Observational data only. Reading this never repairs or fetches evidence."""
        from core.realtime.evidence_store import EvidenceStore
        from core.realtime.market_evidence import EVIDENCE_KINDS, timestamp
        from core.realtime.trading_session import shanghai_now
        if kind not in EVIDENCE_KINDS - {"evidence_publication", "evidence_quality"}:
            raise ValueError("Unsupported observational evidence kind")
        code6 = normalize_stock_code(code, add_suffix=False)
        row = self.provider.cache.backend.get_json(EvidenceStore.key(date, code6, kind))
        if not row:
            return {}
        age = (shanghai_now(now) - timestamp(row["received_at"])).total_seconds()
        return {**row, "age_seconds": age, "is_stale": not 0 <= age <= stale_seconds,
                "formal_eligible": False}

    def get_auction_data(self, code, date):
        code6 = normalize_stock_code(code, add_suffix=False)
        if isinstance(self.provider, PersistentQuoteProvider):
            rows = self.provider._view(date).events("auction", [code6], latest=True)
            return rows[0] if rows else {}
        value = self.provider.cache.backend.get_json(f"realtime:auction:{date}:{code6}")
        if value:
            return value
        path = self.cache_dir / "stock" / "auction" / f"{normalize_stock_code(code, add_suffix=True)}_{date}.json"
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("数据源") in {"eltdx", "eltdx_0925", "eltdx_open", "minute_tick"}:
                return value
        return {}
