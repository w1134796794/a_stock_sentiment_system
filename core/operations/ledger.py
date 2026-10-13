"""Durable, idempotent audit trail for intraday decisions and paper trades."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
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
                CREATE TABLE IF NOT EXISTS signal_case_staging (
                    case_id TEXT PRIMARY KEY, market_date TEXT NOT NULL,
                    payload_json TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_case_staging_date
                    ON signal_case_staging(archived, market_date);
                CREATE TABLE IF NOT EXISTS signal_case_forecasts (
                    forecast_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL, model_version TEXT NOT NULL,
                    as_of TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_case_forecasts_signal ON signal_case_forecasts(signal_id, as_of);
                CREATE TABLE IF NOT EXISTS signal_case_matches (
                    forecast_id TEXT NOT NULL, case_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(forecast_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS signal_similarity_models (
                    model_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signal_analog_jobs (
                    forecast_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, case_id TEXT NOT NULL,
                    market_date TEXT NOT NULL, code TEXT NOT NULL, state TEXT NOT NULL,
                    query_json TEXT NOT NULL, created_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_analog_jobs_lookup
                    ON signal_analog_jobs(market_date, code, created_at);
                CREATE INDEX IF NOT EXISTS idx_analog_jobs_signal ON signal_analog_jobs(signal_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_analog_jobs_code ON signal_analog_jobs(code, market_date);
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
            if "lease_until" not in {row[1] for row in conn.execute("PRAGMA table_info(signal_analog_jobs)")}:
                conn.execute("ALTER TABLE signal_analog_jobs ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''")

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
            case = payload.get("signal_case")
            if case and case.get("confirm_at") and payload.get("status") in {"confirmed", "unfilled"}:
                conn.execute("INSERT OR IGNORE INTO signal_case_staging(case_id, market_date, payload_json) VALUES (?, ?, ?)",
                             (case["case_id"], case["market_date"], json.dumps(case, ensure_ascii=False, allow_nan=False)))
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

    def list_experiments(self, limit: int = 30, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind not in {None, "strategy", "analog", "replay"}:
            raise ValueError("Unknown experiment family")
        where = ""
        if kind == "strategy":
            where = "WHERE COALESCE(json_extract(report_json, '$.kind'), '') NOT LIKE 'analog_%' AND COALESCE(json_extract(report_json, '$.kind'), '') != 'replay_research' "
        elif kind == "analog":
            where = "WHERE json_extract(report_json, '$.kind') LIKE 'analog_%' "
        elif kind == "replay":
            where = "WHERE json_extract(report_json, '$.kind') = 'replay_research' "
        with self._connect() as conn:
            rows = conn.execute("SELECT experiment_id, frozen_at, configuration_hash, factor_hash, updated_at "
                                "FROM strategy_experiments " + where + "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def save_case_forecast(self, payload: Mapping[str, Any]) -> dict:
        """First prediction and its finite match list commit together, never refresh."""
        from core.signals.case_features import clean
        payload = clean(dict(payload))
        payload.setdefault("prediction_generated_at", datetime.now().isoformat(timespec="milliseconds"))
        with self._connect() as conn:
            inserted = conn.execute("""INSERT OR IGNORE INTO signal_case_forecasts VALUES (?, ?, ?, ?, ?, ?)""",
                (payload["forecast_id"], payload["case_id"], payload.get("signal_id") or "",
                 payload.get("frozen_model_id") or payload["model_version"], payload["as_of"],
                 json.dumps(payload, ensure_ascii=False, allow_nan=False))).rowcount
            if inserted:
                conn.executemany("INSERT INTO signal_case_matches VALUES (?, ?, ?)",
                    [(payload["forecast_id"], match["case_id"], json.dumps(match, ensure_ascii=False, allow_nan=False))
                     for match in payload.get("matches") or []])
            row = conn.execute("SELECT payload_json FROM signal_case_forecasts WHERE forecast_id=?",
                               (payload["forecast_id"],)).fetchone()
        return json.loads(row[0])

    def case_forecast(self, forecast_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM signal_case_forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_case_model(self, artifact: dict) -> dict:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO signal_similarity_models VALUES (?, ?)",
                         (artifact["model_id"], json.dumps(artifact, ensure_ascii=False, allow_nan=False)))
            row = conn.execute("SELECT payload_json FROM signal_similarity_models WHERE model_id=?", (artifact["model_id"],)).fetchone()
        return json.loads(row[0])

    def enqueue_analog(self, case: dict) -> dict:
        ident = stable_id("live_analog_v1", case["case_id"])
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO signal_analog_jobs "
                         "(forecast_id, signal_id, case_id, market_date, code, state, query_json, created_at) "
                         "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                         (ident, case.get("signal_id", ""), case["case_id"], case["market_date"], case["code"],
                          json.dumps(case, ensure_ascii=False, allow_nan=False), datetime.now().isoformat()))
        return self.analog_analysis(forecast_id=ident)

    def pending_analog_jobs(self, limit: int = 8) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM signal_analog_jobs WHERE state='queued' OR "
                                "(state='processing' AND lease_until<?) ORDER BY created_at LIMIT ?",
                                (datetime.now().isoformat(), min(max(limit, 1), 32))).fetchall()
        return [{**dict(row), "case": json.loads(row["query_json"])} for row in rows]

    def claim_analog_job(self, forecast_id: str) -> bool:
        now = datetime.now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claimed = conn.execute("UPDATE signal_analog_jobs SET state='processing', lease_until=? "
                "WHERE forecast_id=? AND (state='queued' OR (state='processing' AND lease_until<?))",
                ((now + timedelta(minutes=10)).isoformat(), forecast_id, now.isoformat())).rowcount
        return bool(claimed)

    def finish_analog_job(self, forecast_id: str, *, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute("UPDATE signal_analog_jobs SET state=?, error=? WHERE forecast_id=?",
                         ("failed" if error else "done", error[:300], forecast_id))

    def analog_analysis(self, *, forecast_id: str = "", signal_id: str = "",
                        market_date: str = "", code: str = "") -> dict:
        with self._connect() as conn:
            if forecast_id:
                job = conn.execute("SELECT * FROM signal_analog_jobs WHERE forecast_id=?", (forecast_id,)).fetchone()
            elif signal_id:
                job = conn.execute("SELECT * FROM signal_analog_jobs WHERE signal_id=? ORDER BY created_at LIMIT 1",
                                   (signal_id,)).fetchone()
            else:
                job = conn.execute("SELECT * FROM signal_analog_jobs WHERE code=? AND "
                                   "(market_date=? OR json_extract(query_json, '$.candidate_date')=?) "
                                   "ORDER BY market_date DESC, created_at DESC LIMIT 1", (code, market_date, market_date)).fetchone()
            ident = forecast_id or (job["forecast_id"] if job else "")
            if not ident and signal_id:
                saved = conn.execute("SELECT forecast_id FROM signal_case_forecasts WHERE signal_id=? "
                                     "ORDER BY as_of LIMIT 1", (signal_id,)).fetchone()
                ident = saved[0] if saved else ""
            row = conn.execute("SELECT payload_json FROM signal_case_forecasts WHERE forecast_id=?", (ident,)).fetchone()
        if row:
            result = json.loads(row[0])
            result.pop("matches", None)
            return result
        return {"forecast_id": ident, "signal_id": signal_id or (job["signal_id"] if job else ""),
                "status": "failed" if job and job["state"] == "failed" else "not_ready",
                "reason": job["error"] if job and job["error"] else "awaiting_background_analysis" if job else "no_confirmed_signal",
                "probability": None, "trading_gate_enabled": False}

    def analog_matches(self, forecast_id: str, *, offset: int = 0, limit: int = 10) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM signal_case_matches WHERE forecast_id=?", (forecast_id,)).fetchone()[0]
            rows = conn.execute("SELECT payload_json FROM signal_case_matches WHERE forecast_id=? "
                                "ORDER BY json_extract(payload_json, '$.distance'), case_id LIMIT ? OFFSET ?",
                                (forecast_id, min(max(limit, 1), 50), max(offset, 0))).fetchall()
        return {"forecast_id": forecast_id, "total": total, "offset": max(offset, 0),
                "rows": [json.loads(row[0]) for row in rows]}

    def analog_models(self, limit: int = 20, *, released_only: bool = False) -> list[dict]:
        with self._connect() as conn:
            where = "WHERE json_extract(payload_json, '$.release_allowed')=1 " if released_only else ""
            rows = conn.execute("SELECT payload_json FROM signal_similarity_models " + where + "ORDER BY rowid DESC LIMIT ?",
                                (min(max(limit, 1), 50),)).fetchall()
        return [json.loads(row[0]) for row in rows]
