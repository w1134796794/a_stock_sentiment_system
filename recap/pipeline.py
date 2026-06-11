"""
复盘短视频「一键流水线」（P4）。

串联：storyboard → (可选) 配音 TTS → 时间轴对齐 reflow → 字幕 SRT/VTT →
自包含分镜 HTML → 给出（或执行）HyperFrames render 命令。

收盘跑批末尾调用 ``build_recap_video(date, with_tts=True, render=True)`` 即可全自动出片。
所有外部能力（TTS / Node / FFmpeg）缺失时优雅降级：
- 无 TTS → 出无声片（字幕仍在）；
- 无 Node/FFmpeg → 产出 HTML + 字幕，并打印好可复制的 render 命令。
"""
from __future__ import annotations

import math
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


def _build_dir(date: str, out_dir: Optional[Path]) -> Path:
    if out_dir is not None:
        return Path(out_dir)
    from config.settings import BASE_DIR
    return Path(BASE_DIR) / "video" / "build" / date


def reflow_scenes(story: Dict[str, Any], durations: Dict[str, float],
                  pad: float = 0.8, min_dur: float = 3.0) -> Dict[str, Any]:
    """按配音时长重排时间轴：每幕时长 ≥ 配音时长+留白，再累加 start。

    无对应配音的幕保留原时长。返回新的 story（深拷贝，不改原对象）。
    """
    story = deepcopy(story)
    t = 0.0
    for sc in story.get("scenes") or []:
        base = float(sc.get("duration") or 0)
        vo = durations.get(sc.get("key"))
        if vo:
            base = max(base, math.ceil(vo + pad))
        base = max(base, min_dur)
        sc["duration"] = float(base)
        sc["start"] = round(t, 3)
        t += base
    story["total_duration"] = round(t, 3)
    return story


def render_command(html_path: Path, out_path: Path) -> List[str]:
    """构造 HyperFrames 渲染命令（用 npx 调 CLI）。"""
    return ["npx", "hyperframes", "render", str(html_path), "--output", str(out_path)]


def _run_render(cmd: List[str], cwd: Path) -> bool:
    if shutil.which("npx") is None:
        logger.warning("[recap] 未找到 npx（需 Node.js >= 22），跳过渲染。请手动执行打印的命令。")
        return False
    logger.info(f"[recap] 开始渲染：{' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd))
        if proc.returncode == 0:
            return True
        logger.error(f"[recap] 渲染失败，退出码 {proc.returncode}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[recap] 渲染异常：{e}")
    return False


def build_recap_video(date: Optional[str] = None, *,
                      story: Optional[Dict[str, Any]] = None,
                      with_tts: bool = False,
                      with_captions: bool = True,
                      bgm: Optional[str] = None,
                      out_dir: Optional[Path] = None,
                      render: bool = False) -> Dict[str, Any]:
    """生成某日复盘短视频的全部出片素材，返回各产物路径与 render 命令。

    参数：
    - story：直接传入分镜（如合规版「情绪温度计」story）；缺省时按 date 加载/生成；
    - with_tts：是否逐幕合成配音（并据此对齐时间轴）；
    - with_captions：是否生成 SRT/VTT 字幕；
    - bgm：背景音乐文件（相对 composition.html 的路径或绝对路径）；
    - render：是否就地调用 hyperframes 渲染出 MP4。
    """
    from recap.storyboard import build_and_save, load_recap

    target = date
    if story is None:
        story = load_recap(target) if target else None
        if story is None:
            story = build_and_save(target)
    date = str(story.get("date") or target or "")

    out = _build_dir(date, out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 配音（可选）→ 2) 按配音对齐时间轴
    audio_manifest: Dict[str, Dict[str, Any]] = {}
    if with_tts:
        from recap.tts import synthesize_scenes
        clips = synthesize_scenes(story.get("scenes") or [], out / "audio")
        if clips:
            story = reflow_scenes(story, {k: c.duration for k, c in clips.items()})
            for key, clip in clips.items():
                audio_manifest[key] = {
                    "src": f"audio/{clip.path.name}",   # 相对 composition.html
                    "duration": clip.duration,
                }

    # 3) 字幕（在 reflow 之后，保证与画面/配音对齐）
    srt = vtt = None
    if with_captions:
        from recap.captions import build_and_save_captions
        caps = build_and_save_captions(story, out)
        srt, vtt = caps["srt"], caps["vtt"]

    # 4) 自包含分镜 HTML
    from recap.compose import build_and_save_composition
    html_path = build_and_save_composition(
        date, story=story, out_dir=out, audio=audio_manifest or None, bgm=bgm)

    # 5) render 命令（可选就地执行）
    mp4 = out / f"recap_{date}.mp4"
    cmd = render_command(html_path, mp4)
    rendered = False
    if render:
        from config.settings import BASE_DIR
        rendered = _run_render(cmd, cwd=Path(BASE_DIR) / "video")

    result = {
        "date": date,
        "dir": out,
        "html": html_path,
        "srt": srt,
        "vtt": vtt,
        "audio": audio_manifest,
        "mp4": mp4 if rendered else None,
        "mp4_target": mp4,
        "render_cmd": cmd,
        "total_duration": story.get("total_duration"),
        "scenes": len(story.get("scenes") or []),
        "rendered": rendered,
    }
    logger.info(f"[recap] {date} 出片素材就绪：{html_path}")
    return result