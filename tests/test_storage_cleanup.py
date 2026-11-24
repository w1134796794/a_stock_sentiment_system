from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from subprocess import CompletedProcess
import sqlite3

import pytest
import scripts.maintain_storage as module
from scripts.install_storage_cleanup_systemd import render_units


def test_pressure_and_window(monkeypatch):
    monkeypatch.setattr(module.shutil, "disk_usage", lambda p: (1000, 800, 200))
    assert not module.pressure("/", 85, 0)
    assert module.pressure("/", 75, 0)
    assert module.pressure("/", 85, 1)
    assert module.night(datetime(2026, 11, 24, 23))
    assert module.night(datetime(2026, 11, 25, 5, 59))
    assert not module.night(datetime(2026, 11, 25, 6))
    assert not module.night(datetime(2026, 11, 25, 10))


def test_database_only_removes_old_diagnostics(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("CREATE TABLE runtime_observations(market_date TEXT,payload TEXT);"
                        "CREATE TABLE trading_events(kind TEXT,occurred_at TEXT);"
                        "CREATE TABLE portfolio_positions(id INTEGER,shares INTEGER);")
        c.executemany("INSERT INTO runtime_observations VALUES (?,?)",
                      [("20261116", "old")] * 1001 + [("20261117", "keep")])
        c.executemany("INSERT INTO trading_events VALUES (?,?)", [
            ("runtime_cycle", "2026-11-16"), ("runtime_cycle", "2026-11-17"),
            ("signal", "2026-01-01"), ("paper_fill", "2026-01-01")])
        c.execute("INSERT INTO portfolio_positions VALUES(1,100)")
    c.close()
    monkeypatch.setattr(module.shutil, "which", lambda p: "lsof")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: CompletedProcess([], 1, b"", b""))
    module.trim_database(tmp_path, "20261117")
    c = sqlite3.connect(path)
    assert c.execute("SELECT * FROM runtime_observations").fetchall() == [("20261117", "keep")]
    assert c.execute("SELECT count(*) FROM trading_events").fetchone()[0] == 3
    assert c.execute("SELECT * FROM portfolio_positions").fetchall() == [(1, 100)]
    c.close()


def test_no_pressure_has_no_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "pressure", lambda *a: False)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("no commands expected"))
    module.maintain(SimpleNamespace(root=tmp_path, threshold=85, minimum_free_gib=4))
    assert not list(tmp_path.iterdir())


def test_preview_does_not_write_or_stop_services(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "pressure", lambda *a: True)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("preview must not execute commands"))
    args = SimpleNamespace(root=tmp_path, threshold=85, minimum_free_gib=4, keep_days=7, apply=False)
    module.maintain(args)
    assert not list(tmp_path.iterdir())


def test_insufficient_reserve_preserves_database(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.sqlite"
    path.write_bytes(b"not a database; must not open")
    monkeypatch.setattr(module.shutil, "which", lambda p: "lsof")
    monkeypatch.setattr(module.shutil, "disk_usage", lambda p: SimpleNamespace(free=1))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: CompletedProcess([], 1, b"", b""))
    module.trim_database(tmp_path, "20261117")
    assert path.read_bytes() == b"not a database; must not open"


def test_units_are_opt_in_and_exclude_docker():
    units = render_units("/opt/a_stock", "/srv/a-stock/webdata", "deploy")
    service = units["a-stock-storage-cleanup.service"]
    assert "--threshold 70" in service and "--keep-days 7" in service
    assert "--apply" in service and "User=root" in service
    assert "docker" not in "".join(units.values())
    with pytest.raises(ValueError):
        render_units("/opt/app", "/srv/data", "root")


@pytest.mark.parametrize("worker_error", [False, True])
def test_maintenance_restores_services_on_failure(tmp_path, monkeypatch, worker_error):
    import sys
    import types
    calls = []
    (tmp_path / "portfolio.sqlite").touch()
    monkeypatch.setattr(module, "pressure", lambda *a: True)
    monkeypatch.setattr(module, "night", lambda *a: True)
    monkeypatch.setattr(module, "trim_files", lambda *a: None)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda p: p)
    fake = types.ModuleType("pwd")
    fake.getpwnam = lambda user: SimpleNamespace(pw_uid=(tmp_path / "portfolio.sqlite").stat().st_uid)
    # Windows fixture ownership is zero; use a mocked metadata object for the DB.
    original_stat = Path.stat
    def stat(p, *a, **k):
        result = original_stat(p, *a, **k)
        if p.name == "portfolio.sqlite":
            return SimpleNamespace(st_uid=1000, st_size=result.st_size, st_mode=result.st_mode)
        return result
    fake.getpwnam = lambda user: SimpleNamespace(pw_uid=1000)
    monkeypatch.setitem(sys.modules, "pwd", fake)
    monkeypatch.setattr(Path, "stat", stat)
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "runuser" and worker_error:
            raise RuntimeError("worker failed")
        return CompletedProcess(cmd, 0)
    monkeypatch.setattr(module.subprocess, "run", run)
    args = SimpleNamespace(root=tmp_path, threshold=85, minimum_free_gib=4,
                           keep_days=7, apply=True, user="deploy")
    if worker_error:
        with pytest.raises(RuntimeError, match="worker failed"):
            module.maintain(args)
    else:
        module.maintain(args)
    assert any(cmd[:2] == ["systemctl", "start"] for cmd in calls)
