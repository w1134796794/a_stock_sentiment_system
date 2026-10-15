"""Date-scoped eltdx opening facts, retained independently of rolling ticks."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import RLock

from core.infrastructure.atomic_json import atomic_write_json
from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now
from core.signals.opening import positive_price


def quote_opening(row, date, *, until="23:59:59"):
    """Validate original quote identity and receipt date, not a fabricated date."""
    code = normalize_stock_code(row.get("code"), add_suffix=False)
    opening, reference = positive_price(row.get("open_price")), positive_price(row.get("pre_close"))
    if (not code or str(row.get("date") or "").replace("-", "") != date
            or opening is None or reference is None
            or row.get("quality_ok") is False or row.get("is_stale") is True
            or row.get("source") != "eltdx_batch"):
        return None
    try:
        stamp = shanghai_now(datetime.fromisoformat(str(row.get("received_at") or "")))
    except (ValueError, TypeError):
        return None
    if stamp.strftime("%Y%m%d") != date or not "09:30:00" <= stamp.strftime("%H:%M:%S") <= until:
        return None
    return {"code": code, "market_date": date, "open_price": opening, "pre_close": reference,
            "source": "eltdx_batch", "received_at": stamp.isoformat(timespec="milliseconds")}


class SessionOpenRepository:
    def __init__(self, cache_dir=None, *, backend=None):
        from config.settings import CACHE_DIR
        self.root = Path(cache_dir or CACHE_DIR) / "stock" / "session_open"
        self.backend = backend or get_shared_state_backend()
        self._days = {}
        self._lock = RLock()

    @staticmethod
    def _date(value):
        text = str(value).replace("-", "")
        if datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d") != text:
            raise ValueError("Invalid session date")
        return text

    @staticmethod
    def _key(date):
        return f"realtime:session-open:{date}"

    def _load(self, date):
        path = self.root / f"{date}.json"
        payloads = []
        if path.exists():
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        shared = self.backend.get_json(self._key(date))
        if shared:
            payloads.append(shared)
        merged = {}
        for payload in payloads:
            if payload.get("market_date") != date:
                continue
            for code, revisions in (payload.get("rows") or {}).items():
                for row in revisions:
                    valid = quote_opening({**row, "date": row.get("market_date")}, date)
                    if valid and valid["code"] == code and valid not in merged.setdefault(code, []):
                        merged[code].append(valid)
        return merged

    def record_batch(self, rows, *, now=None):
        clock = shanghai_now(now)
        date = clock.strftime("%Y%m%d")
        if not "09:30:00" <= clock.strftime("%H:%M:%S") <= "15:05:00":
            return 0
        valid = [value for row in rows if (value := quote_opening(row, date, until=clock.strftime("%H:%M:%S")))]
        if not valid:
            return 0
        with self._lock:
            if date not in self._days:
                self._days[date] = self._load(date)
            records = self._days[date]
            updated = {code: list(values) for code, values in records.items()}
            changed = 0
            for row in valid:
                revisions = updated.setdefault(row["code"], [])
                latest = max(revisions, key=lambda item: item["received_at"]) if revisions else None
                if latest and (latest["open_price"], latest["pre_close"]) == (row["open_price"], row["pre_close"]):
                    continue
                if latest and row["received_at"] <= latest["received_at"]:
                    continue
                revisions.append(row)
                changed += 1
            if changed:
                payload = {"schema_version": 1, "market_date": date, "rows": updated}
                atomic_write_json(self.root / f"{date}.json", payload)
                # No short TTL: opening facts must outlive the 3-second rolling window.
                self.backend.set_json(self._key(date), payload)
                self._days = {date: updated}
            return changed

    def read(self, date, codes=None, *, until="23:59:59"):
        date = self._date(date)
        wanted = set(codes) if codes is not None else None
        result = {}
        for code, revisions in self._load(date).items():
            if wanted is not None and code not in wanted:
                continue
            eligible = [row for row in revisions if row["received_at"][11:19] <= until]
            if eligible:
                result[code] = max(eligible, key=lambda row: row["received_at"])
        return result
