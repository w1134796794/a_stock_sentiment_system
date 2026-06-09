"""
生成复盘短视频「分镜脚本」storyboard JSON（P0）。

收盘跑批后调用，把当日快照压成 ``webdata/recaps/{date}.json``，作为后续
演出视图 / HyperFrames 出片的单一数据源。可复现、不依赖大模型。

用法：
    python scripts/build_recap.py                # 最新交易日
    python scripts/build_recap.py 20260605       # 指定交易日
    python scripts/build_recap.py --all          # 批量回填所有已有快照
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python scripts/build_recap.py` 运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from config.settings import SNAPSHOT_DIR  # noqa: E402
from recap.storyboard import build_and_save  # noqa: E402
from snapshot.reader import SnapshotReader  # noqa: E402


def _build_one(date: str | None) -> None:
    story = build_and_save(date)
    logger.info(
        f"[build_recap] {story['date']} 分镜已生成："
        f"{len(story['scenes'])} 幕 · 约 {story['total_duration']:.0f}s · 周期 {story['cycle']}"
    )


def main(argv: list[str]) -> int:
    args = [a for a in argv if a]
    if "--all" in args:
        reader = SnapshotReader(SNAPSHOT_DIR)
        dates = reader.list_dates()
        if not dates:
            logger.warning("[build_recap] 没有任何快照")
            return 1
        for d in dates:
            try:
                _build_one(d)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[build_recap] {d} 失败：{e}")
        return 0

    date = next((a for a in args if a.isdigit() and len(a) == 8), None)
    try:
        _build_one(date)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[build_recap] 失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
