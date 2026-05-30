"""
灌库：快照 → 知识块 → （可选）向量 → KBStore。按 date 幂等覆盖。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import loguru

from kb.chunker import snapshot_to_chunks
from kb.store import KBStore

logger = loguru.logger


def ingest_snapshot(snapshot: Dict[str, Any], store: KBStore, embedder: Optional[Any] = None) -> int:
    chunks = snapshot_to_chunks(snapshot)
    if not chunks:
        return 0
    date = chunks[0].date

    if embedder is not None:
        vecs = embedder.embed([c.text for c in chunks])
        if vecs is not None and vecs.shape[0] == len(chunks):
            for c, v in zip(chunks, vecs):
                c.embedding = v
        else:
            logger.info(f"[KB] {date} 未取得向量，按词法检索入库")

    store.delete_by_date(date, kinds=["market", "plan", "signal", "review"])
    n = store.upsert_many(chunks)
    logger.info(f"[KB] {date} 入库 {n} 块"
                f"（{'向量' if chunks[0].embedding is not None else '词法'}）")
    return n


def ingest_all(snapshot_dir: Path, store: KBStore, embedder: Optional[Any] = None) -> Dict[str, int]:
    """回填目录下全部快照。返回 {date: 块数}。"""
    from snapshot.reader import SnapshotReader

    reader = SnapshotReader(snapshot_dir)
    result: Dict[str, int] = {}
    for date in reader.list_dates():
        snap = reader.load(date)
        if snap is None:
            continue
        try:
            result[date] = ingest_snapshot(snap, store, embedder)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[KB] {date} 灌库失败（跳过）: {e}")
    logger.info(f"[KB] 回填完成：{len(result)} 天，共 {sum(result.values())} 块")
    return result