"""Lossless storage maintenance, provenance, offline readers and rollback."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from core.data.minute_cache_files import (
    compress_minute_file,
    iter_minute_files,
    minute_cache_exists,
    read_minute_bytes,
    read_minute_csv,
    restore_minute_file,
    write_minute_csv,
)
from scripts.optimize_storage import compact_json_file


def frame():
    times = [
        *pd.date_range("2026-06-30 09:31", periods=120, freq="min"),
        *pd.date_range("2026-06-30 13:01", periods=120, freq="min"),
    ]
    return pd.DataFrame(
        dict(
            time=[x.strftime("%H:%M:%S") for x in times],
            trade_date="20260630",
            code="000001",
            open=10.0,
            high=10.1,
            low=9.9,
            close=10.0,
            volume=100,
            amount=1000,
        )
    )


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / "stock" / "tick"
    folder.mkdir(parents=True)
    file = folder / "000001.SZ_20260630.csv"
    frame().to_csv(file, index=False)
    return file


def test_preview_creates_no_files_and_estimates_same_compression(cache):
    before = {p.name for p in cache.parent.iterdir()}
    preview = compress_minute_file(cache)
    assert preview["saved_bytes"] > 0
    assert {p.name for p in cache.parent.iterdir()} == before
    result = compress_minute_file(cache, apply=True)
    assert result["compressed_bytes"] == preview["compressed_bytes"]
    assert result["saved_bytes"] == preview["saved_bytes"]


def test_byte_for_byte_round_trip_and_metadata(cache):
    original = cache.read_bytes()
    mtime = cache.stat().st_mtime_ns
    mode = cache.stat().st_mode & 0o777
    original_frame = pd.read_csv(cache)
    result = compress_minute_file(cache, apply=True)
    assert not cache.exists() and minute_cache_exists(cache)
    assert read_minute_bytes(cache) == original
    pd.testing.assert_frame_equal(read_minute_csv(cache), original_frame)
    packed = cache.with_suffix(".csv.gz")
    assert packed.stat().st_mtime_ns == mtime and packed.stat().st_mode & 0o777 == mode
    assert result["sha256_uncompressed"] == hashlib.sha256(original).hexdigest()
    assert compress_minute_file(cache, apply=True)["status"] == "already_compressed"
    assert restore_minute_file(cache)["status"] == "restored"
    assert cache.read_bytes() == original and not packed.exists()


def test_logical_iteration_deduplicates_both_formats(cache):
    with gzip.open(cache.with_suffix(".csv.gz"), "wb") as out:
        out.write(cache.read_bytes())
    assert list(iter_minute_files(cache.parent)) == [cache]
    original_size = cache.stat().st_size
    result = compress_minute_file(cache, apply=True)
    assert result["saved_bytes"] > original_size - 100  # The redundant original was reclaimed too.


def test_conflicting_gzip_is_preserved(cache):
    packed = cache.with_suffix(".csv.gz")
    with gzip.open(packed, "wb") as out:
        out.write(b"conflicting data")
    original = cache.read_bytes()
    with pytest.raises(RuntimeError, match="differs"):
        compress_minute_file(cache, apply=True)
    assert cache.read_bytes() == original
    with gzip.open(packed, "rb") as stream:
        assert stream.read() == b"conflicting data"


def test_missing_read_has_no_directory_or_lock_side_effect(tmp_path):
    path = tmp_path / "does-not-exist" / "000001.SZ_20260630.csv"
    assert not minute_cache_exists(path)
    with pytest.raises(FileNotFoundError):
        read_minute_csv(path)
    assert list(tmp_path.iterdir()) == []


def test_new_write_preserves_existing_compression(cache):
    compress_minute_file(cache, apply=True)
    updated = frame().assign(close=10.05)
    write_minute_csv(updated, cache)
    assert not cache.exists()
    assert read_minute_csv(cache)["close"].iloc[0] == 10.05
    write_minute_csv(updated, cache, compress=False)
    assert cache.exists() and not cache.with_suffix(".csv.gz").exists()


def test_opt_in_new_write_is_compressed(tmp_path, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "MINUTE_CACHE_COMPRESSION", True)
    path = tmp_path / "000001.SZ_20260630.csv"
    write_minute_csv(frame(), path)
    assert path.with_suffix(".csv.gz").exists() and not path.exists()
    pd.testing.assert_frame_equal(
        read_minute_csv(path), pd.read_csv(__import__("io").StringIO(frame().to_csv(index=False)))
    )


def test_corrupt_gzip_never_appears_as_valid_empty(cache):
    compress_minute_file(cache, apply=True)
    packed = cache.with_suffix(".csv.gz")
    packed.write_bytes(packed.read_bytes()[:-8])
    with pytest.raises((OSError, EOFError)):
        read_minute_bytes(cache)
    with pytest.raises((OSError, EOFError)):
        restore_minute_file(cache)
    assert not cache.exists() and packed.exists()


def test_gzip_does_not_trigger_remote_fetch(cache):
    from core.data.data_manager_stock import StockDataManager

    compress_minute_file(cache, apply=True)
    dm = StockDataManager.__new__(StockDataManager)
    dm.stock_dir = cache.parent.parent
    dm.allow_remote_history = False
    result = dm.get_stock_tick("000001.SZ", "20260630")
    assert len(result) == 240 and dm.get_minute_quality_report("000001.SZ", "20260630")["valid"]


def test_json_minification_keeps_fields_strings_and_numeric_tokens(tmp_path):
    file = tmp_path / "strategy_runs_20260630.json"
    original = b'{\n "number": 1.234567890123456789, "text": "a  b\\n c ", "results": {"x": {"rejected": [1,2,3]}}\n}'
    file.write_bytes(original)
    assert compact_json_file(file)["saved_bytes"] > 0 and file.read_bytes() == original
    result = compact_json_file(file, apply=True)
    assert result["saved_bytes"] == len(original) - len(file.read_bytes())
    assert b"1.234567890123456789" in file.read_bytes()
    assert json.loads(file.read_bytes()) == json.loads(original)
    assert json.loads(file.read_bytes())["results"]["x"]["rejected"] == [1, 2, 3]
    assert compact_json_file(file, apply=True)["status"] == "already_compact"


def test_duplicate_keys_not_silently_discarded(tmp_path):
    file = tmp_path / "strategy_runs_20260630.json"
    file.write_text('{"x":1,"x":2}')
    with pytest.raises(ValueError, match="Duplicate"):
        compact_json_file(file, apply=True)
    assert file.read_text() == '{"x":1,"x":2}'


def test_unsupported_file_and_symlink_rejected(cache, tmp_path):
    with pytest.raises(ValueError):
        compress_minute_file(tmp_path / "app.sqlite", apply=True)
    link = tmp_path / "linked_20260630.csv"
    link.symlink_to(cache)
    with pytest.raises(ValueError):
        compress_minute_file(link, apply=True)


def test_minute_training_source_hash_survives_compression(tmp_path):
    from core.signals.minute_amount_profile import (
        MinuteAmountProfileRepository,
        MinuteAmountProfileTrainer,
        validate_historical_profile,
    )

    folder = tmp_path / "tick"
    folder.mkdir()
    for index in range(1, 61):
        file = folder / f"{index:06d}.SZ_20260630.csv"
        frame().assign(code=f"{index:06d}").to_csv(file, index=False)
    repository = MinuteAmountProfileRepository(tmp_path / "profile.json")
    trainer = MinuteAmountProfileTrainer(cache_dir=folder, repository=repository)
    original = trainer.train_historical(start_date="20260601", end_date="20260630")
    assert original["ok"]
    for path in iter_minute_files(folder):
        compress_minute_file(path, apply=True)
    validate_historical_profile(original, folder, "20260701")
    repeated = trainer.train_historical(start_date="20260601", end_date="20260630")
    assert repeated["historical_training"]["sources"] == original["historical_training"]["sources"]


def test_cli_respects_cutoff_limit_preview_and_restore(cache, tmp_path, monkeypatch, capsys):
    from scripts.optimize_storage import main

    recent = cache.parent / "000002.SZ_20261025.csv"
    recent.write_bytes(cache.read_bytes())
    web = tmp_path / "webdata"
    folder = web / "screening" / "combinations"
    folder.mkdir(parents=True)
    report = folder / "strategy_runs_20260630.json"
    report.write_text(json.dumps({"full": list(range(50))}, indent=4))
    args = [
        "optimize",
        "--target",
        "all",
        "--cache-dir",
        str(cache.parent.parent.parent),
        "--web-data-dir",
        str(web),
        "--before",
        "20261025",
    ]
    monkeypatch.setattr(sys, "argv", args)
    assert main() == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["minute_files"] == 1 and preview["combination_files"] == 1 and preview["saved_bytes"] > 0
    assert cache.exists() and recent.exists()
    monkeypatch.setattr(sys, "argv", args + ["--apply"])
    assert main() == 0
    actual = json.loads(capsys.readouterr().out)
    assert actual["saved_bytes"] == preview["saved_bytes"]
    assert not cache.exists() and recent.exists()
    monkeypatch.setattr(sys, "argv", args + ["--apply", "--target", "minutes", "--restore-minutes"])
    assert main() == 0
    capsys.readouterr()
    assert cache.exists()


def test_strategy_training_reads_gzip(cache):
    from core.factors.strategy_training import StrategyMinuteTrainingBuilder

    compress_minute_file(cache, apply=True)
    loader = StrategyMinuteTrainingBuilder.__new__(StrategyMinuteTrainingBuilder)
    from collections import OrderedDict

    loader.tick_dir = cache.parent
    loader._tick_cache = OrderedDict()
    loader._cache_limit = 64
    assert len(loader._load_tick("000001.SZ", "20260630")) == 240


def test_cli_rejects_future_cutoff(monkeypatch):
    from datetime import datetime

    from scripts import optimize_storage

    monkeypatch.setattr(optimize_storage, "shanghai_now", lambda: datetime(2026, 10, 25))
    monkeypatch.setattr(sys, "argv", ["optimize", "--before", "20261026"])
    with pytest.raises(SystemExit) as exc:
        optimize_storage.main()
    assert exc.value.code == 2


def test_cli_skips_large_json(tmp_path, monkeypatch, capsys):
    from scripts import optimize_storage

    folder = tmp_path / "screening" / "combinations"
    folder.mkdir(parents=True)
    path = folder / "screening_20260630.json"
    raw = b'{"value":"' + b"x" * (1024 * 1024) + b'"}'
    path.write_bytes(raw)
    monkeypatch.setattr(
        sys,
        "argv",
        ["optimize", "--target", "combinations", "--web-data-dir", str(tmp_path), "--max-json-mb", "1", "--apply"],
    )
    assert optimize_storage.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["skipped"] == 1 and result["files"] == 0
    assert path.read_bytes() == raw
