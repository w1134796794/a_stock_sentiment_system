"""Lossless minute-cache gzip + JSON whitespace removal; preview by default.

Run with the matching reader support deployed and historical writers paused.
Never removes historical rows, signals, trades, databases or replay sessions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data.minute_cache_files import (
    _preserve_metadata,
    _sync_directory,
    compress_minute_file,
    iter_minute_files,
    restore_minute_file,
)
from core.infrastructure.file_mutex import file_mutex
from core.realtime.trading_session import shanghai_now


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys; skip rather than discard content")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Nonstandard JSON number: {value}")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def compact_json_file(path, *, apply=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refuse JSON symlinks")

    def prepare():
        before = path.stat()
        raw = path.read_bytes()
        value = strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("Expected a strategy JSON object")
        # Preserve numeric token precision: remove whitespace lexically rather
        # than re-serializing float values or discarding any object keys.
        output = bytearray()
        quoted = False
        escaped = False
        for byte in raw.removeprefix(b"\xef\xbb\xbf"):
            if quoted:
                output.append(byte)
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
                output.append(byte)
            elif byte not in b" \t\r\n":
                output.append(byte)
        encoded = bytes(output)
        if strict_json(encoded) != value:
            raise ValueError("JSON semantic comparison failed")
        return before, raw, encoded

    if not apply:
        before, raw, encoded = prepare()
        return dict(
            path=str(path),
            status="preview",
            original_bytes=len(raw),
            compressed_bytes=len(encoded),
            saved_bytes=max(0, len(raw) - len(encoded)),
        )
    with file_mutex(str(path) + ".lock"):
        before, raw, encoded = prepare()
        if len(encoded) >= len(raw):
            return dict(path=str(path), status="already_compact", saved_bytes=0)
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ) or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(raw).digest():
                raise RuntimeError("JSON changed during maintenance; original retained")
            _preserve_metadata(temporary, before)
            os.replace(temporary, path)
            _sync_directory(path.parent)
            return dict(
                path=str(path),
                status="compacted",
                original_bytes=len(raw),
                compressed_bytes=len(encoded),
                saved_bytes=len(raw) - len(encoded),
            )
        finally:
            temporary.unlink(missing_ok=True)


def main():
    from config.settings import CACHE_DIR, WEB_DATA_DIR

    parser = argparse.ArgumentParser(description="保留所有数据：压缩历史分钟CSV、去除组合JSON排版空白。默认预演。")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--web-data-dir", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--target", choices=["all", "minutes", "combinations"], default="minutes")
    parser.add_argument(
        "--before", default=shanghai_now().strftime("%Y%m%d"), help="仅处理严格早于此业务日期的文件，默认今天"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-minutes", action="store_true", help="配合--apply恢复CSV，需要足够磁盘空间")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个文件，0为不限")
    parser.add_argument("--min-free-mb", type=int, default=512)
    parser.add_argument("--max-json-mb", type=int, default=16, help="跳过大于此大小的JSON，限制维护内存占用")
    parser.add_argument("--report", type=Path, help="可选逐文件JSONL结果；不会覆盖已有报告")
    args = parser.parse_args()
    cutoff = datetime.strptime(args.before, "%Y%m%d")
    if cutoff.strftime("%Y%m%d") != args.before or args.before > shanghai_now().strftime("%Y%m%d"):
        parser.error("--before must be YYYYMMDD and no later than today in Shanghai")
    if args.restore_minutes and (not args.apply or args.target == "combinations"):
        parser.error("--restore-minutes requires --apply and a minute target")
    if args.limit < 0 or args.min_free_mb < 0 or args.max_json_mb <= 0:
        parser.error("Invalid limit/free-space threshold")
    summary = dict(
        mode="restore" if args.restore_minutes else "apply" if args.apply else "preview",
        before_exclusive=args.before,
        files=0,
        saved_bytes=0,
        errors=0,
        skipped=0,
        minute_files=0,
        combination_files=0,
        samples=[],
        historical_rows_deleted=0,
        databases_modified=False,
    )
    stream = None
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        stream = args.report.open("x", encoding="utf-8")

    def report(item):
        if len(summary["samples"]) < 20:
            summary["samples"].append(item)
        if stream:
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

    def candidates():
        if args.target in {"all", "minutes"}:
            for path in iter_minute_files(args.cache_dir / "stock" / "tick"):
                if args.restore_minutes and not path.with_suffix(".csv.gz").exists():
                    continue
                yield "minute", path
        if args.target in {"all", "combinations"} and not args.restore_minutes:
            folder = args.web_data_dir / "screening" / "combinations"
            for path in sorted(folder.rglob("*.json")):
                if path.is_file():
                    yield "combination", path

    try:
        for kind, path in candidates():
            match = re.search(r"_(\d{8})$", path.stem)
            if not match or match[1] >= args.before:
                summary["skipped"] += 1
                continue
            if args.limit and summary["files"] >= args.limit:
                break
            try:
                datetime.strptime(match[1], "%Y%m%d")
                if kind == "combination" and path.stat().st_size > args.max_json_mb * 1024**2:
                    summary["skipped"] += 1
                    report(dict(kind=kind, path=str(path), status="skipped_large_json"))
                    continue
                if args.apply:
                    source = path if path.exists() else path.with_suffix(".csv.gz")
                    free = shutil.disk_usage(source.parent).free
                    required = source.stat().st_size * 2 if args.restore_minutes else source.stat().st_size + 65536
                    # Restore size is only an estimate; ENOSPC still preserves gzip.
                    if free < args.min_free_mb * 1024**2 + required:
                        raise OSError("Insufficient free space for an atomic single-file operation")
                if kind == "minute":
                    item = (
                        restore_minute_file(path)
                        if args.restore_minutes
                        else compress_minute_file(path, apply=args.apply)
                    )
                    summary["minute_files"] += 1
                else:
                    item = compact_json_file(path, apply=args.apply)
                    summary["combination_files"] += 1
                summary["files"] += 1
                summary["saved_bytes"] += item.get("saved_bytes", 0)
                report(dict(kind=kind, **item))
            except (OSError, ValueError, RuntimeError, EOFError) as exc:
                summary["errors"] += 1
                report(dict(kind=kind, path=str(path), status="failed", reason=str(exc)))
    finally:
        if stream:
            stream.close()
    summary["saved_mib"] = round(summary["saved_bytes"] / 1024**2, 2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
