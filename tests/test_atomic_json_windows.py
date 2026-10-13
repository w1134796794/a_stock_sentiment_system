from __future__ import annotations

import ctypes
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

import pytest

import core.infrastructure.atomic_json as atomic


def windows_error(code=5):
    error = PermissionError("file temporarily occupied")
    error.winerror = code
    return error


def test_replace_retries_transient_windows_access_errors_without_losing_json(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    atomic.atomic_write_json(path, {"state":"old"})
    replace = atomic.os.replace
    attempts = []
    def occupied_then_free(source, target):
        attempts.append(source)
        if len(attempts) <= 3:
            raise windows_error((5, 32, 33)[len(attempts)-1])
        return replace(source, target)
    monkeypatch.setattr(atomic.os, "replace", occupied_then_free)
    monkeypatch.setattr(atomic.time, "sleep", lambda delay: None)
    atomic.atomic_write_json(path, {"state":"done"})
    assert len(attempts) == 4
    assert atomic.atomic_read_json(path) == {"state":"done"}
    assert not list(tmp_path.glob("*.tmp"))


def test_permanent_denial_is_bounded_and_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    atomic.atomic_write_json(path, {"state":"old"})
    attempts = []
    def denied(*args):
        attempts.append(args)
        raise windows_error()
    monkeypatch.setattr(atomic.os, "replace", denied)
    monkeypatch.setattr(atomic.time, "sleep", lambda delay: None)
    with pytest.raises(PermissionError):
        atomic.atomic_write_json(path, {"state":"new"})
    assert len(attempts) == 8
    assert atomic.atomic_read_json(path) == {"state":"old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_non_windows_permission_error_is_not_retried(tmp_path, monkeypatch):
    attempts = []
    def denied(*args):
        attempts.append(args)
        raise PermissionError("permanent POSIX permission failure")
    monkeypatch.setattr(atomic.os, "replace", denied)
    with pytest.raises(PermissionError):
        atomic.atomic_write_json(tmp_path / "status.json", {})
    assert len(attempts) == 1


def test_concurrent_readers_and_writers_observe_only_complete_documents(tmp_path):
    path = tmp_path / "status.json"
    atomic.atomic_write_json(path, {"sequence":0,"payload":"x"*500})
    def writer():
        for sequence in range(100):
            atomic.atomic_write_json(path, {"sequence":sequence,"payload":"x"*500})
    def reader():
        for _ in range(120):
            value = atomic.atomic_read_json(path)
            assert value["payload"] == "x"*500
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer), *[pool.submit(reader) for _ in range(3)]]
        for future in futures:
            future.result(timeout=20)


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing regression")
def test_real_windows_reader_without_delete_sharing_does_not_fail_writer(tmp_path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    path = tmp_path / "status.json"
    atomic.atomic_write_json(path, {"state":"old"})
    # This is the sharing mode of an unrelated reader we cannot coordinate with.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    started = Event()
    errors = []
    def publish():
        started.set()
        try:
            atomic.atomic_write_json(path, {"state":"done"})
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=publish)
    try:
        thread.start()
        assert started.wait(timeout=2)
        time.sleep(.05)
        assert thread.is_alive()
    finally:
        kernel.CloseHandle(handle)
        if thread.ident is not None:
            thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    assert atomic.atomic_read_json(path) == {"state":"done"}
