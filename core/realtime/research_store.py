"""Append-only low-frequency research versions. Reads never initialize databases."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from core.realtime.market_evidence import fingerprint, timestamp
from core.realtime.trading_session import shanghai_now


class ResearchStore:
    def __init__(self, root):
        self.path = Path(root) / "market_archive" / "research" / "research.sqlite3"

    @contextmanager
    def connect(self, *, write=False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(
            str(self.path) if write else self.path.resolve().as_uri() + "?mode=ro", uri=not write, timeout=10
        )
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                con.execute("""CREATE TABLE IF NOT EXISTS versions (
                    version_id TEXT PRIMARY KEY,code TEXT NOT NULL,kind TEXT NOT NULL,
                    captured_at TEXT NOT NULL,request_hash TEXT NOT NULL,payload TEXT NOT NULL)""")
                con.execute("CREATE INDEX IF NOT EXISTS research_lookup ON versions(code,kind,captured_at)")
                con.execute("""CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,updated_at TEXT NOT NULL,payload TEXT NOT NULL)""")
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def request_hash(value):
        return fingerprint({key: value.get(key) for key in ("code", "kind", "start", "end", "period", "adjust")})

    def write(self, value):
        timestamp(value["captured_at"])
        if (
            value.get("source") != "eltdx"
            or value.get("formal_eligible") is not False
            or not value.get("research_only")
        ):
            raise ValueError("Research versions must be isolated observational data")
        identity = fingerprint(value)
        with self.connect(write=True) as con:
            con.execute(
                "INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    value["code"],
                    value["kind"],
                    value["captured_at"],
                    self.request_hash(value),
                    json.dumps(value, ensure_ascii=False, allow_nan=False),
                ),
            )
        return identity

    def read(self, code, kind, *, as_of=None, limit=1):
        with self.connect() as con:
            if con is None:
                return []
            sql = "SELECT version_id,payload FROM versions WHERE code=? AND kind=?"
            args = [code, kind]
            if as_of:
                # julian day compares absolute instants across timezone offsets.
                timestamp(as_of)
                sql += " AND julianday(captured_at)<=julianday(?)"
                args.append(as_of)
            sql += " ORDER BY julianday(captured_at) DESC,version_id DESC LIMIT ?"
            args.append(min(100, max(1, limit)))
            rows = con.execute(sql, args).fetchall()
        values = []
        for row in rows:
            value = json.loads(row["payload"])
            if fingerprint(value) != row["version_id"]:
                raise ValueError("Corrupt research version")
            values.append(dict(version_id=row["version_id"], **value))
        return values

    def completed(self, request):
        key = self.request_hash(request)
        with self.connect() as con:
            if con is None:
                return False
            rows = con.execute("SELECT payload FROM versions WHERE request_hash=? ORDER BY captured_at DESC", (key,))
            return any(json.loads(row["payload"])["quality_status"] == "ok" for row in rows)

    def save_job(self, job):
        value = {**job, "updated_at": shanghai_now().isoformat()}
        with self.connect(write=True) as con:
            con.execute(
                "INSERT OR REPLACE INTO research_jobs VALUES (?,?,?)",
                (value["job_id"], value["updated_at"], json.dumps(value, ensure_ascii=False, allow_nan=False)),
            )

    def jobs(self, limit=20):
        with self.connect() as con:
            if con is None:
                return []
            return [
                json.loads(row["payload"])
                for row in con.execute(
                    "SELECT payload FROM research_jobs ORDER BY updated_at DESC LIMIT ?", (min(100, max(1, limit)),)
                )
            ]

    def coverage(self, *, date=None):
        with self.connect() as con:
            if con is None:
                return []
            query = """SELECT code,kind,count(*) AS versions,max(captured_at) AS latest_captured_at
                     FROM versions"""
            args = ()
            if date:
                from datetime import datetime

                datetime.strptime(date, "%Y%m%d")
                query += " WHERE substr(captured_at,1,10)=?"
                args = (f"{date[:4]}-{date[4:6]}-{date[6:]}",)
            return [dict(row) for row in con.execute(query + " GROUP BY code,kind ORDER BY code,kind", args)]
