"""Remove only named cleanup backups and daily intraday debug files; preview by default."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess


BACKUPS = (
    re.compile(r"portfolio-before-cleanup-\d{8}-\d{6}-\d{6}\.sql\.gz"),
    re.compile(r"factors\.duckdb\.before-retention-\d{8}-\d{6}-\d{6}\.gz"),
)
DEBUG = re.compile(r"\d{8}\.sqlite3(?:-wal|-shm|-journal)?")


def candidates(root):
    root = root.absolute()
    if not root.is_dir() or root.resolve() != root:
        raise ValueError("Expected existing data directory without symlink ancestors")
    files = [p for p in root.iterdir() if any(r.fullmatch(p.name) for r in BACKUPS)]
    debug = root / "logs" / "intraday_debug"
    if debug.is_dir() and debug.resolve() == debug:
        files.extend(p for p in debug.iterdir() if DEBUG.fullmatch(p.name))
    return sorted(p for p in files if p.is_file() and not p.is_symlink() and p.resolve() == p)


def cleanup(root, apply=False):
    files = candidates(root)
    total = sum(p.stat().st_size for p in files)
    for path in files:
        print(f"{path.stat().st_size / 1024**2:.1f} MiB  {path}")
    print(f"Selected {len(files)} files, {total / 1024**2:.1f} MiB")
    if not apply or not files:
        return
    if not shutil.which("lsof"):
        raise RuntimeError("lsof required; nothing deleted")
    # Check whole SQLite families so an open main DB also protects its sidecars.
    checks = set(files)
    for path in files:
        if DEBUG.fullmatch(path.name):
            base = path.parent / (path.name[:8] + ".sqlite3")
            checks.update(p for p in (base, *(Path(str(base) + s) for s in ("-wal", "-shm", "-journal"))) if p.exists())
    for path in sorted(checks):
        result = subprocess.run(["lsof", "-t", "--", str(path)], capture_output=True)
        if result.returncode not in (0, 1) or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError(f"Open file or lsof error; nothing deleted: {path}")
    for path in files:
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError("Path changed; stopping")
        path.unlink()
    print("Deleted selected backup/debug files only; databases and market archives preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Stop debug writers first; permanent deletion")
    args = parser.parse_args()
    cleanup(args.root, args.apply)
