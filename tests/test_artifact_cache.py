from __future__ import annotations

import json
import time
from pathlib import Path

from core.etl.daily_pipeline import _compact_strategy_result
from snapshot.artifact_cache import DirectoryIndexCache, VersionedArtifactCache
from snapshot.reader import SnapshotReader


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_json_cache_reuses_and_invalidates_by_file_version(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    _write(path, {"value": 1})
    cache = VersionedArtifactCache(max_entries=4, max_bytes=1024 * 1024)

    first = cache.load_json(path)
    second = cache.load_json(path)
    assert first is second
    assert cache.stats()["hits"] == 1

    time.sleep(0.002)
    _write(path, {"value": 2, "changed": True})
    assert cache.load_json(path)["value"] == 2
    assert cache.stats()["misses"] == 2


def test_json_cache_evicts_to_stay_within_budget(tmp_path: Path) -> None:
    cache = VersionedArtifactCache(max_entries=10, max_bytes=1200)
    for index in range(3):
        path = tmp_path / f"{index}.json"
        _write(path, {"value": "x" * 100})
        cache.load_json(path)

    stats = cache.stats()
    assert stats["estimated_bytes"] <= 1200
    assert stats["entries"] <= 1


def test_directory_index_and_snapshot_reader_return_sorted_dates(tmp_path: Path) -> None:
    _write(tmp_path / "20260102.json", {"meta": {"date": "20260102"}})
    _write(tmp_path / "20260101.json", {"meta": {"date": "20260101"}})
    _write(tmp_path / "notes.json", {})

    index = DirectoryIndexCache(ttl_seconds=30)
    assert sorted(index.stems(tmp_path, "*.json")) == ["20260101", "20260102", "notes"]
    assert SnapshotReader(tmp_path).list_dates() == ["20260102", "20260101"]


def test_strategy_aggregate_keeps_candidates_without_duplicate_diagnostics() -> None:
    compact = _compact_strategy_result({
        "strategy_id": "weak_to_strong",
        "final": [{"code": "000001"}],
        "candidate_pool": [{"code": "000001"}, {"code": "000002"}],
        "rejected": [{"code": "000003", "many_factors": [1] * 100}],
        "scenarios": {"base": [{"code": "000001"}]},
    })

    assert compact["final"] == [{"code": "000001"}]
    assert compact["candidate_pool_count"] == 2
    assert compact["rejected_count"] == 1
    assert compact["scenario_counts"] == {"base": 1}
    assert "candidate_pool" not in compact
    assert "rejected" not in compact
