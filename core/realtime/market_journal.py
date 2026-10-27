"""Durable ingestion spool and idempotent asynchronous daily market archive."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.realtime.trading_session import shanghai_now
from core.infrastructure.file_mutex import file_mutex


class MarketJournal:
    def __init__(self, root=None, *, clock=shanghai_now):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or (Path(WEB_DATA_DIR) / "market_archive"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ingestion.sqlite3"
        self.clock = clock
        with file_mutex(self.lifecycle_lock), self.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS market_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, market_date TEXT NOT NULL, code TEXT NOT NULL,
                received_at TEXT NOT NULL, payload TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)""")
            con.execute("CREATE INDEX IF NOT EXISTS pending_market_events ON market_events(archived,seq)")
            self._upgrade(con)
            con.execute("""CREATE TABLE IF NOT EXISTS evidence_cursors (
                cursor_key TEXT PRIMARY KEY, payload TEXT NOT NULL)""")

    @property
    def lifecycle_lock(self):
        return self.root / ".lifecycle.lock"

    @staticmethod
    def _upgrade(con):
        columns = {r[1] for r in con.execute("PRAGMA table_info(market_events)")}
        for name, kind in (("write_seq", "INTEGER"), ("persisted_ts", "REAL"),
                           ("received_ts", "REAL"), ("event_ts", "REAL")):
            if name not in columns:
                con.execute(f"ALTER TABLE market_events ADD COLUMN {name} {kind}")
        con.execute("CREATE INDEX IF NOT EXISTS market_kind_code_time ON market_events(kind,code,event_ts)")
        con.execute("CREATE INDEX IF NOT EXISTS market_write_seq ON market_events(write_seq)")
        con.execute("CREATE INDEX IF NOT EXISTS market_latest_receipt ON market_events(kind,code,received_ts DESC,write_seq DESC)")

    @staticmethod
    def _timestamp(value):
        try:
            return shanghai_now(datetime.fromisoformat(str(value))).timestamp()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _expand(kind, rows):
        # Store minute versions once, instead of duplicating an ever-growing full
        # session on every poll. Keep legacy payload shape for existing readers.
        for row in rows:
            field = "bars" if kind == "minutes" else "points" if kind == "sector_minutes" else None
            if field and row.get(field):
                for value in row[field]:
                    yield {**row, field: [value]}
            else:
                yield row

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        try:
            with con:
                yield con
        finally:
            con.close()

    def append(self, kind, date, rows, *, cursor=None):
        datetime.strptime(str(date), "%Y%m%d")
        values = []
        for row in self._expand(kind, rows):
            body = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            identity = (kind, date, row.get("code"), row.get("received_at"),
                        row.get("time")) if kind == "snapshot" else (kind, date, body)
            if kind in {"minutes", "sector_minutes"}:
                field = "bars" if kind == "minutes" else "points"
                identity = (kind, date, row.get("code"), row.get(field))
            ident = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
            received = str(row.get("received_at") or shanghai_now(self.clock()).isoformat())
            event_time = str(row.get("time") or "")
            if kind in {"minutes", "sector_minutes"}:
                field = "bars" if kind == "minutes" else "points"
                item = (row.get(field) or [{}])[0]
                event_time = str(item.get("time") or item.get("datetime") or "")
                if len(event_time) == 5:
                    event_time += ":00"
            event_at = self._timestamp(event_time if len(event_time) > 8 else
                                       f"{date[:4]}-{date[4:6]}-{date[6:]}T{event_time}")
            if kind == "board_snapshot" and event_at is None:
                event_at = self._timestamp(received)
            values.append((ident, kind, date, str(row.get("code") or ""), received, body,
                           None, self._timestamp(received), event_at))
        with file_mutex(self.lifecycle_lock), self.connect() as con:
            active = self.root / "active_day.json"
            if active.is_file():
                active_date = json.loads(active.read_text())["market_date"]
                if date < active_date:
                    raise ValueError("旧交易日实时行情已清理，拒绝迟到的旧日写入")
                if date > active_date:
                    raise ValueError("未来交易日尚未启用，拒绝提前写入实时行情")
            con.executemany("""INSERT OR IGNORE INTO market_events
                (event_id,kind,market_date,code,received_at,payload,persisted_ts,received_ts,event_ts)
                VALUES (?,?,?,?,?,?,?,?,?)""", values)
            con.execute("UPDATE market_events SET write_seq=seq WHERE write_seq IS NULL")
            if cursor is not None:
                con.execute("INSERT OR REPLACE INTO evidence_cursors VALUES (?, ?)",
                            (cursor[0], json.dumps(cursor[1], allow_nan=False)))
            # Timestamp only after the raw transaction has durably committed.
            # Keep the lifecycle lock through the metadata commit so no reader
            # or archiver can mistake a half-published batch for available input.
            con.commit()
            committed_at = shanghai_now(self.clock()).timestamp()
            con.executemany("UPDATE market_events SET persisted_ts=? WHERE event_id=? AND persisted_ts IS NULL",
                            [(committed_at, row[0]) for row in values])
        return len(values)

    def evidence_cursor(self, key):
        with self.connect() as con:
            row = con.execute("SELECT payload FROM evidence_cursors WHERE cursor_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def archive_once(self, limit=1000):
        with file_mutex(self.lifecycle_lock):
            return self._archive_once(limit)

    def _archive_once(self, limit):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM market_events WHERE archived=0 ORDER BY seq LIMIT ?", (limit,)).fetchall()
        dates = sorted({row["market_date"] for row in rows})
        for date in dates:
            datetime.strptime(date, "%Y%m%d")
            group = [row for row in rows if row["market_date"] == date]
            archive = sqlite3.connect(self.root / f"market_{date}.sqlite3", timeout=10)
            try:
                archive.execute("PRAGMA journal_mode=WAL")
                archive.execute("PRAGMA synchronous=FULL")
                with archive:
                    archive.execute("""CREATE TABLE IF NOT EXISTS market_events (
                        event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL,
                        received_at TEXT NOT NULL, payload TEXT NOT NULL)""")
                    archive.execute("CREATE INDEX IF NOT EXISTS market_code_time ON market_events(code,received_at)")
                    self._upgrade(archive)
                    archive.executemany("""INSERT OR IGNORE INTO market_events
                        (event_id,kind,code,received_at,payload,write_seq,persisted_ts,received_ts,event_ts)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        [(r["event_id"], r["kind"], r["code"], r["received_at"], r["payload"],
                          r["write_seq"], r["persisted_ts"], r["received_ts"], r["event_ts"]) for r in group])
                # A crash between these commits only causes an idempotent replay.
                with self.connect() as con:
                    con.executemany("DELETE FROM market_events WHERE event_id=?", [(r["event_id"],) for r in group])
            finally:
                archive.close()
        return len(rows)

    def health(self):
        with self.connect() as con:
            row = con.execute("SELECT count(*),min(received_at),max(seq) FROM market_events WHERE archived=0").fetchone()
        return {"pending": row[0], "oldest_pending_at": row[1], "last_pending_seq": row[2]}
