from datetime import datetime
import sqlite3
from types import SimpleNamespace

from core.operations.evidence import record_runtime_observations
from core.operations.ledger import TradingLedger
from core.realtime.debug_journal import IntradayDebugJournal


def test_default_debug_creates_no_files_or_observation_work(tmp_path, monkeypatch):
    monkeypatch.delenv("INTRADAY_DEBUG_ENABLED", raising=False)
    root = tmp_path / "debug"
    log = IntradayDebugJournal(root)
    log.record("evaluation", "20261119", payload={"reason": "observe"})
    assert not root.exists()
    ledger = SimpleNamespace(record_observations=lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert record_runtime_observations(ledger, None) == 0


def test_only_cycle_events_suppressed_signal_audit_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("INTRADAY_DEBUG_ENABLED", "false")
    ledger = TradingLedger(tmp_path / "portfolio.sqlite")
    assert ledger.event("cycle", "runtime_cycle", {}) is False
    assert ledger.event("signal", "signal", {"status": "confirmed"})
    assert ledger.event("fill", "paper_fill", {"price": 10})
    ledger.evidence("signal", {"status": "confirmed", "code": "000001"})
    with ledger._connect() as c:
        assert c.execute("SELECT kind FROM trading_events ORDER BY kind").fetchall()
        assert c.execute("SELECT count(*) FROM trading_events").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM signal_evidence").fetchone()[0] == 1
    monkeypatch.setenv("INTRADAY_DEBUG_ENABLED", " TRUE ")
    assert ledger.event("cycle", "runtime_cycle", {})
    IntradayDebugJournal(tmp_path / "enabled").record("cycle", "20261119")
    assert (tmp_path / "enabled" / "20261119.sqlite3").exists()


def test_cleanup_allowlist_preview_and_delete(tmp_path, monkeypatch):
    from scripts.cleanup_debug_files import candidates, cleanup
    from subprocess import CompletedProcess
    import scripts.cleanup_debug_files as module
    debug = tmp_path / "logs" / "intraday_debug"
    debug.mkdir(parents=True)
    selected = [debug / "20261119.sqlite3", debug / "20261119.sqlite3-wal",
                tmp_path / "portfolio-before-cleanup-20261116-224013-678401.sql.gz",
                tmp_path / "factors.duckdb.before-retention-20261116-224013-678401.gz"]
    preserved = [tmp_path / "portfolio.sqlite", tmp_path / "factors.duckdb",
                 debug / "unknown.sqlite3", tmp_path / "other-backup.gz"]
    for p in selected + preserved:
        p.write_text("data")
    assert set(candidates(tmp_path)) == set(selected)
    cleanup(tmp_path)
    assert all(p.exists() for p in selected + preserved)
    monkeypatch.setattr(module.shutil, "which", lambda *a: "lsof")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: CompletedProcess([], 1, b"", b""))
    cleanup(tmp_path, apply=True)
    assert not any(p.exists() for p in selected)
    assert all(p.exists() for p in preserved)


def test_cleanup_refuses_open_files(tmp_path, monkeypatch):
    import pytest
    from subprocess import CompletedProcess
    import scripts.cleanup_debug_files as module
    p = tmp_path / "portfolio-before-cleanup-20261116-224013-678401.sql.gz"
    p.write_text("backup")
    monkeypatch.setattr(module.shutil, "which", lambda *a: "lsof")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: CompletedProcess([], 0, b"123", b""))
    with pytest.raises(RuntimeError, match="nothing deleted"):
        module.cleanup(tmp_path, apply=True)
    assert p.exists()
