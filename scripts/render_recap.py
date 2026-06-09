"""
生成某日复盘短视频的「自包含分镜 HTML」并给出 HyperFrames 渲染命令（P3）。

只做合成（不配音、不生成字幕），用于快速预览/调画面。完整出片用
``scripts/make_recap_video.py``。

用法：
    python scripts/render_recap.py                 # 最新交易日
    python scripts/render_recap.py 20260605        # 指定交易日
    python scripts/render_recap.py 20260605 --render   # 末尾自动调用 hyperframes render
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from recap.pipeline import build_recap_video  # noqa: E402


def main(argv: list[str]) -> int:
    args = [a for a in argv if a]
    do_render = "--render" in args
    date = next((a for a in args if a.isdigit() and len(a) == 8), None)
    try:
        r = build_recap_video(date, with_tts=False, with_captions=False, render=do_render)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[render_recap] 失败：{e}")
        return 1

    logger.info(f"[render_recap] {r['date']}：{r['scenes']} 幕 · 约 {r['total_duration']:.0f}s")
    logger.info(f"[render_recap] 分镜 HTML：{r['html']}")
    if r["rendered"]:
        logger.info(f"[render_recap] 已渲染：{r['mp4']}")
    else:
        logger.info("[render_recap] 渲染命令（在 video/ 目录执行；需 Node>=22 + FFmpeg）：")
        logger.info("    " + " ".join(r["render_cmd"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
