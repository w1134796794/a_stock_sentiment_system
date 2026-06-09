"""
复盘短视频「一键出片」流水线（P4）。

storyboard → 配音(TTS) → 时间轴对齐 → 字幕(SRT/VTT) → 自包含分镜 HTML →
（可选）HyperFrames 渲染 MP4。可直接挂到收盘跑批末尾，实现每日全自动出片。

用法：
    python scripts/make_recap_video.py                       # 最新交易日（含字幕，按环境决定是否配音）
    python scripts/make_recap_video.py 20260605              # 指定交易日
    python scripts/make_recap_video.py 20260605 --tts        # 强制开启配音
    python scripts/make_recap_video.py 20260605 --no-tts     # 关闭配音（无声片）
    python scripts/make_recap_video.py 20260605 --tts --render   # 配音 + 立即渲染出 MP4
    python scripts/make_recap_video.py --all                 # 批量回填所有已有快照（不渲染）

环境变量：RECAP_TTS_ENGINE=dashscope|pyttsx3|none，RECAP_TTS_VOICE=<音色>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from recap.pipeline import build_recap_video  # noqa: E402


def _report(r: dict) -> None:
    logger.info(f"[recap_video] {r['date']}：{r['scenes']} 幕 · 约 {r['total_duration']:.0f}s")
    logger.info(f"[recap_video] 分镜 HTML：{r['html']}")
    if r.get("srt"):
        logger.info(f"[recap_video] 字幕：{r['srt']} / {r['vtt']}")
    if r.get("audio"):
        logger.info(f"[recap_video] 配音：{len(r['audio'])} 幕")
    else:
        logger.info("[recap_video] 无配音（无声片，字幕可用）")
    if r["rendered"]:
        logger.info(f"[recap_video] 已渲染：{r['mp4']}")
    else:
        logger.info("[recap_video] 渲染命令（在 video/ 目录执行；需 Node>=22 + FFmpeg）：")
        logger.info("    " + " ".join(r["render_cmd"]))


def main(argv: list[str]) -> int:
    args = [a for a in argv if a]
    do_render = "--render" in args
    # 配音开关：--tts 强制开；--no-tts 强制关；缺省按环境自动探测
    if "--no-tts" in args:
        with_tts = False
    elif "--tts" in args:
        with_tts = True
    else:
        with_tts = True   # 默认尝试配音；引擎不可用会自动降级为无声片

    if "--all" in args:
        from config.settings import SNAPSHOT_DIR
        from snapshot.reader import SnapshotReader
        for d in SnapshotReader(SNAPSHOT_DIR).list_dates():
            try:
                _report(build_recap_video(d, with_tts=with_tts, render=False))
            except Exception as e:  # noqa: BLE001
                logger.error(f"[recap_video] {d} 失败：{e}")
        return 0

    date = next((a for a in args if a.isdigit() and len(a) == 8), None)
    try:
        r = build_recap_video(date, with_tts=with_tts, with_captions=True, render=do_render)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[recap_video] 失败：{e}")
        return 1
    _report(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
