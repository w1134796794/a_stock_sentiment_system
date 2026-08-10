"""Small, version-aware cache for generated JSON artifacts.

The web process reads immutable daily artifacts far more often than they are
rewritten.  This cache keeps a bounded working set and invalidates entries by
file mtime and size, so a low-memory server avoids repeated JSON parsing
without retaining months of snapshots in RAM.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Callable, Optional


def _positive_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except (TypeError, ValueError):
        return default


@dataclass
class _Entry:
    version: tuple[int, int]
    value: Any
    estimated_bytes: int


class VersionedArtifactCache:
    """Thread-safe LRU constrained by both item count and estimated memory."""

    def __init__(
        self,
        *,
        max_entries: int = 24,
        max_bytes: int = 96 * 1024 * 1024,
        max_file_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.max_entries = max(int(max_entries), 1)
        self.max_bytes = max(int(max_bytes), 1)
        self.max_file_bytes = max(int(max_file_bytes), 1)
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._estimated_bytes = 0
        self._lock = RLock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def version(path: Path | str) -> Optional[tuple[int, int]]:
        try:
            stat = Path(path).stat()
            return int(stat.st_mtime_ns), int(stat.st_size)
        except OSError:
            return None

    def get_or_load(self, path: Path | str, loader: Callable[[Path], Any]) -> Any:
        artifact = Path(path)
        version = self.version(artifact)
        if version is None:
            return None
        key = str(artifact.resolve())
        with self._lock:
            cached = self._entries.get(key)
            if cached and cached.version == version:
                self._entries.move_to_end(key)
                self._hits += 1
                return cached.value
            if cached:
                self._estimated_bytes -= cached.estimated_bytes
                self._entries.pop(key, None)
            self._misses += 1

        value = loader(artifact)
        file_bytes = version[1]
        if value is None or file_bytes > self.max_file_bytes:
            return value

        # Parsed JSON generally occupies several times its UTF-8 file size.
        estimated = max(file_bytes * 4, 1024)
        if estimated > self.max_bytes:
            return value
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous:
                self._estimated_bytes -= previous.estimated_bytes
            self._entries[key] = _Entry(version, value, estimated)
            self._estimated_bytes += estimated
            while (
                len(self._entries) > self.max_entries
                or self._estimated_bytes > self.max_bytes
            ):
                _, removed = self._entries.popitem(last=False)
                self._estimated_bytes -= removed.estimated_bytes
        return value

    def load_json(self, path: Path | str) -> Any:
        def parse(artifact: Path) -> Any:
            try:
                with artifact.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError):
                return None

        return self.get_or_load(path, parse)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._estimated_bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "estimated_bytes": self._estimated_bytes,
                "hits": self._hits,
                "misses": self._misses,
            }


class DirectoryIndexCache:
    """Short-lived cache for directory glob results shared across readers."""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self._items: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._lock = RLock()

    def stems(self, directory: Path | str, pattern: str) -> list[str]:
        folder = Path(directory)
        key = (str(folder.resolve()), pattern)
        now = monotonic()
        with self._lock:
            cached = self._items.get(key)
            if cached and cached[0] > now:
                return list(cached[1])
        values = [path.stem for path in folder.glob(pattern)] if folder.exists() else []
        with self._lock:
            self._items[key] = (now + self.ttl_seconds, values)
        return list(values)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


GLOBAL_ARTIFACT_CACHE = VersionedArtifactCache(
    max_entries=_positive_int("WEB_ARTIFACT_CACHE_ENTRIES", 24),
    max_bytes=_positive_int("WEB_ARTIFACT_CACHE_MB", 96) * 1024 * 1024,
    max_file_bytes=_positive_int("WEB_ARTIFACT_CACHE_MAX_FILE_MB", 20) * 1024 * 1024,
)
GLOBAL_DIRECTORY_CACHE = DirectoryIndexCache(
    ttl_seconds=float(os.getenv("WEB_DIRECTORY_CACHE_SECONDS", "30") or 30),
)


__all__ = [
    "DirectoryIndexCache",
    "GLOBAL_ARTIFACT_CACHE",
    "GLOBAL_DIRECTORY_CACHE",
    "VersionedArtifactCache",
]
