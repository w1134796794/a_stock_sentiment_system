"""Single-writer DuckDB archive; realtime producers use SQLite staging."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.signals.case_features import clean, event_time


class SignalCaseRepository:
    def __init__(self, path: Path | str | None = None):
        from config.settings import FACTOR_DB_PATH
        self.path = Path(path or FACTOR_DB_PATH)

    @contextmanager
    def connect(self):
        import duckdb
        from core.infrastructure.duckdb_access import connect as duckdb_connect
        from config.settings import DUCKDB_MEMORY_LIMIT, DUCKDB_THREADS
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb_connect(str(self.path))
        try:
            con.execute("SET threads=?", [DUCKDB_THREADS])
            con.execute("SET memory_limit=?", [DUCKDB_MEMORY_LIMIT])
            con.execute("""CREATE TABLE IF NOT EXISTS signal_cases (
                case_id VARCHAR PRIMARY KEY, market_date VARCHAR, code VARCHAR,
                entry_mode VARCHAR, config_version VARCHAR, pit_verified BOOLEAN, payload_json VARCHAR);
                CREATE TABLE IF NOT EXISTS signal_case_outcomes (
                case_id VARCHAR, label VARCHAR, state VARCHAR, label_end_at VARCHAR,
                available_at VARCHAR, payload_json VARCHAR, PRIMARY KEY(case_id, label));
                CREATE TABLE IF NOT EXISTS signal_case_progress (
                case_id VARCHAR PRIMARY KEY, state VARCHAR, last_date VARCHAR, payload_json VARCHAR);
                CREATE TABLE IF NOT EXISTS signal_case_attempts (
                attempt_id VARCHAR PRIMARY KEY, market_date VARCHAR, state VARCHAR, payload_json VARCHAR);
                CREATE INDEX IF NOT EXISTS idx_cases_date ON signal_cases(market_date, entry_mode);
                CREATE INDEX IF NOT EXISTS idx_outcomes_available ON signal_case_outcomes(available_at, state);
            """)
            yield con
        finally:
            con.close()

    @staticmethod
    @contextmanager
    def transaction(con):
        con.execute("BEGIN TRANSACTION")
        try:
            yield
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    def record(self, case: dict, con=None) -> bool:
        if not case.get("confirm_at") or not case.get("code"):
            raise ValueError("A frozen case must have an objective confirmation")
        if con is None:
            with self.connect() as conn:
                return self.record(case, conn)
        return bool(con.execute("""INSERT INTO signal_cases VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING RETURNING case_id""", [case["case_id"], case["market_date"], case["code"],
            case["entry_mode"], case["config_version"], bool(case["pit_verified"]),
            json.dumps(clean(case), ensure_ascii=False, allow_nan=False)]).fetchone())

    def get(self, case_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT payload_json FROM signal_cases WHERE case_id=?", [case_id]).fetchone()
            return json.loads(row[0]) if row else {}

    def save_progress(self, case_id: str, state: str, last_date: str, payload: dict, con=None):
        if con is None:
            with self.connect() as conn:
                return self.save_progress(case_id, state, last_date, payload, conn)
        con.execute("INSERT OR REPLACE INTO signal_case_progress VALUES (?, ?, ?, ?)",
                    [case_id, state, last_date, json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)])

    def save_outcome(self, case_id: str, label: str, payload: dict, con=None):
        state = payload["state"]
        if state == "mature" and (not payload.get("label_end_at") or not payload.get("available_at")
                                  or payload["available_at"] < payload["label_end_at"]):
            raise ValueError("Mature labels require availability at/after label end")
        if con is None:
            with self.connect() as conn:
                return self.save_outcome(case_id, label, payload, conn)
        con.execute("""INSERT INTO signal_case_outcomes VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id, label) DO UPDATE SET state=excluded.state,
            label_end_at=excluded.label_end_at, available_at=excluded.available_at,
            payload_json=excluded.payload_json WHERE signal_case_outcomes.state <> 'mature'""",
            [case_id, label, state, payload.get("label_end_at", ""), payload.get("available_at", ""),
             json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)])

    def outcomes(self, case_id: str, *, as_of: str, research: bool = False) -> dict:
        as_of = event_time("", as_of)
        if not as_of:
            raise ValueError("A valid as-of timestamp is required")
        with self.connect() as con:
            rows = con.execute("""SELECT o.label, o.payload_json FROM signal_case_outcomes o
                JOIN signal_cases c USING(case_id) WHERE case_id=? AND state='mature'
                AND available_at < ? AND label_end_at < ? AND (c.pit_verified OR ?)
                ORDER BY label""", [case_id, as_of, as_of, research]).fetchall()
        return {label: json.loads(payload) for label, payload in rows}

    def import_staged(self, ledger_path: Path | str, *, through_date: str) -> int:
        import sqlite3
        if not Path(ledger_path).exists():
            return 0
        with sqlite3.connect(str(ledger_path)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='signal_case_staging'").fetchone()
            if not exists:
                return 0
            rows = db.execute("SELECT payload_json FROM signal_case_staging WHERE market_date<=? AND archived=0",
                              (through_date,)).fetchall()
            cases = [json.loads(row[0]) for row in rows]
            with self.connect() as con:
                con.execute("BEGIN TRANSACTION")
                try:
                    for case in cases:
                        self.record(case, con)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            db.executemany("UPDATE signal_case_staging SET archived=1 WHERE case_id=?",
                           [(case["case_id"],) for case in cases])
        return len(cases)

    def coverage(self) -> dict[str, Any]:
        with self.connect() as con:
            cases = con.execute("""SELECT origin, COUNT(*), SUM(CASE WHEN pit_verified THEN 1 ELSE 0 END)
                FROM (SELECT json_extract_string(payload_json, '$.origin') origin, pit_verified FROM signal_cases)
                GROUP BY origin""").fetchall()
            states = con.execute("SELECT state, COUNT(*) FROM signal_case_progress GROUP BY state").fetchall()
            labels = con.execute("SELECT label, state, COUNT(*) FROM signal_case_outcomes GROUP BY label, state").fetchall()
            attempts = con.execute("SELECT state, COUNT(*) FROM signal_case_attempts GROUP BY state").fetchall()
        return {"cases": [{"origin": o, "count": n, "pit_verified": p} for o, n, p in cases],
                "progress": dict(states), "attempts": dict(attempts),
                "labels": [{"label": l, "state": s, "count": n} for l, s, n in labels]}

    def export_records(self, *, start: str = "", end: str = "99999999", limit: int | None = 5000) -> list[dict]:
        """Offline export only; realtime readers consume the published JSON index."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        with self.connect() as con:
            rows = con.execute("""SELECT c.case_id, c.payload_json, p.payload_json, p.state
                FROM signal_cases c LEFT JOIN signal_case_progress p USING(case_id)
                WHERE c.market_date>=? AND c.market_date<=? ORDER BY c.market_date DESC, c.case_id
                """ + ("LIMIT ?" if limit is not None else ""),
                [start, end] + ([limit] if limit is not None else [])).fetchall()
            if not rows:
                return []
            ids = [row[0] for row in rows]
            labels = {}
            for case_id, label, text in con.execute("""SELECT case_id, label, payload_json
                    FROM signal_case_outcomes WHERE case_id IN (SELECT unnest(?))""", [ids]).fetchall():
                labels.setdefault(case_id, {})[label] = json.loads(text)
        records = []
        for case_id, text, progress_text, state in rows:
            progress = json.loads(progress_text or "{}")
            records.append({"case": json.loads(text), "labels": labels.get(case_id, {}),
                            "versions": {key: progress.get(key) for key in
                                         ("cost_version", "exit_rule_version", "outcome_version")},
                            "progress_state": state})
        return records
