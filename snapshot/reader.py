"""
SnapshotReader

Web 端只读访问。仅依赖标准库（不 import core / tushare），保证 Web 进程轻量。
优先读 ``snapshots/{date}.json``；列日期时回退扫描目录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from snapshot.artifact_cache import GLOBAL_ARTIFACT_CACHE, GLOBAL_DIRECTORY_CACHE


class SnapshotReader:
    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = Path(snapshot_dir)

    def list_dates(self) -> List[str]:
        if not self.snapshot_dir.exists():
            return []
        dates = [
            stem for stem in GLOBAL_DIRECTORY_CACHE.stems(self.snapshot_dir, "*.json")
            if stem.isdigit()
        ]
        return sorted(dates, reverse=True)

    def latest(self) -> Optional[str]:
        pointer = self.snapshot_dir / "latest.txt"
        if pointer.exists():
            val = pointer.read_text(encoding="utf-8").strip()
            if val and (self.snapshot_dir / f"{val}.json").exists():
                return val
        dates = self.list_dates()
        return dates[0] if dates else None

    def load(self, date: str) -> Optional[Dict[str, Any]]:
        path = self.snapshot_dir / f"{date}.json"
        if not path.exists():
            return None
        payload = GLOBAL_ARTIFACT_CACHE.load_json(path)
        return payload if isinstance(payload, dict) else None

    def load_latest(self) -> Optional[Dict[str, Any]]:
        date = self.latest()
        return self.load(date) if date else None
