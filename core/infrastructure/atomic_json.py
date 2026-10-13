"""Publish complete JSON documents without exposing partially written files."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def atomic_write_json(path: Path, payload, *, default=None, indent=None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, default=default, indent=indent)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
