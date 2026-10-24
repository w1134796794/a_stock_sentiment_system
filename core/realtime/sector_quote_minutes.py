"""Retain actual constituent quote points, not synthetic OHLC or live proof."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now
from core.signals.opening import positive_price


class SectorQuoteMinuteRepository:
    def __init__(self, cache_dir=None, *, backend=None, journal=None, clock=None):
        from config.settings import CACHE_DIR, REALTIME_QUOTE_STALE_SECONDS
        self.root = Path(cache_dir or CACHE_DIR) / "stock" / "sector_quote_minutes"
        self.backend = backend or get_shared_state_backend()
        self.stale_seconds = REALTIME_QUOTE_STALE_SECONDS
        self._pending = {}
        self._history = {}
        self._lock = RLock()
        self.journal = journal
        self.clock = clock or shanghai_now

    @staticmethod
    def _key(date, code):
        return f"realtime:sector-quote-minutes:{date}:{code}"

    def record_batch(self, rows, *, now=None):
        clock = shanghai_now(now)
        date = clock.strftime("%Y%m%d")
        boundary = clock.replace(second=0, microsecond=0)
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if key[0] == date}
            for raw in rows:
                code = normalize_stock_code(raw.get("code"), add_suffix=False)
                price = positive_price(raw.get("last_price"))
                if (len(code) != 6 or not price or raw.get("source") != "eltdx_batch"
                        or raw.get("is_stale") or raw.get("quality_ok") is False
                        or str(raw.get("date") or "").replace("-", "") != date):
                    continue
                try:
                    stamp = shanghai_now(datetime.fromisoformat(str(raw.get("received_at") or "")))
                except (ValueError, TypeError):
                    continue
                if (stamp.strftime("%Y%m%d") != date or not 0 <= (clock - stamp).total_seconds() <= self.stale_seconds
                        or not "09:30:00" <= stamp.strftime("%H:%M:%S") <= "15:00:00"
                        or "11:30:00" < stamp.strftime("%H:%M:%S") < "13:00:00"):
                    continue
                # A 09:35:58 observation belongs to the minute closing at 09:36,
                # never to 09:35. No carry-forward across an unobserved minute.
                end = stamp.replace(second=0, microsecond=0)
                if stamp > end:
                    end += timedelta(minutes=1)
                minute = end.strftime("%H:%M:%S")
                if minute in {"09:30:00", "13:00:00"} or minute > "15:00:00":
                    continue
                value = {"code": code, "time": minute, "price": price,
                         "open_price": positive_price(raw.get("open_price")),
                         "pre_close": positive_price(raw.get("pre_close")),
                         "received_at": stamp.isoformat(timespec="milliseconds"), "source": "eltdx_batch"}
                key = date, code, minute
                if value["received_at"] > self._pending.get(key, {}).get("received_at", ""):
                    self._pending[key] = value
            closed = {key: value for key, value in self._pending.items() if key[2] <= boundary.strftime("%H:%M:%S")}
            if not closed:
                return 0
            self.root.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.root / f"{date}.sqlite3", timeout=5) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("CREATE TABLE IF NOT EXISTS quote_minutes(code TEXT, minute TEXT, received_at TEXT, payload TEXT, "
                            "PRIMARY KEY(code, minute))")
                con.executemany("INSERT INTO quote_minutes VALUES (?,?,?,?) ON CONFLICT(code,minute) DO UPDATE SET "
                                "received_at=excluded.received_at,payload=excluded.payload "
                                "WHERE excluded.received_at>quote_minutes.received_at",
                                [(code, minute, row["received_at"], json.dumps(row))
                                 for (_, code, minute), row in closed.items()])
            self._write_shared(date, closed)
            if self.journal is not None:
                published = shanghai_now(self.clock()).isoformat()
                by_code = {}
                for (_, code, _), row in closed.items():
                    by_code.setdefault(code, []).append(row)
                self.journal.append("sector_minutes", date, [
                    {"code": code, "date": date, "received_at": published,
                     "source": "collector_closed_quote_minutes", "points": points}
                    for code, points in by_code.items()])
            for key in closed:
                del self._pending[key]
            return len(closed)

    def _write_shared(self, date, closed):
        client = getattr(self.backend, "client", None)
        key_builder = getattr(self.backend, "_key", None)
        if client is not None and callable(key_builder):
            # Update one minute field, not a growing full-day JSON on every tick.
            pipe = client.pipeline(transaction=False)
            for (_, code, minute) in closed:
                pipe.hget(key_builder(self._key(date, code)), minute)
            existing = pipe.execute()
            pipe = client.pipeline(transaction=False)
            for ((_, code, minute), row), old in zip(closed.items(), existing, strict=True):
                prior = json.loads(old) if old else {}
                if row["received_at"] > prior.get("received_at", ""):
                    key = key_builder(self._key(date, code))
                    pipe.hset(key, minute, json.dumps(row))
                    pipe.expire(key, 2 * 86400)
            pipe.execute()
        else:
            shared = {}
            missing = sorted({self._key(date, code) for _, code, _ in closed} - self._history.keys())
            retained = self.backend.get_many_json(missing)
            for key in missing:
                self._history[key] = {r["time"]: r for r in retained.get(key) or []}
            for (_, code, minute), row in closed.items():
                key = self._key(date, code)
                history = self._history[key]
                if row["received_at"] > history.get(minute, {}).get("received_at", ""):
                    history[minute] = row
                shared[key] = list(history.values())
            self.backend.set_many_json(shared, 2 * 86400)
            self._history = {key: value for key, value in self._history.items() if f":{date}:" in key}

    def read_points(self, date, codes, *, until="15:00:00", minutes=None):
        date = str(date).replace("-", "")
        datetime.strptime(date, "%Y%m%d")
        wanted = {normalize_stock_code(code, add_suffix=False) for code in codes}
        keys = [self._key(date, code) for code in sorted(wanted)]
        selected_minutes = set(minutes) if minutes is not None else None
        client = getattr(self.backend, "client", None)
        key_builder = getattr(self.backend, "_key", None)
        if client is not None and callable(key_builder):
            pipe = client.pipeline(transaction=False)
            for key in keys:
                if selected_minutes is None:
                    pipe.hvals(key_builder(key))
                else:
                    pipe.hmget(key_builder(key), sorted(selected_minutes))
            rows = [json.loads(raw) for values in pipe.execute() for raw in values if raw]
        else:
            rows = [row for values in self.backend.get_many_json(keys).values() for row in values or []]
        path = self.root / f"{date}.sqlite3"
        if wanted and path.exists():
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
                # Avoid SQL variable limits on large constituent universes.
                if selected_minutes:
                    placeholders = ",".join("?" for _ in selected_minutes)
                    rows.extend(json.loads(row[0]) for row in con.execute(
                        f"SELECT payload FROM quote_minutes WHERE minute<=? AND minute IN ({placeholders})",
                        (until, *sorted(selected_minutes))))
                elif selected_minutes is None:
                    rows.extend(json.loads(row[0]) for row in con.execute(
                        "SELECT payload FROM quote_minutes WHERE minute<=?", (until,)))
        selected = {}
        for row in rows:
            code, minute = row.get("code"), row.get("time", "")
            price = positive_price(row.get("price"))
            if code not in wanted or not price or minute > until or row.get("source") != "eltdx_batch":
                continue
            if selected_minutes is not None and minute not in selected_minutes:
                continue
            try:
                stamp = shanghai_now(datetime.fromisoformat(row["received_at"]))
                end = shanghai_now(datetime.strptime(date + minute, "%Y%m%d%H:%M:%S"))
            except (ValueError, KeyError, TypeError):
                continue
            if stamp.strftime("%Y%m%d") != date or not 0 <= (end - stamp).total_seconds() < 60:
                continue
            key = code, minute
            if row["received_at"] > selected.get(key, {}).get("received_at", ""):
                selected[key] = row
        result = {}
        for (code, minute), row in selected.items():
            result.setdefault(code, {})[minute] = dict(row)
        return result

    def read_prices(self, date, codes, *, until="15:00:00"):
        points = self.read_points(date, codes, until=until)
        return {code: {minute: row["price"] for minute, row in values.items()}
                for code, values in points.items()}
