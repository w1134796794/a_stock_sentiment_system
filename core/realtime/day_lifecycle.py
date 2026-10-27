"""Collector-owned rollover of raw realtime evidence; no research/debug deletion."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from core.infrastructure.atomic_json import atomic_write_json
from core.infrastructure.file_mutex import file_mutex
from core.realtime.trading_session import shanghai_now


class RealtimeDayLifecycle:
    def __init__(self, journal, *, cache_dir=None, backend=None, clock=shanghai_now):
        from config.settings import CACHE_DIR
        self.journal = journal
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.backend, self.clock = backend, clock

    def start_day(self, date):
        datetime.strptime(date, "%Y%m%d")
        if shanghai_now(self.clock()).strftime("%Y%m%d") != date:
            raise ValueError("只能为当前交易日执行盘中行情清理")
        marker = self.journal.root / "active_day.json"
        with file_mutex(self.journal.lifecycle_lock):
            previous = json.loads(marker.read_text()) if marker.is_file() else {}
            if previous.get("market_date", "") > date:
                raise ValueError("不能回退实时行情交易日")
            # Restarts must never reset this day's data or purge it again.
            if previous.get("market_date") == date:
                return {**previous, "same_day_restart": True}
            removed_keys = self._redis_cleanup(date)
            with self.journal.connect() as con:
                removed_pending = con.execute("DELETE FROM market_events WHERE market_date<?", (date,)).rowcount
                for key, in con.execute("SELECT cursor_key FROM evidence_cursors").fetchall():
                    days = re.findall(r"(?<!\d)(\d{8})(?!\d)", key)
                    if days and all(day < date for day in days):
                        con.execute("DELETE FROM evidence_cursors WHERE cursor_key=?", (key,))
            removed_files = []
            folders = [(self.journal.root, r"market_(\d{8})\.sqlite3"),
                       (self.cache_dir / "stock" / "sector_quote_minutes", r"(\d{8})\.sqlite3")]
            for folder, pattern in folders:
                for path in sorted(folder.glob("*.sqlite3")):
                    match = re.fullmatch(pattern, path.name)
                    if not match or match[1] >= date:
                        continue
                    if path.is_symlink():
                        raise ValueError("拒绝清理符号链接数据库")
                    # Refuse removal when a SQLite reader prevents a complete checkpoint.
                    with file_mutex(str(path) + ".retire.lock", timeout=2):
                        con = sqlite3.connect(path, timeout=1)
                        try:
                            busy, *_ = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                            if busy:
                                raise RuntimeError(f"旧日数据库仍有读写任务：{path}")
                        finally:
                            con.close()
                        path.unlink()
                        for suffix in ("-wal", "-shm"):
                            Path(str(path) + suffix).unlink(missing_ok=True)
                        removed_files.append(str(path))
            result = {"market_date": date, "prepared_at": shanghai_now(self.clock()).isoformat(),
                      "removed_files": removed_files, "removed_pending": removed_pending,
                      "removed_redis_keys": removed_keys, "same_day_restart": False}
            atomic_write_json(marker, result)
            return result

    def _redis_cleanup(self, date):
        if self.backend is None:
            return 0
        prefixes = ("realtime:snapshots:", "realtime:minutes:", "realtime:sector-quote-minutes:",
                    "realtime:tdx-board:", "realtime:evidence:v1:", "realtime:auction:", "realtime:session-open:")
        removed = 0
        for prefix in prefixes:
            for key in self.backend.scan_keys(prefix + "*"):
                days = re.findall(r"(?<!\d)(\d{8})(?!\d)", key)
                if days and all(day < date for day in days):
                    self.backend.delete(key)
                    removed += 1
        for key in [*self.backend.scan_keys("realtime:quotes:latest:*"), "realtime:quotes:meta"]:
            value = self.backend.get_json(key) or {}
            day = str(value.get("date") or value.get("trade_date") or "").replace("-", "")
            if re.fullmatch(r"\d{8}", day) and day < date:
                self.backend.delete(key)
                removed += 1
        return removed
