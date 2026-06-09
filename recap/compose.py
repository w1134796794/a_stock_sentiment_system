"""
复盘短视频「分镜合成」（P3）—— storyboard JSON → 自包含 HyperFrames 分镜 HTML。

产物是一份**确定性、自包含**的 HTML 合成文件，可直接交给 HyperFrames 渲染：

    npx hyperframes lint   video/build/<date>/composition.html
    npx hyperframes render video/build/<date>/composition.html --output recap_<date>.mp4

HyperFrames 契约（见官方 HTML Schema）：
- 根节点 ``<div data-composition-id="root" data-start="0" data-width data-height>``；
- 每个分镜是带 ``class="clip"`` 的可见元素，自带 ``data-start/data-duration/data-track-index``，
  运行时按时间轴自动挂载/卸载；
- 必须创建一个 *paused* 的 GSAP 时间轴并注册到 ``window.__timelines["root"]``，
  总时长取自 ``tl.duration()``；
- 配音/BGM 用 ``<audio>`` clip（不加 class="clip"），由流水线（P4）注入。

设计：纯标准库、确定性（同一 storyboard → 同一 HTML），与演出视图（show.html）同一套视觉语言。
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

GSAP_CDN = "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"
FONT_CDN = ("https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700;900"
            "&family=JetBrains+Mono:wght@600;800&display=swap")

# 情绪周期主色（与 show.html 一致）：c=强调色, glow=光晕 rgb
ACCENTS = {
    "rose":    {"c": "#fb7185", "glow": "244,63,94"},
    "emerald": {"c": "#34d399", "glow": "16,185,129"},
    "sky":     {"c": "#38bdf8", "glow": "14,165,233"},
    "amber":   {"c": "#fbbf24", "glow": "245,158,11"},
    "slate":   {"c": "#a8b3c4", "glow": "100,116,139"},
}
# 涨跌语义色（A股惯例：红涨绿跌）
TONE = {"up": "#ff5d5d", "down": "#3ddc97", "neutral": "#e8edf5"}

VO_TRACK = 9   # 配音音轨
BGM_TRACK = 8  # 背景音乐音轨


# ---------------------------------------------------------------- 小工具
def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _fmt_num(v: Any) -> str:
    """与前端 toLocaleString 对齐：整数千分位，其余原样。"""
    if isinstance(v, bool):
        return _esc(v)
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            return f"{int(v):,}"
        return f"{v:,.2f}"
    return _esc(v)


def _fmt_date(date: str) -> str:
    s = str(date or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


# ---------------------------------------------------------------- 场景内联 HTML
def _stats_html(stats: List[Dict[str, Any]]) -> str:
    cols = " cols-3" if len(stats) == 3 else (" cols-1" if len(stats) == 1 else "")
    cards = []
    for s in stats:
        color = TONE.get(s.get("tone"), TONE["neutral"])
        unit = f'<span class="u">{_esc(s.get("unit"))}</span>' if s.get("unit") else ""
        cards.append(
            f'<div class="stat">'
            f'<div class="stat-val mono"><span style="color:{color}">{_fmt_num(s.get("value"))}</span>{unit}</div>'
            f'<div class="stat-label">{_esc(s.get("label"))}</div></div>'
        )
    return f'<div class="stats{cols}">{"".join(cards)}</div>'


def _list_html(items: List[Dict[str, Any]]) -> str:
    rows = []
    for i, it in enumerate(items):
        top = " top" if i == 0 else ""
        rank = _esc(it.get("rank") if it.get("rank") is not None else i + 1)
        code = f'<span class="row-code mono">{_esc(it.get("code"))}</span>' if it.get("code") else ""
        sub = f'<div class="row-sub">{_esc(it.get("sub"))}</div>' if it.get("sub") else ""
        val = ""
        if it.get("value") not in (None, ""):
            color = TONE.get(it.get("tone"), TONE["neutral"])
            unit = f'<span class="u">{_esc(it.get("unit"))}</span>' if it.get("unit") else ""
            val = (f'<div class="row-val mono"><span style="color:{color}">'
                   f'{_fmt_num(it.get("value"))}</span>{unit}</div>')
        rows.append(
            f'<div class="row{top}"><div class="rank mono">{rank}</div>'
            f'<div class="row-main"><div class="row-name">{_esc(it.get("name"))}{code}</div>{sub}</div>'
            f'{val}</div>'
        )
    return f'<div class="list">{"".join(rows)}</div>'


def _scene_inner(scene: Dict[str, Any], disclaimer: str) -> str:
    key = scene.get("key")
    stats = scene.get("stats") or []
    items = scene.get("list") or []
    centered = key in ("hook", "cta") or (not stats and not items)

    parts: List[str] = []
    if scene.get("subtitle") and key != "hook":
        parts.append(f'<div class="eyebrow">{_esc(scene["subtitle"])}</div>')
    parts.append(f'<div class="title{" hook" if key == "hook" else ""}">{_esc(scene.get("title"))}</div>')
    if key == "hook" and scene.get("subtitle"):
        parts.append(f'<div class="subtitle">{_esc(scene["subtitle"])}</div>')
    if stats:
        parts.append(_stats_html(stats))
    if items:
        parts.append(_list_html(items))
    if centered and scene.get("narration") and key != "cta":
        parts.append(f'<div class="lede">{_esc(scene["narration"])}</div>')
    if key == "cta":
        parts.append(f'<div class="fine">{_esc(disclaimer or scene.get("caption"))}</div>')

    scene_cls = "scene center" if centered else "scene"
    cap = (f'<div class="subband"><span class="cap">{_esc(scene["caption"])}</span></div>'
           if scene.get("caption") else "")
    return f'<div class="{scene_cls}">{"".join(parts)}</div>{cap}'


# ---------------------------------------------------------------- GSAP 时间轴
def _gsap_lines(scenes: List[Dict[str, Any]], total: float) -> str:
    """逐幕铺入场动画。只为实际存在的元素生成 tween，避免 GSAP "target not found"。"""
    lines = ['  const tl = gsap.timeline({ paused: true });']
    # 全局进度条贯穿全片（同时把时间轴时长锚定到 total）
    lines.append(f'  tl.fromTo("#progress", {{ scaleX: 0 }}, '
                 f'{{ scaleX: 1, duration: {total}, ease: "none" }}, 0);')
    for sc in scenes:
        s = float(sc.get("start") or 0)
        key = sc.get("key")
        sel = f'#scene-{key}'
        has_stats = bool(sc.get("stats"))
        has_list = bool(sc.get("list"))
        centered = key in ("hook", "cta") or (not has_stats and not has_list)
        # 副标题/眉标：hook 用 .subtitle，其余用 .eyebrow
        secondary = []
        if sc.get("subtitle") and key != "hook":
            secondary.append(f'{sel} .eyebrow')
        if key == "hook" and sc.get("subtitle"):
            secondary.append(f'{sel} .subtitle')

        lines.append(f'  tl.from("{sel} .title", {{ y: 64, opacity: 0, duration: 0.6, '
                     f'ease: "power3.out" }}, {s + 0.05});')
        if secondary:
            lines.append(f'  tl.from("{", ".join(secondary)}", {{ y: 36, opacity: 0, '
                         f'duration: 0.5, ease: "power2.out" }}, {s + 0.18});')
        if has_stats:
            lines.append(f'  tl.from("{sel} .stat", {{ y: 44, opacity: 0, duration: 0.5, '
                         f'stagger: 0.08, ease: "power3.out" }}, {s + 0.25});')
        if has_list:
            lines.append(f'  tl.from("{sel} .row", {{ x: 48, opacity: 0, duration: 0.5, '
                         f'stagger: 0.09, ease: "power3.out" }}, {s + 0.25});')
        if centered and key == "cta":
            lines.append(f'  tl.from("{sel} .fine", {{ y: 30, opacity: 0, duration: 0.5, '
                         f'ease: "power2.out" }}, {s + 0.35});')
        elif centered and sc.get("narration"):
            lines.append(f'  tl.from("{sel} .lede", {{ y: 30, opacity: 0, duration: 0.5, '
                         f'ease: "power2.out" }}, {s + 0.35});')
        if sc.get("caption"):
            lines.append(f'  tl.from("{sel} .subband", {{ y: 40, opacity: 0, duration: 0.4, '
                         f'ease: "power2.out" }}, {s + 0.15});')
    lines.append('  window.__timelines = window.__timelines || {};')
    lines.append('  window.__timelines["root"] = tl;')
    return "\n".join(lines)


# ---------------------------------------------------------------- 音轨
def _audio_clips(scenes: List[Dict[str, Any]], total: float,
                 audio: Optional[Dict[str, Dict[str, Any]]], bgm: Optional[str]) -> str:
    clips: List[str] = []
    audio = audio or {}
    for sc in scenes:
        info = audio.get(sc.get("key")) or sc.get("audio")
        if not info or not info.get("src"):
            continue
        start = float(sc.get("start") or 0)
        dur = info.get("duration")
        dur_attr = f' data-duration="{float(dur)}"' if dur else ""
        clips.append(f'  <audio id="vo-{_esc(sc.get("key"))}" data-start="{start}"{dur_attr} '
                     f'data-track-index="{VO_TRACK}" data-volume="1" src="{_esc(info["src"])}"></audio>')
    if bgm:
        clips.append(f'  <audio id="bgm" data-start="0" data-duration="{total}" '
                     f'data-track-index="{BGM_TRACK}" data-volume="0.12" src="{_esc(bgm)}"></audio>')
    return "\n".join(clips)


# ---------------------------------------------------------------- CSS
def _css(accent: Dict[str, str]) -> str:
    return """
  * { box-sizing: border-box; margin: 0; }
  html, body { width: 1080px; height: 1920px; background: #05070d; overflow: hidden;
               font-family: 'Noto Sans SC', system-ui, sans-serif; color: #f8fafc; }
  .mono { font-family: 'JetBrains Mono', monospace; }
  #root { position: relative; width: 1080px; height: 1920px; overflow: hidden;
    background:
      radial-gradient(120%% 80%% at 50%% -10%%, rgba(%(glow)s, .28), transparent 60%%),
      radial-gradient(90%% 60%% at 50%% 115%%, rgba(%(glow)s, .18), transparent 55%%),
      linear-gradient(180deg, #0b1220 0%%, #060912 60%%, #05070d 100%%); }
  #root::after { content:''; position:absolute; inset:0; pointer-events:none;
    background: radial-gradient(140%% 100%% at 50%% 50%%, transparent 60%%, rgba(0,0,0,.45) 100%%); }
  /* 持久层：进度条 + 胶囊 + 水印（非 clip，全程可见） */
  .progress-wrap { position:absolute; top:64px; left:90px; right:90px; height:8px;
    border-radius:99px; background:rgba(255,255,255,.16); overflow:hidden; z-index:5; }
  #progress { transform-origin:left center; height:100%%; width:100%%; background:%(accent)s; border-radius:99px; }
  .chips { position:absolute; top:96px; left:90px; display:flex; gap:16px; z-index:5; }
  .chip { font-size:28px; font-weight:700; padding:8px 22px; border-radius:99px;
    background:rgba(%(glow)s,.16); color:%(accent)s; border:2px solid rgba(%(glow)s,.35); }
  .chip.ghost { background:rgba(255,255,255,.06); color:#cbd5e1; border-color:rgba(255,255,255,.12); }
  .brand { position:absolute; top:96px; right:96px; font-size:26px; font-weight:800;
    color:rgba(255,255,255,.5); z-index:5; }
  /* 场景：每个 clip 铺满舞台，安全区内居中 */
  .clip.scene-clip { position:absolute; inset:0; }
  .safe { position:absolute; inset:0; padding:200px 90px 230px; display:flex; flex-direction:column; }
  .scene { flex:1; display:flex; flex-direction:column; justify-content:center; min-height:0; }
  .scene.center { align-items:center; text-align:center; }
  .eyebrow { font-size:30px; font-weight:700; letter-spacing:.12em; color:%(accent)s; margin-bottom:18px; }
  .title { font-size:86px; line-height:1.12; font-weight:900; letter-spacing:-.01em; }
  .title.hook { font-size:100px; }
  .subtitle { margin-top:22px; font-size:38px; font-weight:500; color:#cbd5e1; }
  .stats { display:grid; grid-template-columns:repeat(2,1fr); gap:30px; margin-top:56px; }
  .stats.cols-1 { grid-template-columns:1fr; } .stats.cols-3 { grid-template-columns:repeat(3,1fr); }
  .stat { background:rgba(255,255,255,.05); border:2px solid rgba(255,255,255,.08); border-radius:32px; padding:38px 34px; }
  .stat-val { font-size:92px; font-weight:900; line-height:1; letter-spacing:-.02em; }
  .stat-val .u { font-size:40px; font-weight:700; margin-left:10px; opacity:.85; }
  .stat-label { margin-top:16px; font-size:34px; color:#94a3b8; font-weight:600; }
  .list { margin-top:48px; display:flex; flex-direction:column; gap:22px; }
  .row { display:flex; align-items:center; gap:28px; background:rgba(255,255,255,.045);
    border:2px solid rgba(255,255,255,.07); border-radius:28px; padding:28px 34px; }
  .rank { flex:0 0 78px; height:78px; border-radius:22px; display:flex; align-items:center;
    justify-content:center; font-size:44px; font-weight:900; background:rgba(255,255,255,.08); color:#e2e8f0; }
  .row.top .rank { background:%(accent)s; color:#07120c; }
  .row-main { flex:1; min-width:0; }
  .row-name { font-size:48px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row-code { font-size:28px; color:#64748b; margin-left:14px; font-weight:600; }
  .row-sub { margin-top:8px; font-size:30px; color:#94a3b8; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .row-val { flex:0 0 auto; text-align:right; font-size:64px; font-weight:900; }
  .row-val .u { font-size:30px; font-weight:700; margin-left:8px; opacity:.8; }
  .lede { margin-top:40px; font-size:44px; line-height:1.5; font-weight:600; color:#e2e8f0; }
  .fine { margin-top:36px; font-size:28px; line-height:1.6; color:#64748b; max-width:760px; }
  .subband { position:absolute; left:0; right:0; bottom:0; padding:0 90px 70px; }
  .subband .cap { display:inline-block; max-width:100%%; font-size:40px; font-weight:700; line-height:1.4;
    padding:20px 34px; border-radius:24px; background:rgba(0,0,0,.55); border:2px solid rgba(255,255,255,.08); }
""" % {"accent": accent["c"], "glow": accent["glow"]}


# ---------------------------------------------------------------- 主入口
def build_composition_html(story: Dict[str, Any], *,
                           audio: Optional[Dict[str, Dict[str, Any]]] = None,
                           bgm: Optional[str] = None,
                           gsap_cdn: str = GSAP_CDN) -> str:
    """storyboard dict → 自包含 HyperFrames 分镜 HTML 字符串。"""
    scenes = story.get("scenes") or []
    fmt = story.get("format") or {}
    w = int(fmt.get("width") or 1080)
    h = int(fmt.get("height") or 1920)
    total = float(story.get("total_duration")
                  or sum(float(s.get("duration") or 0) for s in scenes))
    accent = ACCENTS.get(story.get("accent"), ACCENTS["emerald"])
    disclaimer = story.get("disclaimer") or ""

    scene_divs: List[str] = []
    for sc in scenes:
        scene_divs.append(
            f'  <div class="clip scene-clip" id="scene-{_esc(sc.get("key"))}" '
            f'data-start="{float(sc.get("start") or 0)}" '
            f'data-duration="{float(sc.get("duration") or 0)}" data-track-index="0">'
            f'<div class="safe">{_scene_inner(sc, disclaimer)}</div></div>'
        )

    audio_html = _audio_clips(scenes, total, audio, bgm)
    chips = (f'<div class="chips">'
             f'<span class="chip">{_esc(story.get("cycle") or "A股")}</span>'
             f'<span class="chip ghost">{_esc(_fmt_date(story.get("date")))}</span></div>')

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<title>{_esc(story.get("title") or "A股复盘")}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="{FONT_CDN}" rel="stylesheet">
<style>{_css(accent)}</style>
</head>
<body>
<div id="root" data-composition-id="root" data-start="0" data-width="{w}" data-height="{h}">
  <div class="progress-wrap"><div id="progress"></div></div>
  {chips}
  <div class="brand">每日收盘复盘</div>
{chr(10).join(scene_divs)}
{audio_html}
</div>
<script src="{gsap_cdn}"></script>
<script>
{_gsap_lines(scenes, total)}
</script>
</body>
</html>
"""


def build_and_save_composition(date: Optional[str] = None, *,
                               story: Optional[Dict[str, Any]] = None,
                               out_dir: Optional[Path] = None,
                               audio: Optional[Dict[str, Dict[str, Any]]] = None,
                               bgm: Optional[str] = None) -> Path:
    """生成某日分镜 HTML 并落盘到 ``video/build/<date>/composition.html``。

    story 缺省时按 date 读取（或重建）storyboard。返回写出的 HTML 路径。
    """
    from recap.storyboard import build_and_save, load_recap

    if story is None:
        target = date
        story = load_recap(target) if target else None
        if story is None:
            story = build_and_save(target)
    date = date or str(story.get("date") or "")

    if out_dir is None:
        from config.settings import BASE_DIR
        out_dir = Path(BASE_DIR) / "video" / "build" / date
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    html_str = build_composition_html(story, audio=audio, bgm=bgm)
    out_path = out_dir / "composition.html"
    out_path.write_text(html_str, encoding="utf-8")
    # 附带落一份当日 storyboard，方便排查
    (out_dir / "storyboard.json").write_text(
        json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
