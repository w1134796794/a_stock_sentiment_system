"""Unified, compressed diagnostic trail for the independent strategy worker."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from pathlib import Path
from threading import RLock
from time import monotonic

from loguru import logger

from core.realtime.trading_session import shanghai_now


class IntradayDebugJournal:
    def __init__(self, root=None, *, enabled=None, clock=shanghai_now):
        from config.settings import WEB_DATA_DIR

        self.root = Path(root or Path(WEB_DATA_DIR) / "logs" / "intraday_debug")
        self.enabled = (os.getenv("INTRADAY_DEBUG_ENABLED", "true").lower() in {"1", "true", "yes"}
                        if enabled is None else enabled)
        self.clock = clock
        self._states = {}
        self._date = ""
        self._lock = RLock()
        self._warning_at = float("-inf")

    def record(self, stage, date, code="", *, payload=None):
        """Fail open for diagnostics; preserve reason/boolean changes and minute samples."""
        if not self.enabled:
            return
        try:
            self._record(stage, date, code, payload or {})
        except Exception as exc:
            if monotonic() - self._warning_at >= 60:
                self._warning_at = monotonic()
                logger.warning("盘中debug留档失败，策略继续运行: {}", exc)

    def _record(self, stage, date, code, payload):
        from datetime import datetime

        datetime.strptime(date, "%Y%m%d")
        now = shanghai_now(self.clock())
        # Numerical market values vary every tick. Only gate state changes trigger
        # additional records; once per minute also captures the latest actual values.
        flags = {}

        def booleans(value, prefix=""):
            if not isinstance(value, dict):
                return
            for key, item in value.items():
                path = prefix + str(key)
                if isinstance(item, bool) or item is None:
                    flags[path] = item
                elif isinstance(item, dict):
                    booleans(item, path + ".")

        booleans(payload.get("condition_checks") or {})
        booleans(payload.get("sector_detail") or {}, "sector.")
        checks = payload.get("condition_checks") or {}
        signature = {key: payload.get(key) for key in (
            "reason", "signal_status", "confirm_status", "data_status", "health_gate_reason",
            "trigger_source", "error_type", "error")}
        signature.update(flags=flags, fallback_reason=checks.get("fallback_reason"))
        digest = hashlib.sha256(json.dumps(signature, sort_keys=True, default=str).encode()).hexdigest()
        minute = now.strftime("%Y%m%d%H%M")
        key = stage, code
        with self._lock:
            if date != self._date:
                self._states.clear()
                self._date = date
            prior = self._states.get(key)
            if prior == (minute, digest):
                return
            event = {"schema_version": 1, "stage": stage, "market_date": date, "code": code,
                     "recorded_at": now.isoformat(timespec="milliseconds"),
                     "sample_kind": "state_change" if prior and prior[1] != digest else "minute_sample",
                     "not_evaluated_semantics": "Absent checks were not reached; null is unknown, not a failed rule.",
                     "payload": payload}
            blob = zlib.compress(json.dumps(event, ensure_ascii=False, default=str).encode(), level=6)
            self.root.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.root / f"{date}.sqlite3", timeout=0.1) as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("""CREATE TABLE IF NOT EXISTS debug_events (
                    id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, code TEXT NOT NULL,
                    stage TEXT NOT NULL, reason TEXT NOT NULL, payload_zlib BLOB NOT NULL)""")
                con.execute("CREATE INDEX IF NOT EXISTS debug_code_time ON debug_events(code,recorded_at)")
                con.execute("INSERT INTO debug_events(recorded_at,code,stage,reason,payload_zlib) VALUES (?,?,?,?,?)",
                            (event["recorded_at"], code, stage, str(payload.get("reason") or ""), blob))
            self._states[key] = minute, digest
