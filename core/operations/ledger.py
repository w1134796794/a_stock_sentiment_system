"""Durable, idempotent audit trail for intraday decisions and paper trades."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from config.settings import HOLDING_DB_PATH


def stable_id(*parts: Any) -> str:
    data = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


class TradingLedger:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or HOLDING_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signal_evidence (
                    signal_id TEXT PRIMARY KEY,
                    candidate_date TEXT NOT NULL,
                    market_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signal_evidence_date
                    ON signal_evidence(market_date, code);
                CREATE TABLE IF NOT EXISTS trading_events (
                    event_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    account_key TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trading_events_signal
                    ON trading_events(signal_id, occurred_at);
                CREATE TABLE IF NOT EXISTS strategy_experiments (
                    experiment_id TEXT PRIMARY KEY,
                    frozen_at TEXT NOT NULL,
                    configuration_hash TEXT NOT NULL,
                    factor_hash TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_equity_daily (
                    experiment_id TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (experiment_id, account_key, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_account_equity_date
                    ON account_equity_daily(account_key, trade_date);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def evidence(self, signal_id: str, evidence: Mapping[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="milliseconds")
        payload = dict(evidence)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO signal_evidence
                    (signal_id, candidate_date, market_date, code, status, evidence_json, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    status=excluded.status, evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
            """, (signal_id, str(payload.get("candidate_date") or ""),
                  str(payload.get("market_date") or ""), str(payload.get("code") or ""),
                  str(payload.get("status") or ""), json.dumps(payload, ensure_ascii=False, default=str), now, now))

    def event(self, event_id: str, kind: str, payload: Mapping[str, Any], *,
              signal_id: str = "", account_key: str = "") -> bool:
        with self._connect() as conn:
            result = conn.execute("""
                INSERT OR IGNORE INTO trading_events
                    (event_id, signal_id, kind, account_key, occurred_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event_id, signal_id, kind, account_key,
                  datetime.now().isoformat(timespec="milliseconds"),
                  json.dumps(dict(payload), ensure_ascii=False, default=str)))
        return bool(result.rowcount)

    def timeline(self, signal_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            evidence = conn.execute("SELECT evidence_json FROM signal_evidence WHERE signal_id=?", (signal_id,)).fetchone()
            events = conn.execute("SELECT * FROM trading_events WHERE signal_id=? ORDER BY occurred_at, event_id", (signal_id,)).fetchall()
        return {
            "signal_id": signal_id,
            "evidence": json.loads(evidence[0]) if evidence else {},
            "events": [{**dict(row), "payload": json.loads(row["payload_json"])} for row in events],
        }

    def save_equity_rows(self, experiment_id: str, account_key: str,
                         rows: list[dict[str, Any]], source_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM account_equity_daily WHERE experiment_id=? AND account_key=?",
                         (experiment_id, account_key))
            conn.executemany(
                "INSERT INTO account_equity_daily "
                "(experiment_id, account_key, trade_date, source_hash, payload_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(experiment_id, account_key, trade_date) "
                "DO UPDATE SET source_hash=excluded.source_hash, payload_json=excluded.payload_json",
                [(experiment_id, account_key, row["trade_date"], source_hash,
                  json.dumps(row, ensure_ascii=False)) for row in rows],
            )

    def save_experiment(self, experiment_id: str, frozen_at: str, configuration_hash: str,
                        factor_hash: str, report: Mapping[str, Any]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO strategy_experiments
                    (experiment_id, frozen_at, configuration_hash, factor_hash, report_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET report_json=excluded.report_json,
                    updated_at=excluded.updated_at
            """, (experiment_id, frozen_at, configuration_hash, factor_hash,
                  json.dumps(dict(report), ensure_ascii=False, default=str), now))

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM strategy_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
        return {**dict(row), "report": json.loads(row["report_json"])} if row else {}

    def list_experiments(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT experiment_id, frozen_at, configuration_hash, factor_hash, updated_at "
                                "FROM strategy_experiments ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
