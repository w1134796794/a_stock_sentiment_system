"""Short-lived DuckDB connections serialized across application processes."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.infrastructure.file_mutex import file_mutex


class _Connection:
    def __init__(self, connection: Any, guard: Any):
        self._connection = connection
        self._guard = guard
        self._closed = False

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._connection.close()
            finally:
                self._guard.__exit__(None, None, None)


def connect(database: str | Path = ":memory:", *, read_only: bool = False,
            timeout: float = 60.0, **kwargs):
    import duckdb

    if str(database) in {":memory:", ""}:
        return duckdb.connect(str(database), read_only=read_only, **kwargs)
    path = Path(database).resolve()
    guard = file_mutex(str(path) + ".access.lock", timeout=timeout)
    guard.__enter__()
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection = duckdb.connect(str(path), read_only=read_only, **kwargs)
                return _Connection(connection, guard)
            except Exception as exc:
                message = str(exc).lower()
                locked = any(marker in message for marker in (
                    "could not set lock", "conflicting lock", "another program",
                    "\u53e6\u4e00\u4e2a\u7a0b\u5e8f", "used by another process",
                ))
                if not locked or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    except BaseException:
        guard.__exit__(None, None, None)
        raise
