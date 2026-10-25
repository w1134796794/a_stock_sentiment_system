"""Logical .csv minute caches backed by plain CSV or verified lossless gzip.

Logical filenames and decompressed bytes remain stable for frozen provenance.
Reads never create directories/locks. Writers and maintenance share a directory
mutex; maintenance must not overlap external, non-cooperating file writers.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path

from core.infrastructure.file_mutex import file_mutex


def _logical(path):
    path = Path(path)
    if path.name.endswith(".csv.gz"):
        path = path.with_suffix("")
    if path.suffix != ".csv":
        raise ValueError("Minute cache requires a logical .csv filename")
    return path


def minute_cache_exists(path):
    path = _logical(path)
    return path.is_file() or path.with_suffix(".csv.gz").is_file()


def read_minute_bytes(path):
    path = _logical(path)
    for _ in range(2):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            try:
                with gzip.open(path.with_suffix(".csv.gz"), "rb") as stream:
                    return stream.read()
            except FileNotFoundError:
                continue
    raise FileNotFoundError(path)


def read_minute_csv(path, **kwargs):
    import pandas as pd

    # A BytesIO keeps pandas from inferring compression from the logical suffix.
    return pd.read_csv(BytesIO(read_minute_bytes(path)), **kwargs)


def iter_minute_files(directory):
    directory = Path(directory)
    # Yield canonical .csv paths once even when both representations exist.
    return iter(
        sorted(
            {
                p.with_suffix("") if p.suffix == ".gz" else p
                for pattern in ("*.csv", "*.csv.gz")
                for p in directory.glob(pattern)
                if p.is_file()
            }
        )
    )


def write_minute_csv(frame, path, *, compress=None):
    path = _logical(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_mutex(path.parent / ".minute-cache.lock"):
        if compress is None:
            from config.settings import MINUTE_CACHE_COMPRESSION

            compress = MINUTE_CACHE_COMPRESSION or path.with_suffix(".csv.gz").exists()
        destination = path.with_suffix(".csv.gz") if compress else path
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as raw:
                if compress:
                    # Encode the exact CSV text that would otherwise be written.
                    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed:
                        from io import TextIOWrapper

                        with TextIOWrapper(packed, encoding="utf-8", newline="") as stream:
                            frame.to_csv(stream, index=False)
                else:
                    raw.write(frame.to_csv(index=False).encode("utf-8"))
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, destination)
            alternate = path if compress else path.with_suffix(".csv.gz")
            alternate.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def _digest_file(path, *, packed=False):
    digest = hashlib.sha256()
    size = 0
    opener = gzip.open if packed else open
    with opener(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _preserve_metadata(path, before):
    import stat

    os.chmod(path, stat.S_IMODE(before.st_mode))
    if hasattr(os, "chown") and (path.stat().st_uid != before.st_uid or path.stat().st_gid != before.st_gid):
        os.chown(path, before.st_uid, before.st_gid)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def compress_minute_file(path, *, apply=False):
    path = _logical(path)
    if path.is_symlink() or path.with_suffix(".csv.gz").is_symlink():
        raise ValueError("Refuse symlink minute caches")
    if not path.exists():
        return dict(
            path=path.name, status="already_compressed" if minute_cache_exists(path) else "missing", saved_bytes=0
        )
    if not apply:
        # Stream the compression into a counter, with no temporary file or lock.
        class Counter:
            size = 0

            def write(self, data):
                self.size += len(data)
                return len(data)

        before = path.stat()
        counter = Counter()
        with (
            path.open("rb") as source,
            gzip.GzipFile(filename="", mode="wb", fileobj=counter, mtime=0, compresslevel=6) as target,
        ):
            shutil.copyfileobj(source, target, 1024 * 1024)
        if path.stat().st_mtime_ns != before.st_mtime_ns or path.stat().st_size != before.st_size:
            raise RuntimeError("Source changed during preview")
        return dict(
            path=path.name,
            status="preview",
            original_bytes=before.st_size,
            compressed_bytes=counter.size,
            saved_bytes=max(0, before.st_size - counter.size),
        )
    with file_mutex(path.parent / ".minute-cache.lock"):
        if not path.exists():
            return dict(path=path.name, status="already_compressed", saved_bytes=0)
        before = path.stat()
        destination = path.with_suffix(".csv.gz")
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".gz.tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with path.open("rb") as source, os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                raw.flush()
                os.fsync(raw.fileno())
            original_hash, original_size = _digest_file(path)
            packed_hash, packed_size = _digest_file(temporary, packed=True)
            after = path.stat()
            if (
                original_hash != packed_hash
                or original_size != packed_size
                or (before.st_size, before.st_mtime_ns, before.st_ino)
                != (after.st_size, after.st_mtime_ns, after.st_ino)
            ):
                raise RuntimeError("Source changed or gzip verification failed; original retained")
            if temporary.stat().st_size >= original_size:
                return dict(path=path.name, status="not_smaller", saved_bytes=0)
            if destination.exists() and _digest_file(destination, packed=True) != (original_hash, original_size):
                raise RuntimeError("Existing gzip differs; preserve both files for diagnosis")
            compressed_size = temporary.stat().st_size
            existing_size = destination.stat().st_size if destination.exists() else 0
            _preserve_metadata(temporary, before)
            os.replace(temporary, destination)
            _sync_directory(path.parent)
            path.unlink()  # Only after CRC/SHA256 verification and durable rename.
            _sync_directory(path.parent)
            return dict(
                path=path.name,
                status="compressed",
                original_bytes=original_size,
                compressed_bytes=compressed_size,
                saved_bytes=original_size + existing_size - compressed_size,
                sha256_uncompressed=original_hash,
            )
        finally:
            temporary.unlink(missing_ok=True)


def restore_minute_file(path):
    """Restore original byte-for-byte CSV using one file's temporary disk space."""
    path = _logical(path)
    packed = path.with_suffix(".csv.gz")
    if path.is_symlink() or packed.is_symlink():
        raise ValueError("Refuse symlink minute caches")
    with file_mutex(path.parent / ".minute-cache.lock"):
        if not packed.exists():
            return dict(path=path.name, status="already_plain")
        expected = _digest_file(packed, packed=True)
        if path.exists():
            if _digest_file(path) != expected:
                raise ValueError("Plain and gzip differ; original files retained")
            packed.unlink()
            return dict(path=path.name, status="removed_identical_gzip")
        before = packed.stat()
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".restore.tmp", dir=path.parent)
        temporary = Path(name)
        try:
            with gzip.open(packed, "rb") as source, os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if _digest_file(temporary) != expected:
                raise ValueError("Restore hash mismatch; gzip retained")
            _preserve_metadata(temporary, before)
            os.replace(temporary, path)
            _sync_directory(path.parent)
            packed.unlink()
            _sync_directory(path.parent)
            return dict(path=path.name, status="restored", bytes=expected[1])
        finally:
            temporary.unlink(missing_ok=True)
