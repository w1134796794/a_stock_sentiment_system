"""Pressure-triggered cleanup; never delete trading records or market inputs."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from core.infrastructure.file_mutex import file_mutex
from scripts.cleanup_debug_files import candidates

UNITS = ("a-stock.service", "a-stock-market.target", "a-stock-collector.service",
         "a-stock-archiver.service", "a-stock-strategy.service", "a-stock-execution.service")


def pressure(path, threshold=70, minimum_gib=4):
    total, used, free = shutil.disk_usage(path)
    # Match df: filesystem-reserved blocks are not available to the service user.
    percent = used / (used + free) * 100 if used + free else 100
    return percent >= threshold or free < minimum_gib * 1024**3


def night(now):
    return now.hour >= 23 or now.hour < 6


def valid_root(root):
    if not root.is_absolute() or not root.is_dir() or root.resolve() != root:
        raise ValueError("Expected existing absolute data directory without symlink ancestors")


def trim_database(root, cutoff):
    """Run as the normal service user, after all known writers have stopped."""
    valid_root(root)
    path = root / "portfolio.sqlite"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Missing or symlinked portfolio database")
    if not shutil.which("lsof"):
        raise RuntimeError("lsof required")
    opened = subprocess.run(["lsof", "-t", "--", str(path)], capture_output=True, timeout=20)
    if opened.returncode not in (0, 1) or opened.stdout or opened.stderr:
        raise RuntimeError("Portfolio database still open or lsof verification failed")
    if shutil.disk_usage(root).free <= 2 * path.stat().st_size + 512 * 1024**2:
        print("SKIP: insufficient reserve for safe portfolio compaction", flush=True)
        return
    c = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
    try:
        targets = {
            "runtime_observations": ("market_date < ?", (cutoff,)),
            "trading_events": ("kind='runtime_cycle' AND occurred_at < ?",
                               (datetime.strptime(cutoff, "%Y%m%d").strftime("%Y-%m-%d"),)),
        }
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if not set(targets).issubset(tables):
            raise RuntimeError("Unexpected portfolio schema")
        for t in tables:
            quoted = '"' + t.replace('"', '""') + '"'
            if any(r[2] in targets for r in c.execute(f"PRAGMA foreign_key_list({quoted})")):
                raise RuntimeError("Incoming foreign key requires manual review")
        for t in targets:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (t,)).fetchone():
                raise RuntimeError("Trigger requires manual review")
        removed = 0
        for table, (where, values) in targets.items():
            count = 0
            while True:
                if shutil.disk_usage(root).free < 512 * 1024**2:
                    raise RuntimeError("Disk reserve reached; prior batches already committed")
                n = c.execute(f'DELETE FROM "{table}" WHERE rowid IN '
                              f'(SELECT rowid FROM "{table}" WHERE {where} LIMIT 500)', values).rowcount
                count += n
                if c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError("Checkpoint busy; prior batches already committed")
                if not n:
                    break
            removed += count
            print(f"{table}: deleted={count}", flush=True)
        page = c.execute("PRAGMA page_size").fetchone()[0]
        free_pages = c.execute("PRAGMA freelist_count").fetchone()[0]
        if free_pages * page >= 64 * 1024**2:
            if shutil.disk_usage(root).free <= 2 * path.stat().st_size + 512 * 1024**2:
                print("SKIP VACUUM: reserve changed; deleted pages remain reusable", flush=True)
                return
            c.execute("VACUUM")
            if c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("Post-vacuum checkpoint busy")
        if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Portfolio integrity check failed")
        print(f"Portfolio: removed={removed}; size_MiB={path.stat().st_size / 1024**2:.1f}", flush=True)
    finally:
        c.close()


def trim_files(root, now, keep_days):
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y%m%d")
    for p in candidates(root):
        # Always protect today's debug database and all newer retention dates.
        if p.name.endswith(".gz"):
            eligible = p.stat().st_mtime < now.timestamp() - keep_days * 86400
        else:
            eligible = p.name[:8] < cutoff
        if not eligible:
            continue
        family = [p]
        if not p.name.endswith(".gz"):
            base = p.parent / (p.name[:8] + ".sqlite3")
            family = [q for q in [base] + [Path(str(base) + s) for s in
                      ("-wal", "-shm", "-journal")] if q.exists()]
        if any(q.is_symlink() or q.resolve() != q for q in family):
            continue
        results = [subprocess.run(["lsof", "-t", "--", str(q)], capture_output=True,
                                  timeout=20) for q in family]
        if any(r.returncode not in (0, 1) or r.stdout or r.stderr for r in results):
            print(f"SKIP open file: {p}")
            continue
        if p.exists() and not p.is_symlink() and p.resolve() == p:
            size = p.stat().st_size
            p.unlink()
            print(f"Deleted {size} bytes: {p}", flush=True)


def maintain(args):
    root = args.root
    valid_root(root)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not pressure(root, args.threshold, args.minimum_free_gib):
        print("Storage healthy; no cleanup or service interruption")
        return
    cutoff = (now - timedelta(days=args.keep_days)).strftime("%Y%m%d")
    print(f"Pressure detected; retain diagnostics from {cutoff}; night={night(now)}", flush=True)
    if not args.apply:
        print("Preview only. --apply enables file deletion and nighttime database maintenance")
        return
    if os.geteuid() != 0:
        raise RuntimeError("Orchestrator requires Linux root; worker runs as service user")
    if not shutil.which("lsof"):
        raise RuntimeError("lsof required")
    with file_mutex(root / "storage-cleanup.lock", timeout=0):
        trim_files(root, now, args.keep_days)
        for cmd in (["apt-get", "clean"], ["journalctl", "--rotate"],
                    ["journalctl", "--vacuum-size=300M"]):
            subprocess.run(cmd, check=True, timeout=120)
        if not pressure(root, args.threshold, args.minimum_free_gib):
            print("Pressure relieved by online cleanup")
            return
        if not night(now):
            print("Database maintenance deferred to 23:00-06:00 Asia/Shanghai; services untouched")
            return
        import pwd
        user = pwd.getpwnam(args.user)
        if user.pw_uid == 0:
            raise RuntimeError("Worker must not run as root")
        db = root / "portfolio.sqlite"
        if not db.is_file() or db.is_symlink() or db.stat().st_uid != user.pw_uid:
            raise RuntimeError("Portfolio ownership/path mismatch")
        if shutil.disk_usage(root).free <= 2 * db.stat().st_size + 512 * 1024**2:
            print("Insufficient compaction reserve; no services stopped; manual cleanup needed")
            return
        marker = root / "storage-cleanup-last-attempt.txt"
        if marker.is_symlink():
            raise RuntimeError("Unexpected marker symlink")
        date = now.strftime("%Y%m%d")
        if marker.exists() and marker.read_text().strip() == date:
            print("Already attempted database maintenance today")
            return
        marker.write_text(date)
        active = [u for u in UNITS if subprocess.run(
            ["systemctl", "is-active", "--quiet", u], timeout=15).returncode == 0]
        try:
            subprocess.run(["systemctl", "stop", *UNITS], check=True, timeout=120)
            subprocess.run(["runuser", "-u", args.user, "--", sys.executable, "-X", "utf8",
                str(Path(__file__).resolve()), "--root", str(root), "--db-worker", cutoff],
                check=True, timeout=900)
        finally:
            if active:
                subprocess.run(["systemctl", "start", *active], check=True, timeout=180)
        print(f"Pressure remaining: {pressure(root, args.threshold, args.minimum_free_gib)}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("/srv/a-stock/webdata"))
    p.add_argument("--user", default="deploy")
    p.add_argument("--threshold", type=int, choices=range(50, 99), default=70)
    p.add_argument("--minimum-free-gib", type=float, default=4)
    p.add_argument("--keep-days", type=int, choices=range(1, 366), default=7)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--db-worker", help=argparse.SUPPRESS)
    args = p.parse_args()
    if not math.isfinite(args.minimum_free_gib) or args.minimum_free_gib < 0:
        p.error("minimum free space must be non-negative")
    if args.db_worker:
        if os.name != "posix" or os.geteuid() == 0:
            p.error("Database worker must run as non-root on Linux")
        datetime.strptime(args.db_worker, "%Y%m%d")
        trim_database(args.root, args.db_worker)
    else:
        maintain(args)


if __name__ == "__main__":
    main()
