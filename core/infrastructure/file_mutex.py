"""Cooperative process/thread mutex with bounded waits and crash-safe release."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_registry_lock = threading.Lock()
_locks: dict[str, tuple[threading.RLock, threading.local]] = {}


@contextmanager
def file_mutex(path: Path | str, *, timeout: float = 60.0):
    key = os.path.normcase(str(Path(path).resolve()))
    with _registry_lock:
        lock, state = _locks.setdefault(key, (threading.RLock(), threading.local()))
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=max(timeout, 0)):
        raise TimeoutError(f"Timed out waiting for file mutex: {key}")
    handle = None
    try:
        depth = getattr(state, "depth", 0)
        if depth == 0:
            Path(key).parent.mkdir(parents=True, exist_ok=True)
            handle = open(key, "a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for file mutex: {key}")
                    time.sleep(0.05)
        state.depth = depth + 1
        try:
            yield
        finally:
            state.depth -= 1
    finally:
        if handle is not None:
            # Closing releases the OS lock, including when acquisition failed.
            handle.close()
        lock.release()
