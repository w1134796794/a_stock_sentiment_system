"""Publish complete JSON documents without exposing partially written files."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from core.infrastructure.file_mutex import file_mutex


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            # Windows readers/security scanners may briefly deny delete sharing.
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                raise
            time.sleep(min(0.01 * 2 ** attempt, 0.2))


def atomic_read_json(path: Path):
    path = Path(path)
    with file_mutex(str(path) + ".lock"):
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)


def atomic_write_json(path: Path, payload, *, default=None, indent=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with file_mutex(str(path) + ".lock"):
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, default=default, indent=indent)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path
