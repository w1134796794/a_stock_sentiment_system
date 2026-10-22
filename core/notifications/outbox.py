"""Durable, expiring notification jobs independent of paper execution."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Thread
from time import time

from core.operations.ledger import stable_id
from core.realtime.trading_session import shanghai_now


class RealtimeNotificationOutbox:
    def __init__(self, ledger, *, notifier_factory=None, clock=time, max_age_seconds=30):
        from core.notifications.notifier import NotificationService
        self.ledger = ledger
        self.notifier_factory = notifier_factory or NotificationService
        self.clock = clock
        self.max_age = max_age_seconds
        self._stop = Event()
        self._wake = Event()
        self._thread = None
        with ledger._connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS realtime_notification_jobs (
                job_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                next_attempt REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0)""")

    def enqueue(self, payload):
        now = self.clock()
        count = 0
        # Keep the notifier's priority ordering even when jobs are independent.
        from core.notifications.notifier import NotificationService
        rows = sorted(payload.get("rows") or [], key=NotificationService._notification_priority, reverse=True)
        for row in rows:
            if (row.get("confirm_status") or row.get("status")) != "confirmed":
                continue
            if row.get("health_gate_reason") or row.get("entry_veto_reason"):
                continue
            key = stable_id("notify", payload.get("market_date"), row.get("code"), row.get("entry_mode"))
            body = {k: payload.get(k) for k in ("market_date", "candidate_date", "profile", "strategy", "observation_source")}
            body["rows"] = [row]
            signal_id = str(row.get("signal_id") or "")
            with self.ledger._connect() as con:
                inserted = con.execute("INSERT OR IGNORE INTO realtime_notification_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, signal_id, json.dumps(body, ensure_ascii=False, default=str), "queued",
                     now, now + self.max_age, now, 0, 0)).rowcount
                # A later, newly confirmed setup may refresh an expired job, never a sent one.
                if not inserted:
                    inserted = con.execute("""UPDATE realtime_notification_jobs SET payload=?, signal_id=?,
                        state='queued', created=?, expires=?, next_attempt=?, attempts=0
                        WHERE job_id=? AND state='expired' AND created<?""",
                        (json.dumps(body, ensure_ascii=False, default=str), signal_id, now,
                         now + self.max_age, now, key, now - self.max_age)).rowcount
            if inserted:
                count += 1
                self.ledger.event(stable_id(key, "queued", now), "notification_queued",
                    {"code": row.get("code"), "side": "buy", "queued_at": shanghai_now().isoformat(),
                     "expires_epoch": now + self.max_age}, signal_id=signal_id)
        self._wake.set()
        return count

    def _invalid(self, payload, now):
        clock = shanghai_now(datetime.fromtimestamp(now, timezone.utc))
        if payload.get("market_date") != clock.strftime("%Y%m%d"):
            return "通知已跨交易日"
        row = payload["rows"][0]
        try:
            confirmed = str(row.get("confirm_time") or "").replace("T", " ").split(" ")[-1][:8]
            confirmed = datetime.strptime(payload["market_date"] + confirmed, "%Y%m%d%H:%M:%S")
            # Minute labels may precede availability by one minute, but never
            # permit a retained historical confirmation to become a new alert.
            if not -1 <= (clock - shanghai_now(confirmed)).total_seconds() <= 120:
                return "确认时点已过期，拒绝补推历史买点"
            minute = ((row.get("condition_checks") or {}).get("confirmation") or {}).get("minute")
            if minute:
                structure_at = shanghai_now(datetime.strptime(payload["market_date"] + minute, "%Y%m%d%H:%M:%S"))
                if not -1 <= (clock - structure_at).total_seconds() <= 120:
                    return "快照确认引用的分钟结构已过期"
        except (ValueError, TypeError):
            return "通知缺少有效确认时点"
        stamp = row.get("received_at") or row.get("quote_time")
        try:
            received = shanghai_now(datetime.fromisoformat(stamp))
            from config.settings import REALTIME_QUOTE_STALE_SECONDS
            if not 0 <= (clock - received).total_seconds() <= REALTIME_QUOTE_STALE_SECONDS:
                return "通知发送前原确认行情已过期"
        except (TypeError, ValueError):
            return "通知缺少真实行情接收时间"
        from backtest.minute_entry import resolve_entry_deadline
        deadline = resolve_entry_deadline(row.get("strategy_execution") or {}, row.get("entry_mode") or "",
                    (row.get("entry_parameters") or {}).get("deadline") or "15:00:00")
        if clock.strftime("%H:%M:%S") > deadline:
            return "超过策略通知截止时间"
        return ""

    def process_once(self):
        now = self.clock()
        with self.ledger._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = con.execute("""SELECT * FROM realtime_notification_jobs WHERE
                (state='queued' AND next_attempt<=?) OR (state='processing' AND lease_until<?)
                ORDER BY created, rowid LIMIT 1""", (now, now)).fetchone()
            if job is None:
                return False
            con.execute("UPDATE realtime_notification_jobs SET state='processing', lease_until=? WHERE job_id=?",
                        (now + 300, job["job_id"]))
        payload = json.loads(job["payload"])
        row = payload["rows"][0]
        reason = "确认通知超过有效期" if now > job["expires"] else self._invalid(payload, now)
        dispatched = not reason
        submitted = shanghai_now().isoformat(timespec="milliseconds")
        result = {}
        if reason:
            state, status = "expired", "已过期未发送"
        else:
            try:
                self.notifier_factory().notify_realtime_payload(payload)
                result = row.get("notification_result") or {}
                status = row.get("notification_status") or "推送失败"
                state = "sent" if result.get("all_delivered") or result.get("deduplicated") else "queued"
                reason = row.get("notification_skip_reason") or ""
                if status == "同主题推送已达上限":
                    state = "skipped"
            except Exception:
                state, status, reason = "queued", "推送失败", "通知工作线程发送异常"
        completed = shanghai_now().isoformat(timespec="milliseconds")
        outcome = {"code": row.get("code"), "side": "buy", "status": status,
                   "sent": int(result.get("sent") or 0), "skip_reason": reason,
                   "dispatch_at": submitted if dispatched else "", "completed_at": completed,
                   "submitted_at": next((c["submitted_at"] for c in result.get("results") or [] if c.get("submitted_at")), ""),
                   "queue_wait_ms": round((now - job["created"]) * 1000, 3),
                   "all_delivered": bool(result.get("all_delivered"))}
        outcome["channels"] = [{k: item.get(k) for k in (
            "channel", "recipient", "ok", "deduplicated", "retry_pending", "submitted_at", "completed_at", "duration_ms")}
            for item in result.get("results") or []]
        self.ledger.event(stable_id(job["job_id"], "attempt", job["attempts"]),
                          "notification_attempt", outcome, signal_id=job["signal_id"])
        if outcome["sent"]:
            self.ledger.event(stable_id(job["job_id"], "sent", job["attempts"]), "notification",
                              outcome, signal_id=job["signal_id"])
        with self.ledger._connect() as con:
            con.execute("""UPDATE realtime_notification_jobs SET state=?, attempts=attempts+1,
                next_attempt=?, lease_until=0 WHERE job_id=?""", (state, now + 5, job["job_id"]))
        return True

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            from loguru import logger
            while not self._stop.is_set():
                try:
                    if self.process_once():
                        continue
                except Exception:
                    logger.exception("Notification outbox worker failed")
                self._wake.wait(1)
                self._wake.clear()
        self._thread = Thread(target=run, name="realtime-notification-outbox", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)
