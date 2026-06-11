"""
合规版「情绪温度计」内容生成器。

把每日快照压成**只含市场整体聚合数据 + 合规文案**的内容包，并一键产出：
- 文案.md   口播稿 + 小红书标题/正文 + 标签 + 风险提示（复制即用）；
- 卡片.html 自包含 9:16 暗色卡片页，浏览器一键下载 PNG（发小红书）；
- story.json 合规分镜（recap.v1 schema），可走 recap.pipeline 出 MP4。

与 recap/storyboard.py 的区别（关键）：
storyboard 会**点名个股、显示选股/置信度/明日仓位**，适合自己复盘，不适合公开发布；
本模块是其**合规子集**——只描述"已发生的市场整体状态"，不点名个股、不预测个体、
不给买卖/仓位建议。对外发布一律用本模块。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from recap.storyboard import (
    CYCLE_ACCENT,
    DEFAULT_FORMAT,
    SCHEMA,
    _fmt_date,
    _int,
    _num,
    _p1,
    _rows,
    _section_by_name,
)

# 合规风险提示（口播 / 字幕 / 图文 / 简介）
DISCLAIMER_ORAL = "以上为市场状态记录，不构成投资建议，股市有风险，决策请独立判断。"
DISCLAIMER_CAPTION = "内容仅供学习交流 · 不构成投资建议 · 市场有风险"
DISCLAIMER_TEXT = "📌 本内容为个人复盘/市场记录，不构成任何投资建议，据此操作风险自负。"

# 情绪周期 → 一句话"温度"措辞（描述已发生状态，不预测、不建议）
CYCLE_TEMP = {
    "高潮期": "情绪火热，高位分歧加大",
    "上升期": "赚钱效应回升，情绪偏暖",
    "震荡期": "多空胶着，情绪反复",
    "退潮期": "情绪退潮转冷",
    "冰点期": "情绪处于冰点",
}
CYCLE_EMOJI = {"高潮期": "🔥", "上升期": "📈", "震荡期": "🌊", "退潮期": "🍂", "冰点期": "❄️"}


# ------------------------------------------------------------------ 数据抽取
def _board_dist_text(metrics: Dict[str, Any]) -> str:
    """board_distribution {"1.0":58,...} → "1板58家·2板7家·3板4家"。"""
    dist = metrics.get("board_distribution") or {}
    items = []
    for k in sorted(dist.keys(), key=lambda x: _num(x) or 0):
        b = _int(k)
        c = _int(dist.get(k))
        if b and c:
            items.append(f"{b}板{c}家")
    return "·".join(items)


def build_thermometer(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """snapshot dict → 合规内容包（数据 + 文案）。"""
    meta = snapshot.get("meta") or {}
    date = str(meta.get("date") or "")
    market = snapshot.get("market") or {}
    metrics = market.get("metrics") or {}
    env = market.get("env") or {}
    sections = snapshot.get("sections") or []
    cycle = market.get("cycle_name") or ""
    accent = CYCLE_ACCENT.get(cycle, "emerald")

    width = env.get("width") or {}
    sh = env.get("sh_index") or {}

    data = {
        "cycle": cycle,
        "limit_up": _int(metrics.get("limit_up_count")),
        "limit_down": _int(metrics.get("limit_down_count")),
        "max_board": _int(metrics.get("max_board_height")),
        "broken_rate": _p1(metrics.get("broken_rate")),
        "continuous_rate": _p1(metrics.get("continuous_rate")),
        "board_dist": _board_dist_text(metrics),
        "up_count": _int(width.get("up_count")),
        "down_count": _int(width.get("down_count")),
        "sh_chg": _p1(sh.get("change_pct")),
        "volume_state": (env.get("volume") or {}).get("state") or "",
        "trend_state": (env.get("trend") or {}).get("state") or "",
        "risk_level": env.get("risk_level") or "",
        "composite_score": _int(env.get("composite_score")),
    }

    # 主线方向：概念连板梯队（只取概念名+涨停总数；剔除龙头个股名，保合规）
    sec = _section_by_name(sections, "概念连板梯队")
    rows = sorted(_rows(sec), key=lambda r: _num(r.get("涨停总数")) or 0, reverse=True)
    mainline = [
        {"name": r.get("概念名称") or "", "count": _int(r.get("涨停总数")),
         "max_board": _int(r.get("最高连板"))}
        for r in rows[:5] if r.get("概念名称")
    ]

    pack = {
        "schema": "thermometer.v1",
        "date": date,
        "cycle": cycle,
        "accent": accent,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data": data,
        "mainline": mainline,
    }
    pack["copy"] = _compose_copy(pack)
    return pack


# ------------------------------------------------------------------ 文案
def _summary_word(cycle: str) -> str:
    return CYCLE_TEMP.get(cycle, "市场情绪以中性为主")


def _concepts_phrase(mainline: List[Dict[str, Any]], n: int = 3) -> str:
    names = [m["name"] for m in mainline[:n] if m.get("name")]
    return "、".join(names) if names else "无明显主线"


def _compose_copy(pack: Dict[str, Any]) -> Dict[str, Any]:
    d = pack["data"]
    date = pack["date"]
    cycle = pack["cycle"] or "—"
    mmdd = _fmt_date(date)
    concepts = _concepts_phrase(pack["mainline"])
    emoji = CYCLE_EMOJI.get(cycle, "📊")

    def _v(x, suffix=""):
        return f"{x}{suffix}" if x is not None else "—"

    # 口播稿（30 秒）
    oral_lines = [
        f"{mmdd} A股情绪温度计。",
        f"今天市场处于{cycle}。",
        f"全场涨停{_v(d['limit_up'])}家、跌停{_v(d['limit_down'])}家，"
        f"最高连板{_v(d['max_board'])}板，炸板率{_v(d['broken_rate'],'%')}。",
    ]
    if d["board_dist"]:
        oral_lines.append(f"涨停梯队呈「{d['board_dist']}」。")
    if d["up_count"] is not None and d["down_count"] is not None:
        oral_lines.append(f"市场宽度上涨{d['up_count']}家、下跌{d['down_count']}家，量能{d['volume_state'] or '正常'}。")
    oral_lines.append(f"热点集中在{concepts}方向。")
    oral_lines.append(f"一句话总结：今天{_summary_word(cycle)}。")
    oral_lines.append(DISCLAIMER_ORAL)
    oral = "\n".join(oral_lines)

    # 小红书标题 / 正文 / 标签
    xhs_title = f"{mmdd} A股情绪温度计 | {cycle}{emoji}"
    xhs_body = "\n".join([
        f"{mmdd} 收盘 · A股情绪温度计{emoji}",
        "",
        f"🌡️ 情绪周期：{cycle}",
        f"📊 涨停 {_v(d['limit_up'])} / 跌停 {_v(d['limit_down'])} / 最高 {_v(d['max_board'])}板 / 炸板率 {_v(d['broken_rate'],'%')}",
        f"🪜 梯队：{d['board_dist'] or '—'}",
        f"📈 宽度：涨{_v(d['up_count'])} / 跌{_v(d['down_count'])}　量能{d['volume_state'] or '—'}",
        f"🎯 主线：{concepts}",
        f"📝 一句话：{_summary_word(cycle)}",
        "",
        DISCLAIMER_TEXT,
    ])
    tags = ["#复盘笔记", "#交易日记", "#市场观察", "#A股", "#情绪周期"]

    return {
        "oral": oral,
        "xhs_title": xhs_title,
        "xhs_body": xhs_body,
        "tags": tags,
        "summary": _summary_word(cycle),
        "disclaimer_caption": DISCLAIMER_CAPTION,
    }


def render_caption_md(pack: Dict[str, Any]) -> str:
    c = pack["copy"]
    return "\n".join([
        f"# 情绪温度计文案 · {pack['date']}（{pack['cycle']}）",
        "",
        "## 一、抖音/视频号 口播稿（结尾必念风险提示）",
        "",
        "```",
        c["oral"],
        "```",
        "",
        "## 二、小红书图文",
        "",
        f"**标题**：{c['xhs_title']}",
        "",
        "**正文**：",
        "",
        "```",
        c["xhs_body"],
        "```",
        "",
        f"**标签**：{' '.join(c['tags'])}",
        "",
        "## 三、字幕风险提示（每条片尾 2 秒）",
        "",
        f"> {c['disclaimer_caption']}",
        "",
        "---",
        "_本文案由 recap/thermometer.py 自动生成，仅含市场整体聚合数据，不点名个股、不构成投资建议。_",
    ])


# ------------------------------------------------------------------ 图文卡片（自包含 HTML）
_ACCENT_HEX = {"rose": "#fb7185", "emerald": "#34d399", "sky": "#38bdf8",
               "amber": "#fbbf24", "slate": "#a8b3c4"}


def _stat_html(label: str, value: Any, unit: str = "", tone: str = "neutral") -> str:
    color = {"up": "#ff5d5d", "down": "#3ddc97"}.get(tone, "#e8edf5")
    val = "—" if value is None else value
    return (f'<div class="stat"><div class="sv" style="color:{color}">{val}'
            f'<span class="u">{unit}</span></div><div class="sl">{label}</div></div>')


def render_cards_html(pack: Dict[str, Any]) -> str:
    d = pack["data"]
    c = pack["copy"]
    accent = _ACCENT_HEX.get(pack["accent"], "#34d399")
    mmdd = _fmt_date(pack["date"])
    cycle = pack["cycle"] or "—"
    emoji = CYCLE_EMOJI.get(cycle, "📊")
    concepts = "".join(
        f'<span class="cpt">{m["name"]} '
        f'<b>{m["count"] if m["count"] is not None else "—"}</b></span>'
        for m in pack["mainline"][:5]
    ) or '<span class="cpt">无明显主线</span>'

    # 4 张卡：封面 / 情绪面 / 梯队结构 / 主线方向
    card_cover = f'''
      <section class="card" id="card-1">
        <div class="badge">市场记录 · 非投资建议</div>
        <div class="eyebrow">A股情绪温度计</div>
        <div class="big">{cycle}{emoji}</div>
        <div class="sub">{mmdd} 收盘 · 市场情绪一览</div>
        <div class="summary">{c["summary"]}</div>
        <div class="foot">{c["disclaimer_caption"]}</div>
      </section>'''

    card_emotion = f'''
      <section class="card" id="card-2">
        <div class="badge">市场记录 · 非投资建议</div>
        <div class="eyebrow">情绪面</div>
        <div class="grid">
          {_stat_html("涨停", d["limit_up"], "家", "up")}
          {_stat_html("跌停", d["limit_down"], "家", "down")}
          {_stat_html("最高板", d["max_board"], "板", "up")}
          {_stat_html("炸板率", d["broken_rate"], "%", "down")}
          {_stat_html("上证", d["sh_chg"], "%", "up" if (d["sh_chg"] or 0) >= 0 else "down")}
          {_stat_html("综合分", d["composite_score"], "", "neutral")}
        </div>
        <div class="foot">{c["disclaimer_caption"]}</div>
      </section>'''

    card_echelon = f'''
      <section class="card" id="card-3">
        <div class="badge">市场记录 · 非投资建议</div>
        <div class="eyebrow">涨停梯队结构</div>
        <div class="dist">{d["board_dist"] or "—"}</div>
        <div class="grid two">
          {_stat_html("上涨", d["up_count"], "家", "up")}
          {_stat_html("下跌", d["down_count"], "家", "down")}
        </div>
        <div class="kv">连板率 <b>{d["continuous_rate"] if d["continuous_rate"] is not None else "—"}%</b> · 量能 <b>{d["volume_state"] or "—"}</b> · 风险 <b>{d["risk_level"] or "—"}</b></div>
        <div class="foot">{c["disclaimer_caption"]}</div>
      </section>'''

    card_mainline = f'''
      <section class="card" id="card-4">
        <div class="badge">市场记录 · 非投资建议</div>
        <div class="eyebrow">今日主线方向</div>
        <div class="cpts">{concepts}</div>
        <div class="summary">{c["summary"]}</div>
        <div class="foot">{c["disclaimer_caption"]}</div>
      </section>'''

    cards = card_cover + card_emotion + card_echelon + card_mainline

    return f'''<!doctype html>
<html lang="zh"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>情绪温度计 · {pack["date"]}</title>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<style>
  :root {{ --accent:{accent}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#05070d; color:#f8fafc; font-family:'Noto Sans SC',system-ui,sans-serif; padding:24px; }}
  .bar {{ display:flex; gap:10px; align-items:center; margin-bottom:18px; flex-wrap:wrap; }}
  .bar h1 {{ font-size:18px; margin:0; font-weight:800; }}
  .bar button {{ background:var(--accent); color:#06140d; border:none; border-radius:10px;
                 padding:8px 16px; font-weight:700; cursor:pointer; }}
  .bar .hint {{ color:#64748b; font-size:13px; }}
  .deck {{ display:flex; gap:20px; flex-wrap:wrap; }}
  .card {{ position:relative; width:360px; height:640px; border-radius:28px; padding:48px 40px;
           display:flex; flex-direction:column; overflow:hidden;
           background:
             radial-gradient(120% 80% at 50% -10%, color-mix(in srgb, var(--accent) 28%, transparent), transparent 60%),
             linear-gradient(180deg,#0b1220 0%,#060912 60%,#05070d 100%);
           border:1px solid rgba(255,255,255,.08); }}
  .badge {{ position:absolute; top:22px; left:40px; font-size:13px; color:#94a3b8;
            border:1px solid rgba(255,255,255,.14); border-radius:99px; padding:5px 12px; }}
  .eyebrow {{ margin-top:54px; font-size:18px; font-weight:700; letter-spacing:.1em; color:var(--accent); }}
  .big {{ font-size:64px; font-weight:900; margin-top:18px; line-height:1.1; }}
  .sub {{ margin-top:14px; font-size:20px; color:#cbd5e1; }}
  .summary {{ margin-top:auto; margin-bottom:14px; font-size:24px; font-weight:600; color:#e2e8f0; line-height:1.5; }}
  .grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:18px; margin-top:30px; }}
  .grid.two {{ grid-template-columns:repeat(2,1fr); margin-top:18px; }}
  .stat {{ background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.08);
           border-radius:18px; padding:18px 16px; }}
  .sv {{ font-size:40px; font-weight:900; font-family:'JetBrains Mono',monospace; line-height:1; }}
  .sv .u {{ font-size:16px; font-weight:700; margin-left:4px; opacity:.85; }}
  .sl {{ margin-top:10px; font-size:16px; color:#94a3b8; }}
  .dist {{ margin-top:26px; font-size:30px; font-weight:800; font-family:'JetBrains Mono',monospace; color:var(--accent); }}
  .kv {{ margin-top:18px; font-size:18px; color:#94a3b8; }} .kv b {{ color:#e2e8f0; }}
  .cpts {{ margin-top:26px; display:flex; flex-direction:column; gap:12px; }}
  .cpt {{ font-size:24px; font-weight:700; background:rgba(255,255,255,.05);
          border:1px solid rgba(255,255,255,.08); border-radius:14px; padding:14px 18px; }}
  .cpt b {{ color:var(--accent); margin-left:8px; }}
  .foot {{ position:absolute; bottom:30px; left:40px; right:40px; font-size:14px; color:#64748b; }}
</style></head>
<body>
  <div class="bar">
    <h1>情绪温度计 · {pack["date"]} · {cycle}</h1>
    <button onclick="dl()">下载全部 PNG</button>
    <span class="hint">合规图文：仅市场整体数据，无个股 · 无收益 · 无操作建议</span>
  </div>
  <div class="deck" id="deck">{cards}</div>
<script>
  async function dl() {{
    const cards = document.querySelectorAll('.card');
    for (let i=0;i<cards.length;i++) {{
      const cv = await html2canvas(cards[i], {{scale:2, backgroundColor:null}});
      const a = document.createElement('a');
      a.href = cv.toDataURL('image/png');
      a.download = '情绪温度计_{pack["date"]}_'+(i+1)+'.png';
      a.click();
      await new Promise(r=>setTimeout(r,300));
    }}
  }}
</script>
</body></html>'''


# ------------------------------------------------------------------ 合规分镜（供视频管线）
def build_story(pack: Dict[str, Any]) -> Dict[str, Any]:
    """合规内容包 → recap.v1 storyboard（仅市场整体场景，可走 recap.pipeline 出 MP4）。"""
    d = pack["data"]
    accent = pack["accent"]
    date = pack["date"]
    cycle = pack["cycle"] or "A股"

    def _stats():
        out = []
        if d["limit_up"] is not None:
            out.append({"label": "涨停", "value": d["limit_up"], "unit": "家", "tone": "up"})
        if d["limit_down"] is not None:
            out.append({"label": "跌停", "value": d["limit_down"], "unit": "家", "tone": "down"})
        if d["max_board"] is not None:
            out.append({"label": "最高板", "value": d["max_board"], "unit": "板", "tone": "up"})
        if d["broken_rate"] is not None:
            out.append({"label": "炸板率", "value": d["broken_rate"], "unit": "%", "tone": "down"})
        return out

    mainline_list = [
        {"rank": i, "name": m["name"], "value": m["count"], "unit": "家涨停",
         "sub": f"最高{m['max_board']}板" if m.get("max_board") else ""}
        for i, m in enumerate(pack["mainline"][:4], 1)
    ]

    scenes = [
        {"key": "hook", "title": f"{cycle} · 情绪温度计", "subtitle": f"{_fmt_date(date)} · A股市场记录",
         "narration": f"{_fmt_date(date)}，先看一眼今天的市场情绪。",
         "caption": f"{cycle} · 今日市场情绪", "stats": [], "list": [], "duration": 4, "accent": accent},
        {"key": "emotion", "title": "情绪温度计", "subtitle": cycle,
         "narration": (f"今天市场{cycle}，涨停{d['limit_up']}家、跌停{d['limit_down']}家，"
                       f"最高{d['max_board']}板，炸板率{d['broken_rate']}%。"),
         "caption": "今日情绪一览", "stats": _stats(), "list": [], "duration": 9, "accent": accent},
        {"key": "echelon", "title": "涨停梯队结构", "subtitle": d["board_dist"],
         "narration": (f"涨停梯队呈{d['board_dist']}，市场宽度上涨{d['up_count']}家、下跌{d['down_count']}家。"),
         "caption": "梯队结构", "stats": [], "list": [], "duration": 8, "accent": accent},
        {"key": "mainline", "title": "今日主线方向", "subtitle": "板块连板梯队",
         "narration": f"今天热点集中在{_concepts_phrase(pack['mainline'])}方向。",
         "caption": "今天钱往哪儿走", "stats": [], "list": mainline_list, "duration": 9, "accent": accent},
        {"key": "cta", "title": "今日市场记录结束", "subtitle": "只讲状态 · 不荐股 · 不预测",
         "narration": DISCLAIMER_ORAL, "caption": DISCLAIMER_CAPTION,
         "stats": [], "list": [], "duration": 4, "accent": accent},
    ]
    t = 0.0
    for s in scenes:
        s["start"] = round(t, 2)
        t += float(s.get("duration") or 0)

    return {
        "schema": SCHEMA, "date": date, "generated_at": pack["generated_at"],
        "format": dict(DEFAULT_FORMAT), "cycle": cycle, "accent": accent,
        "title": f"{cycle} 情绪温度计 | {_fmt_date(date)}",
        "hook": f"{cycle} · 情绪温度计",
        "disclaimer": DISCLAIMER_ORAL, "scenes": scenes, "total_duration": round(t, 2),
    }


# ------------------------------------------------------------------ 落盘 / CLI
def _content_dir(date: str) -> Path:
    from config.settings import WEB_DATA_DIR
    return Path(WEB_DATA_DIR) / "content" / date


def save(date: Optional[str] = None, *, with_video: bool = False,
         render: bool = False) -> Dict[str, Any]:
    """生成并落盘某日的图文/视频素材。返回各产物路径。"""
    from config.settings import SNAPSHOT_DIR
    from snapshot.reader import SnapshotReader

    reader = SnapshotReader(SNAPSHOT_DIR)
    date = date or reader.latest()
    if not date:
        raise FileNotFoundError("没有可用的快照")
    snapshot = reader.load(date)
    if snapshot is None:
        raise FileNotFoundError(f"找不到 {date} 的快照")

    pack = build_thermometer(snapshot)
    out = _content_dir(date)
    out.mkdir(parents=True, exist_ok=True)

    md_path = out / "文案.md"
    html_path = out / "卡片.html"
    story_path = out / "story.json"
    md_path.write_text(render_caption_md(pack), encoding="utf-8")
    html_path.write_text(render_cards_html(pack), encoding="utf-8")
    story = build_story(pack)
    story_path.write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"date": date, "dir": out, "文案": md_path, "卡片": html_path, "story": story_path,
              "video": None}

    if with_video:
        from recap.pipeline import build_recap_video
        video = build_recap_video(date, story=story, with_captions=True, render=render)
        result["video"] = video

    return result


def main() -> None:
    import sys

    args = [a for a in sys.argv[1:] if a]
    date = next((a for a in args if a.isdigit()), None)
    with_video = "--video" in args
    render = "--render" in args

    res = save(date, with_video=with_video, render=render)
    print(f"[情绪温度计] {res['date']} 生成完成 → {res['dir']}")
    print(f"  文案: {res['文案']}")
    print(f"  图文: {res['卡片']}   （浏览器打开，点「下载全部PNG」）")
    print(f"  分镜: {res['story']}")
    if with_video and res.get("video"):
        v = res["video"]
        print(f"  视频HTML: {v['html']}")
        if v.get("rendered"):
            print(f"  视频MP4 : {v['mp4']}")
        else:
            print(f"  渲染命令: {' '.join(v['render_cmd'])}")


if __name__ == "__main__":
    main()