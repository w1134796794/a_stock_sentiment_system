"""Read-only, time-bounded views over the durable spool and daily archives."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from core.infrastructure.file_mutex import file_mutex
from core.realtime.models import normalize_stock_code, QuoteSnapshot
from core.realtime.trading_session import shanghai_now


def timestamp(value):
    return shanghai_now(datetime.fromisoformat(str(value))).timestamp()


class RetiredMarketDay(ValueError):
    pass


class PersistentMarketReader:
    def __init__(self, root=None, *, clock=shanghai_now):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or Path(WEB_DATA_DIR) / "market_archive")
        self.clock = clock

    def view(self, date, *, asof=None):
        datetime.strptime(date, "%Y%m%d")
        at = shanghai_now(asof or self.clock())
        watermark = 0
        if self.root.exists():
            with file_mutex(self.root / ".lifecycle.lock"):
                for path in self._paths(date):
                    with self._connect(path) as con:
                        cols = self._columns(con)
                        if "write_seq" in cols:
                            watermark = max(watermark, con.execute(
                                "SELECT coalesce(max(write_seq),0) FROM market_events").fetchone()[0])
        return PersistentMarketView(self, date, at, watermark)

    def _paths(self, date):
        return [p for p in (self.root / "ingestion.sqlite3", self.root / f"market_{date}.sqlite3") if p.is_file()]

    @staticmethod
    @contextmanager
    def _connect(path):
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def _columns(con):
        return {r[1] for r in con.execute("PRAGMA table_info(market_events)")}


class PersistentMarketView:
    def __init__(self, reader, date, asof, watermark):
        self.reader, self.date, self.asof, self.watermark = reader, date, asof, watermark
        self.audit = []

    def _at(self, value):
        if isinstance(value, datetime):
            return shanghai_now(value).timestamp()
        if value is None:
            return self.asof.timestamp()
        text = str(value)
        if len(text) in {5, 8}:
            text = f"{self.date[:4]}-{self.date[4:6]}-{self.date[6:]}T{text}"
        return timestamp(text)

    def events(self, kind, codes, *, start=None, end=None, latest=False):
        codes = list(codes)
        try:
            return self._events(kind, codes, start=start, end=end, latest=latest)
        except Exception as exc:
            self.audit.append({"kind": kind, "status": "retired" if isinstance(exc, RetiredMarketDay) else "read_error", "error_type": type(exc).__name__,
                "error": str(exc), "requested_codes": codes[:10], "requested_code_count": len(codes),
                "requested_start": str(start), "requested_end": str(end),
                "evaluation_asof": self.asof.isoformat(), "watermark": self.watermark})
            raise

    def _events(self, kind, codes, *, start=None, end=None, latest=False):
        from time import monotonic
        started = monotonic()
        wanted = sorted({str(c) if kind == "board_snapshot" else normalize_stock_code(c, add_suffix=False) for c in codes})
        lower = self._at(start) if start is not None else float("-inf")
        upper = min(self._at(end), self.asof.timestamp())
        selected = {}
        if wanted and self.reader.root.exists():
            # Readers, archiving and rollover share this short lock. A move can
            # never disappear between the spool read and the archive read.
            with file_mutex(self.reader.root / ".lifecycle.lock", timeout=2):
                marker = self.reader.root / "active_day.json"
                if marker.exists() and self.date < json.loads(marker.read_text())["market_date"]:
                    raise RetiredMarketDay("该日期的原始盘中行情已按保留规则清理；请查询debug或另存的回放输入")
                for path in self.reader._paths(self.date):
                    with self.reader._connect(path) as con:
                        columns = self.reader._columns(con)
                        groups = [[c] for c in wanted] if latest else [wanted[i:i+400] for i in range(0, len(wanted), 400)]
                        for group in groups:
                            clauses = ["kind=?", "code IN (" + ",".join("?" for _ in group) + ")"]
                            params = [kind, *group]
                            if "market_date" in columns:
                                clauses.append("market_date=?")
                                params.append(self.date)
                            if "write_seq" in columns:
                                clauses.append("(write_seq IS NULL OR write_seq<=?)")
                                params.append(self.watermark)
                            if "persisted_ts" in columns:
                                clauses.append("(persisted_ts IS NULL OR persisted_ts<=?)")
                                params.append(self.asof.timestamp())
                            if "received_ts" in columns:
                                clauses.append("(received_ts IS NULL OR received_ts<=?)")
                                params.append(self.asof.timestamp())
                            if "event_ts" in columns:
                                clauses.append("(event_ts IS NULL OR (event_ts>=? AND event_ts<=?))")
                                params.extend((lower, upper))
                            query = "SELECT * FROM market_events WHERE " + " AND ".join(clauses)
                            if latest:
                                ordering = "received_ts DESC,write_seq DESC" if "received_ts" in columns else "received_at DESC"
                                query += " ORDER BY " + ordering + " LIMIT 1"
                            for source in con.execute(query, params):
                                row = dict(source)
                                payload = json.loads(row["payload"])
                                try:
                                    received = timestamp(row["received_at"])
                                except (ValueError, TypeError):
                                    continue
                                if received > self.asof.timestamp():
                                    continue
                                payload["_event_id"] = row["event_id"]
                                payload["_write_seq"] = row.get("write_seq")
                                payload["_persisted_ts"] = row.get("persisted_ts")
                                payload["_legacy_visibility_unknown"] = row.get("persisted_ts") is None
                                selected[row["event_id"]] = payload
        rows = sorted(selected.values(), key=lambda r: (timestamp(r["received_at"]), r.get("_write_seq") or 0))
        if latest:
            by_code = {r["code"]: r for r in rows}
            rows = list(by_code.values())
        self.audit.append({"kind": kind, "requested_codes": wanted[:10], "requested_code_count": len(wanted), "requested_start": str(start),
            "requested_end": str(end), "evaluation_asof": self.asof.isoformat(), "watermark": self.watermark,
            "event_count": len(rows), "first_received_at": rows[0].get("received_at") if rows else None,
            "last_received_at": rows[-1].get("received_at") if rows else None,
            "event_ids": [r["_event_id"] for r in rows] if len(rows) <= 10 else [],
            "legacy_visibility_unknown": any(r["_legacy_visibility_unknown"] for r in rows),
            "status": "ok" if rows else "no_records" if self.reader._paths(self.date) else "missing_store",
            "storage": "durable_spool_and_archive", "duration_ms": round((monotonic()-started)*1000, 3)})
        return rows

    def snapshots(self, codes, *, start=None, end=None, latest=False):
        codes = list(codes)
        rows = self.events("snapshot", codes, start=start, end=end, latest=latest)
        lower = self._at(start) if start is not None else float("-inf")
        upper = min(self._at(end), self.asof.timestamp())
        valid = []
        for row in rows:
            try:
                event_at = self._at(row["time"])
            except (ValueError, KeyError):
                continue
            if lower <= event_at <= upper:
                # Existing archives remain inspectable; missing persistence
                # provenance must not establish timely executable confirmation.
                if row["_legacy_visibility_unknown"]:
                    row["quality_ok"] = False
                valid.append(row)
        gaps = []
        receipts = {}
        for row in valid:
            previous = receipts.get(row["code"])
            received = timestamp(row["received_at"])
            if previous is not None:
                gaps.append(received-previous)
            receipts[row["code"]] = received
        self.audit[-1].update(actual_snapshot_count=len(valid), max_snapshot_gap_seconds=max(gaps) if gaps else None,
                             missing_codes=sorted(set(normalize_stock_code(c, add_suffix=False) for c in codes)-set(receipts)))
        return valid

    def minutes(self, code, *, start="09:30:00", end=None):
        rows = self.events("minutes", [code], start=start, end=end)
        result = {}
        lower, upper = self._at(start), min(self._at(end), self.asof.timestamp())
        for event in rows:
            for bar in event.get("bars") or []:
                text = str(bar.get("time") or bar.get("datetime") or "")
                try:
                    at = self._at(text)
                except (ValueError, TypeError):
                    continue
                if lower <= at <= upper:
                    result[at] = dict(bar)
        self.audit[-1]["actual_minute_count"] = len(result)
        return pd.DataFrame([result[k] for k in sorted(result)])

    def sector_points(self, codes, *, minutes):
        if not minutes:
            return {}
        rows = self.events("sector_minutes", codes, start=min(minutes), end=max(minutes))
        selected = {}
        for event in rows:
            for point in event.get("points") or []:
                code, minute = point.get("code") or event["code"], point.get("time")
                if minute not in minutes or point.get("source") != "eltdx_batch":
                    continue
                try:
                    age = self._at(minute) - timestamp(point["received_at"])
                except (KeyError, ValueError, TypeError):
                    continue
                if 0 <= age < 60 and float(point.get("price") or 0) > 0:
                    selected.setdefault(code, {})[minute] = dict(point)
        self.audit[-1].update(actual_point_count=sum(len(values) for values in selected.values()),
                             observed_code_count=len(selected), requested_minutes=list(minutes))
        return selected


class PersistentQuoteProvider:
    """Keep Redis for coordination/health, use durable records for rule inputs."""
    def __init__(self, *, backend=None, reader=None, cache=None):
        from core.realtime.quote_cache import RealtimeQuoteCache
        self.cache = cache or RealtimeQuoteCache(backend=backend)
        self.reader = reader or PersistentMarketReader()
        self.current_view = None

    @property
    def available(self):
        return self.reader.root.is_dir()

    def begin(self, date, *, asof=None):
        self.current_view = self.reader.view(date, asof=asof)
        return self.current_view

    def _view(self, date=None):
        if date is None and self.current_view is not None:
            return self.current_view
        date = date or shanghai_now().strftime("%Y%m%d")
        if self.current_view is None or self.current_view.date != date:
            return self.reader.view(date)
        return self.current_view

    def get_quote_snapshots(self, codes):
        view = self._view()
        from config.settings import REALTIME_QUOTE_STALE_SECONDS
        result = {}
        for row in view.snapshots(codes, latest=True):
            item = QuoteSnapshot.from_raw(row, stale_after_seconds=REALTIME_QUOTE_STALE_SECONDS)
            value = item.with_freshness(stale_after_seconds=REALTIME_QUOTE_STALE_SECONDS, now=view.asof).to_dict(include_raw=False)
            result[item.code] = value
        return result

    def get_quote_snapshot(self, code):
        return self.get_quote_snapshots([code]).get(normalize_stock_code(code, add_suffix=False), {})

    def get_snapshot_ticks(self, code, date):
        view = self._view(date)
        return pd.DataFrame(view.snapshots([code], start=view.asof-timedelta(seconds=180)))

    def get_minute_bars(self, code, date):
        return self._view(date).minutes(code)

    def health(self):
        return self.cache.health()
