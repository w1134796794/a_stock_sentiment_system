"""Durable ingestion spool and idempotent asynchronous daily market archive."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.realtime.trading_session import shanghai_now


class MarketJournal:
    def __init__(self, root=None):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or (Path(WEB_DATA_DIR) / "market_archive"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ingestion.sqlite3"
        with self.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS market_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, market_date TEXT NOT NULL, code TEXT NOT NULL,
                received_at TEXT NOT NULL, payload TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)""")
            con.execute("CREATE INDEX IF NOT EXISTS pending_market_events ON market_events(archived,seq)")

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

    def append(self, kind, date, rows):
        datetime.strptime(str(date), "%Y%m%d")
        values = []
        for row in rows:
            body = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            identity = (kind, date, row.get("code"), row.get("received_at"),
                        row.get("time")) if kind == "snapshot" else (kind, date, body)
            ident = hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()
            values.append((ident, kind, date, str(row.get("code") or ""),
                           str(row.get("received_at") or shanghai_now().isoformat()), body))
        with self.connect() as con:
            con.executemany("""INSERT OR IGNORE INTO market_events
                (event_id,kind,market_date,code,received_at,payload) VALUES (?,?,?,?,?,?)""", values)
        return len(values)

    def archive_once(self, limit=1000):
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
                    archive.executemany("INSERT OR IGNORE INTO market_events VALUES (?,?,?,?,?)",
                        [(r["event_id"], r["kind"], r["code"], r["received_at"], r["payload"]) for r in group])
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
