"""
复盘短视频字幕（P4）—— 由 storyboard 时间轴**确定性**生成 SRT / VTT。

我们已经同时拥有「口播文案」和「逐幕时序」，因此无需 Whisper 之类的语音识别：
直接把每一幕的 narration 按句切分，并在该幕时间窗内按字数比例铺给字幕条，
即得到与画面/配音对齐、可复现的字幕。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

# 中文断句标点（保留标点，作为一条字幕的结尾）
_SENT_RE = re.compile(r"(?<=[。！？；!?;…])")


def split_sentences(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    return parts or [text]


def build_cues(story: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读取 storyboard，按幕→句生成字幕条 [{index,start,end,text}]。

    依赖每一幕的 ``start`` 与 ``duration``（流水线在配音对齐后会先 reflow）。
    """
    cues: List[Dict[str, Any]] = []
    idx = 1
    for sc in story.get("scenes") or []:
        text = (sc.get("narration") or "").strip()
        if not text:
            continue
        start = float(sc.get("start") or 0)
        dur = float(sc.get("duration") or 0)
        if dur <= 0:
            continue
        end = start + dur
        sents = split_sentences(text)
        weights = [max(1, len(s)) for s in sents]
        wtotal = sum(weights)
        t = start
        for i, (s, w) in enumerate(zip(sents, weights)):
            seg = (end - start) * (w / wtotal)
            c_end = end if i == len(sents) - 1 else min(end, t + seg)
            if c_end - t < 0.4:                 # 过短的句子并入下一条
                c_end = min(end, t + 0.8)
            cues.append({"index": idx, "start": round(t, 3), "end": round(c_end, 3), "text": s})
            t = c_end
            idx += 1
    return cues


def _ts(seconds: float, sep: str) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:                              # 进位修正
        s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(cues: List[Dict[str, Any]]) -> str:
    blocks = []
    for c in cues:
        blocks.append(f"{c['index']}\n{_ts(c['start'], ',')} --> {_ts(c['end'], ',')}\n{c['text']}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(cues: List[Dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for c in cues:
        lines.append(f"{_ts(c['start'], '.')} --> {_ts(c['end'], '.')}")
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def build_and_save_captions(story: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    """生成 captions.srt / captions.vtt 落盘，返回 {'srt':..., 'vtt':...}。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cues = build_cues(story)
    srt_path = out_dir / "captions.srt"
    vtt_path = out_dir / "captions.vtt"
    srt_path.write_text(to_srt(cues), encoding="utf-8")
    vtt_path.write_text(to_vtt(cues), encoding="utf-8")
    return {"srt": srt_path, "vtt": vtt_path, "cues": cues}
