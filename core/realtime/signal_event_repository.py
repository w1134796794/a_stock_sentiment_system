"""Persistent intraday confirmation history.

Realtime payloads describe the latest state only. This repository keeps the
confirmation events that must remain auditable after a signal later weakens.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import APP_DB_PATH
from core.utils.stock_code_utils import StockCodeUtils


class RealtimeSignalEventRepository:
    """Store and merge confirmed intraday signal events."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or APP_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS realtime_signal_events (
                    market_date TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    entry_mode TEXT NOT NULL,
                    first_confirmed_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (market_date, profile, stock_code, entry_mode)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_realtime_signal_events_date
                ON realtime_signal_events (market_date, profile, first_confirmed_at)
                """
            )

    @staticmethod
    def _code(row: Dict[str, Any]) -> str:
        return StockCodeUtils.standardize_code(
            str(row.get("code") or row.get("stock_code") or ""),
            add_suffix=False,
        )

    @staticmethod
    def _profile(payload: Dict[str, Any]) -> str:
        return str(
            payload.get("profile")
            or payload.get("observation_source")
            or (payload.get("strategy") or {}).get("id")
            or "decision_pool"
        )

    def record_payload(self, payload: Dict[str, Any]) -> int:
        """Upsert every currently confirmed row and return the event count."""
        market_date = str(payload.get("market_date") or "").replace("-", "")[:8]
        profile = self._profile(payload)
        if not market_date:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        records = []
        for source in payload.get("rows") or []:
            row = dict(source or {})
            status = str(row.get("confirm_status") or row.get("status") or "")
            code = self._code(row)
            if status != "confirmed" or not code:
                continue
            confirmed_at = str(row.get("confirm_time") or row.get("entry_time") or now)
            if len(confirmed_at) <= 8:
                confirmed_at = f"{market_date} {confirmed_at}"
            row["code"] = code
            row["archived_profile"] = profile
            records.append((
                market_date,
                profile,
                code,
                str(row.get("entry_mode") or "intraday"),
                confirmed_at,
                confirmed_at,
                json.dumps(row, ensure_ascii=False, default=str),
            ))
        if not records:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO realtime_signal_events (
                    market_date, profile, stock_code, entry_mode,
                    first_confirmed_at, last_confirmed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_date, profile, stock_code, entry_mode)
                DO UPDATE SET
                    last_confirmed_at=excluded.last_confirmed_at,
                    payload_json=excluded.payload_json
                """,
                records,
            )
        return len(records)

    def list_events(self, market_date: str, profile: str) -> List[Dict[str, Any]]:
        normalized_date = str(market_date or "").replace("-", "")[:8]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM realtime_signal_events
                WHERE market_date=? AND profile=?
                ORDER BY first_confirmed_at, stock_code
                """,
                (normalized_date, str(profile or "decision_pool")),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for record in rows:
            try:
                payload = dict(json.loads(record["payload_json"]) or {})
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            payload.update({
                "code": record["stock_code"],
                "entry_mode": record["entry_mode"],
                "first_confirmed_at": record["first_confirmed_at"],
                "last_confirmed_at": record["last_confirmed_at"],
                "was_confirmed_today": True,
            })
            events.append(payload)
        return events

    def merge_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Merge today's confirmed events without claiming they remain active."""
        market_date = str(payload.get("market_date") or "").replace("-", "")[:8]
        profile = self._profile(payload)
        events = self.list_events(market_date, profile) if market_date else []
        rows = [dict(row or {}) for row in payload.get("rows") or []]
        current = {
            (self._code(row), str(row.get("entry_mode") or "intraday")): row
            for row in rows
        }
        for event in events:
            key = (self._code(event), str(event.get("entry_mode") or "intraday"))
            row = current.get(key)
            is_current = row is not None
            if row is None:
                row = dict(event)
                rows.append(row)
                current[key] = row
            current_status = (
                str(row.get("confirm_status") or row.get("status") or "observe")
                if is_current
                else "not_in_current_pool"
            )
            row.update({
                "was_confirmed_today": True,
                "first_confirmed_at": event.get("first_confirmed_at") or "",
                "last_confirmed_at": event.get("last_confirmed_at") or "",
            })
            if not is_current or current_status != "confirmed":
                current_text = (
                    str(row.get("status_text") or row.get("signal_status_text") or "当前待观察")
                    if is_current
                    else "已离开当前观察池"
                )
                row.update({
                    "current_status": current_status,
                    "current_status_text": current_text,
                    "status": "confirmed_history",
                    "confirm_status": "confirmed_history",
                    "status_text": "今日曾确认",
                    "signal_status_text": "今日曾确认",
                    "action": "该信号今日曾确认，当前需重新核对，禁止按历史确认追单",
                })
        payload["rows"] = rows
        statuses = [
            str(row.get("confirm_status") or row.get("status") or "") for row in rows
        ]
        payload["counts"] = {
            "confirmed": sum(item in {"confirmed", "confirmed_history"} for item in statuses),
            "confirmed_history": statuses.count("confirmed_history"),
            "unfilled": statuses.count("unfilled"),
            "observe": statuses.count("observe"),
            "cancelled": statuses.count("cancelled"),
        }
        payload["confirmation_history_count"] = len(events)
        return payload


__all__ = ["RealtimeSignalEventRepository"]
