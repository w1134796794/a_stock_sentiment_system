"""
复盘短视频配音（P4）—— 可插拔 TTS 适配器，缺失时优雅降级为「无配音」。

后端优先级（可用即用，均失败则返回 None，流水线继续出无声片）：
1. DashScope CosyVoice（需 ``DASHSCOPE_API_KEY`` + ``pip install dashscope``）→ mp3，音质好、上镜；
2. pyttsx3（离线，Windows 走 SAPI5，无需联网/Key）→ wav，作为兜底；

可用环境变量覆盖：
- ``RECAP_TTS_ENGINE``：``dashscope`` / ``pyttsx3`` / ``none``（强制关闭）；
- ``RECAP_TTS_VOICE``：音色（DashScope 默认 ``longxiaochun``）。

音频时长用 ffprobe（HyperFrames 已依赖 FFmpeg）或 wav 头探测，拿不到时按字数估算。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class VoiceClip:
    path: Path
    duration: float


# ---------------------------------------------------------------- 时长探测
def estimate_duration(text: str, cps: float = 4.3) -> float:
    """中文播报约 4~5 字/秒，给一点尾留白。"""
    n = len((text or "").strip())
    return max(1.5, round(n / cps + 0.6, 2))


def probe_duration(path: Path, text: str = "") -> float:
    p = str(path)
    exe = shutil.which("ffprobe")
    if exe:
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", p],
                capture_output=True, text=True, timeout=20)
            d = float((out.stdout or "").strip())
            if d > 0:
                return round(d, 3)
        except Exception:  # noqa: BLE001
            pass
    if p.lower().endswith(".wav"):
        try:
            with contextlib.closing(wave.open(p, "rb")) as w:
                return round(w.getnframes() / float(w.getframerate()), 3)
        except Exception:  # noqa: BLE001
            pass
    return estimate_duration(text)


# ---------------------------------------------------------------- 后端
class _DashScopeCosyVoice:
    name = "dashscope-cosyvoice"
    ext = "mp3"

    def __init__(self, api_key: str, voice: str = "longxiaochun", model: str = "cosyvoice-v1"):
        self.api_key = api_key
        self.voice = voice
        self.model = model

    def synth(self, text: str, out_noext: Path) -> Optional[VoiceClip]:
        try:
            import dashscope
            from dashscope.audio.tts_v2 import SpeechSynthesizer
        except Exception:  # noqa: BLE001
            return None
        try:
            dashscope.api_key = self.api_key
            syn = SpeechSynthesizer(model=self.model, voice=self.voice)
            audio = syn.call(text)
            if not audio:
                return None
            path = Path(f"{out_noext}.{self.ext}")
            path.write_bytes(audio)
            return VoiceClip(path, probe_duration(path, text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[tts] DashScope 合成失败：{e}")
            return None


class _Pyttsx3:
    name = "pyttsx3-sapi"
    ext = "wav"

    def __init__(self, voice: str = ""):
        self.voice = voice

    def synth(self, text: str, out_noext: Path) -> Optional[VoiceClip]:
        try:
            import pyttsx3
        except Exception:  # noqa: BLE001
            return None
        try:
            engine = pyttsx3.init()
            if self.voice:
                for v in engine.getProperty("voices"):
                    if self.voice in (v.id or "") or self.voice in (getattr(v, "name", "") or ""):
                        engine.setProperty("voice", v.id)
                        break
            path = Path(f"{out_noext}.{self.ext}")
            engine.save_to_file(text, str(path))
            engine.runAndWait()
            if not path.exists() or path.stat().st_size == 0:
                return None
            return VoiceClip(path, probe_duration(path, text))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[tts] pyttsx3 合成失败：{e}")
            return None


# ---------------------------------------------------------------- 选择引擎
def get_tts_engine(prefer: Optional[str] = None):
    """返回可用的 TTS 引擎实例；都不可用时返回 None。"""
    prefer = (prefer or os.getenv("RECAP_TTS_ENGINE", "")).strip().lower()
    voice = os.getenv("RECAP_TTS_VOICE", "").strip()
    if prefer == "none":
        return None

    key = os.getenv("DASHSCOPE_API_KEY", "").strip()

    def make_dashscope():
        if not key:
            return None
        try:
            import dashscope  # noqa: F401
        except Exception:  # noqa: BLE001
            return None
        return _DashScopeCosyVoice(key, voice or "longxiaochun")

    def make_pyttsx3():
        try:
            import pyttsx3  # noqa: F401
        except Exception:  # noqa: BLE001
            return None
        return _Pyttsx3(voice)

    if prefer == "dashscope":
        return make_dashscope()
    if prefer == "pyttsx3":
        return make_pyttsx3()
    return make_dashscope() or make_pyttsx3()


def synthesize_scenes(scenes: List[Dict], audio_dir: Path,
                      engine=None) -> Dict[str, VoiceClip]:
    """逐幕合成 narration 到 ``audio_dir/<key>.<ext>``。

    返回 {scene_key: VoiceClip}；引擎不可用或某幕失败则该幕缺省（静音）。
    """
    if engine is None:
        engine = get_tts_engine()
    if engine is None:
        logger.info("[tts] 未启用任何 TTS 引擎，输出将无配音（字幕仍可用）。")
        return {}
    audio_dir = Path(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, VoiceClip] = {}
    for sc in scenes:
        text = (sc.get("narration") or "").strip()
        key = sc.get("key")
        if not text or not key:
            continue
        clip = engine.synth(text, audio_dir / str(key))
        if clip:
            out[key] = clip
    logger.info(f"[tts] 引擎 {getattr(engine, 'name', '?')} 合成 {len(out)}/{len(scenes)} 幕配音。")
    return out
