"""
复盘短视频「分镜脚本」生成（P0）。

把每日结构化快照 (snapshot dict) 压成一份面向 9:16 短视频的 storyboard JSON：

    hook → 情绪温度计 → 今日主线 → 龙头梯队 → 资金动向 → 策略选股 → 明日策略 → 收尾/免责

每一幕(scene)自带：
- title    场景大标题
- subtitle 副标题（可选）
- narration 口播文案（供 TTS 配音）
- caption  屏幕字幕（更短，给画面用）
- stats    要高亮放大的数字 [{label, value, unit, tone}]
- list     榜单 [{rank, name, code, sub, value, tag}]
- duration 时长（秒）
- accent   主色（由情绪周期决定）

设计目标：
- 纯标准库、确定性（同一快照 → 同一脚本），不依赖大模型，保证收盘后全自动出片稳定可复现；
- 任何字段缺失时优雅跳过该幕/该字段，绝不抛错；
- 输出结构对 HyperFrames 友好：scenes 数组可直接驱动逐幕时间轴。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "recap.v1"

# 9:16 竖屏短视频默认规格
DEFAULT_FORMAT = {
    "ratio": "9:16",
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "target_seconds": 80,
}

DISCLAIMER = "本视频为数据复盘，仅供学习交流，不构成任何投资建议。市场有风险，入市需谨慎。"

# 情绪周期 → 主色（上镜的深色主题配色）
CYCLE_ACCENT = {
    "高潮期": "rose",
    "上升期": "emerald",
    "震荡期": "sky",
    "退潮期": "amber",
    "冰点期": "slate",
}

# 情绪周期 → 开场钩子文案（3 秒留人）
HOOK_BY_CYCLE = {
    "高潮期": "高潮期！情绪烧到顶了吗——还能上车吗？",
    "上升期": "上升期，赚钱效应回来了，今天的主线是谁？",
    "震荡期": "震荡期反复磨人，今天的钱到底去哪了？",
    "退潮期": "退潮期，资金在撤——今天谁还在硬扛？",
    "冰点期": "冰点期，遍地黄金还是接飞刀？",
}


# ---------------------------------------------------------------- 小工具
def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _p1(v: Any) -> Optional[float]:
    """百分数保留 1 位小数（broken_rate=57.53 → 57.5）。"""
    n = _num(v)
    return round(n, 1) if n is not None else None


def _section_by_name(sections: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """按 name 取首个非 signals section（数据浏览类 section）。"""
    for s in sections or []:
        if s.get("name") == name and s.get("kind") != "signals":
            return s
    return None


def _rows(section: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return (section or {}).get("rows") or []


def _fmt_date(date: str) -> str:
    s = str(date or "")
    return f"{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


# ---------------------------------------------------------------- 各幕
def _scene_hook(date: str, cycle: str, accent: str) -> Dict[str, Any]:
    hook = HOOK_BY_CYCLE.get(cycle, f"{cycle or 'A股'} · 今日情绪全复盘")
    return {
        "key": "hook",
        "title": hook,
        "subtitle": f"{_fmt_date(date)} · A股情绪复盘",
        "narration": f"{cycle or '今天'}，先看一眼今天的市场情绪。",
        "caption": hook,
        "stats": [],
        "list": [],
        "duration": 4,
        "accent": accent,
    }


def _scene_emotion(market: Dict[str, Any], accent: str) -> Optional[Dict[str, Any]]:
    metrics = market.get("metrics") or {}
    if not metrics:
        return None
    limit_up = _int(metrics.get("limit_up_count"))
    limit_down = _int(metrics.get("limit_down_count"))
    max_board = _int(metrics.get("max_board_height"))
    broken = _p1(metrics.get("broken_rate"))
    win_rate = _p1(metrics.get("win_rate"))
    avg_profit = _p1(metrics.get("avg_profit"))

    stats: List[Dict[str, Any]] = []
    if limit_up is not None:
        stats.append({"label": "涨停", "value": limit_up, "unit": "家", "tone": "up"})
    if limit_down is not None:
        stats.append({"label": "跌停", "value": limit_down, "unit": "家", "tone": "down"})
    if max_board is not None:
        stats.append({"label": "最高板", "value": max_board, "unit": "板", "tone": "up"})
    if broken is not None:
        stats.append({"label": "炸板率", "value": broken, "unit": "%", "tone": "down"})

    # 指数（上证）作为大盘风向
    env = market.get("env") or {}
    sh = env.get("sh_index") or {}
    sh_chg = _p1(sh.get("change_pct"))
    if sh_chg is not None:
        stats.append({"label": "上证", "value": sh_chg, "unit": "%",
                      "tone": "up" if sh_chg >= 0 else "down"})

    bits: List[str] = []
    if limit_up is not None:
        bits.append(f"今日{limit_up}只涨停")
    if limit_down is not None:
        bits.append(f"{limit_down}只跌停")
    if max_board is not None:
        bits.append(f"最高{max_board}板")
    if broken is not None:
        bits.append(f"炸板率{broken}%")
    tail = ""
    if avg_profit is not None and win_rate is not None:
        tail = f"，昨日涨停股今日平均{avg_profit:+.1f}%、打板胜率{win_rate}%"
    narration = "，".join(bits) + tail + "。"

    return {
        "key": "emotion",
        "title": "情绪温度计",
        "subtitle": market.get("cycle_name") or "",
        "narration": narration,
        "caption": "今日情绪一览",
        "stats": stats,
        "list": [],
        "duration": 10,
        "accent": accent,
    }


def _scene_mainline(sections: List[Dict[str, Any]], accent: str) -> Optional[Dict[str, Any]]:
    sec = _section_by_name(sections, "概念连板梯队")
    rows = sorted(_rows(sec), key=lambda r: _num(r.get("涨停总数")) or 0, reverse=True)
    if not rows:
        return None
    top = rows[:4]
    items: List[Dict[str, Any]] = []
    for i, r in enumerate(top, 1):
        cnt = _int(r.get("涨停总数"))
        hb = _int(r.get("最高连板"))
        items.append({
            "rank": i,
            "name": r.get("概念名称") or "--",
            "value": cnt,
            "unit": "家涨停",
            "sub": f"最高{hb}板 · 龙头{r.get('龙头股') or '--'}" if hb is not None else (r.get("龙头股") or ""),
            "tag": hb,
        })
    lead = top[0]
    n = _int(lead.get("涨停总数"))
    narration = f"今天的主线是{lead.get('概念名称','')}，{n}家涨停，龙头{lead.get('龙头股','')}。"
    if len(top) > 1:
        others = "、".join(r.get("概念名称", "") for r in top[1:3])
        narration += f"其次是{others}。"
    return {
        "key": "mainline",
        "title": "今日主线",
        "subtitle": "板块连板梯队",
        "narration": narration,
        "caption": "今天钱往哪儿走",
        "stats": [],
        "list": items,
        "duration": 9,
        "accent": accent,
    }


def _scene_dragon(sections: List[Dict[str, Any]], accent: str) -> Optional[Dict[str, Any]]:
    sec = _section_by_name(sections, "涨停梯队")
    rows = sorted(_rows(sec), key=lambda r: _num(r.get("连板数")) or 0, reverse=True)
    if not rows:
        return None
    top = rows[:5]
    items: List[Dict[str, Any]] = []
    for i, r in enumerate(top, 1):
        boards = _int(r.get("连板数"))
        items.append({
            "rank": i,
            "name": r.get("股票名称") or "--",
            "code": r.get("股票代码") or "",
            "value": boards,
            "unit": "板",
            "sub": r.get("行业") or "",
            "tag": boards,
        })
    lead = top[0]
    nb = _int(lead.get("连板数"))
    narration = f"空间高度看{lead.get('股票名称','')}，{nb}连板领涨。"
    if len(top) > 1 and _int(top[1].get("连板数")):
        narration += f"{top[1].get('股票名称','')}{_int(top[1].get('连板数'))}板紧随其后。"
    return {
        "key": "dragon",
        "title": "龙头梯队",
        "subtitle": "今日连板高度",
        "narration": narration,
        "caption": "谁是今天的龙头",
        "stats": [],
        "list": items,
        "duration": 9,
        "accent": accent,
    }


def _credit_tone(credit: str) -> str:
    """买方信誉 → 色调：白买入=利好确认，黑买入=风险，余者中性。"""
    s = str(credit or "")
    if "白" in s:
        return "up"
    if "黑" in s or "⚠" in s:
        return "down"
    return "neutral"


def _scene_capital(sections: List[Dict[str, Any]], accent: str) -> Optional[Dict[str, Any]]:
    sec = _section_by_name(sections, "龙虎榜")
    # 只取「真实净流入(>0)」的前 3，最直观；买方信誉作风险标签保留系统的判别力
    rows = [r for r in _rows(sec) if (_num(r.get("净买入(万)")) or 0) > 0]
    rows = sorted(rows, key=lambda r: _num(r.get("净买入(万)")) or 0, reverse=True)[:3]
    if not rows:
        return None
    items: List[Dict[str, Any]] = []
    for i, r in enumerate(rows, 1):
        net = _int(r.get("净买入(万)"))
        credit = r.get("买方信誉") or ""
        items.append({
            "rank": i,
            "name": r.get("股票名称") or "--",
            "code": r.get("股票代码") or "",
            "value": net,
            "unit": "万",
            "sub": credit,
            "tag": credit,
            "tone": _credit_tone(credit),
        })
    lead = rows[0]
    narration = f"龙虎榜今日净买入居前的是{lead.get('股票名称','')}，约{_int(lead.get('净买入(万)'))}万。"
    if len(rows) > 1:
        narration += f"其次{rows[1].get('股票名称','')}。"
    narration += "买方信誉标黑的为风险信号，注意甄别。"
    return {
        "key": "capital",
        "title": "资金动向",
        "subtitle": "龙虎榜·净买入前三",
        "narration": narration,
        "caption": "龙虎榜谁被买最多",
        "stats": [],
        "list": items,
        "duration": 8,
        "accent": accent,
    }


_POS_LABEL = {"heavy": "重仓", "medium": "中等", "light": "轻仓", "watch": "观察"}

# 策略一句话释义（上镜字幕用）；未收录的走通用文案
STRATEGY_DESC = {
    "弱转强": "弱势股放量转强，低吸博反包",
    "二板定龙": "首板次日抢二板，锁定梯队龙头",
    "首板突破": "首次涨停突破关键位，趋势启动",
    "龙二波": "龙头分歧调整后的第二波机会",
    "接力": "强势板块内高位接力",
    "中军": "板块中军跟随补涨",
    "低吸": "回调企稳处低吸",
}


def _strategy_pick_item(rank: int, r: Dict[str, Any]) -> Dict[str, Any]:
    conf = _num(r.get("confidence"))
    conf_pct = round(conf * 100) if conf is not None else None
    return {
        "rank": rank,
        "name": r.get("stock_name") or r.get("股票名称") or "--",
        "code": r.get("stock_code") or r.get("股票代码") or "",
        "value": conf_pct,
        "unit": "%",
        "sub": r.get("l2_industry") or "",
        "tag": r.get("pattern_type") or r.get("模式类型") or "",
        # 置信度高 / 重仓信号用红色强调（A股惯例：红=强）
        "tone": "up" if (conf is not None and conf >= 0.8) or r.get("position_size") == "heavy" else "neutral",
    }


def _build_strategy_scenes(snapshot: Dict[str, Any], accent: str) -> List[Dict[str, Any]]:
    """策略选股：1 幕总览（各策略选股数）+ 每个策略 1 幕（该策略选出的个股）。"""
    patterns = snapshot.get("patterns") or {}
    groups: List[tuple] = []   # (策略名, 按置信度降序的选股)
    for name, blk in patterns.items():
        rows = [r for r in ((blk or {}).get("rows") or []) if isinstance(r, dict)]
        rows.sort(key=lambda r: _num(r.get("confidence")) or 0, reverse=True)
        if rows:
            groups.append((str(name), rows))
    if not groups:
        return []

    groups.sort(key=lambda g: len(g[1]), reverse=True)
    total = sum(len(rows) for _, rows in groups)
    scenes: List[Dict[str, Any]] = []

    # —— 总览幕：各策略选股数（stat 卡）——
    stats = [{"label": name, "value": len(rows), "unit": "只", "tone": "neutral"}
             for name, rows in groups[:4]]
    parts = "、".join(f"{name}{len(rows)}只" for name, rows in groups)
    scenes.append({
        "key": "strategy",
        "title": "策略选股",
        "subtitle": f"今日 {len(groups)} 大策略 · 共 {total} 只",
        "narration": f"今日策略选股共{total}只：{parts}。下面逐个策略来看。",
        "caption": "今日各策略选股一览",
        "stats": stats,
        "list": [],
        "duration": 7,
        "accent": accent,
    })

    # —— 每个策略一幕：该策略选出的个股（按置信度 TOP4）——
    for i, (name, rows) in enumerate(groups, 1):
        items = [_strategy_pick_item(j, r) for j, r in enumerate(rows[:4], 1)]
        lead = rows[0]
        lead_conf = round((_num(lead.get("confidence")) or 0) * 100)
        lead_name = lead.get("stock_name") or lead.get("股票名称", "")
        cnt = len(rows)
        shown = min(4, cnt)
        more = f"，列出前{shown}只" if cnt > shown else ""
        scenes.append({
            "key": f"strategy_{i}",
            "title": name,
            "subtitle": f"策略选股 · {cnt}只",
            "narration": (f"{name}策略今日选出{cnt}只{more}，{lead_name}居首，"
                          f"置信度约{lead_conf}%。仅为模型信号，盘前竞价再确认。"),
            "caption": STRATEGY_DESC.get(name, "策略信号选股"),
            "stats": [],
            "list": items,
            "duration": 8,
            "accent": accent,
        })
    return scenes


def _top_winrate_pattern(cycle: str) -> Optional[Dict[str, Any]]:
    """当前情绪周期下历史胜率最高的模式（缺失则 None）。"""
    try:
        from config.settings import WINRATE_PATH
        from kb.winrate import load_matrix, winrate_for_cycle
    except Exception:  # noqa: BLE001
        return None
    try:
        cells = winrate_for_cycle(load_matrix(WINRATE_PATH), cycle)
    except Exception:  # noqa: BLE001
        return None
    if not cells:
        return None
    c = cells[0]
    return {"pattern": c.get("pattern"), "win_rate": round((c.get("win_rate") or 0) * 100),
            "n": c.get("n")}


def _scene_tomorrow(snapshot: Dict[str, Any], market: Dict[str, Any], accent: str) -> Dict[str, Any]:
    cycle = market.get("cycle_name") or ""
    position = market.get("position") or "--"
    strategy = market.get("strategy") or ""
    plans = (snapshot.get("trade_plans") or {}).get("rows") or []
    rg = snapshot.get("risk_gate") or {}

    stats: List[Dict[str, Any]] = [
        {"label": "建议仓位", "value": position, "unit": "", "tone": "neutral"},
        {"label": "精选计划", "value": len(plans), "unit": "个", "tone": "up"},
    ]
    edge = _top_winrate_pattern(cycle)
    if edge and edge.get("pattern"):
        stats.append({"label": "占优模式", "value": edge["pattern"], "unit": "", "tone": "up"})

    narration = f"明日策略：{strategy or '以稳为主'}，建议仓位{position}。"
    if edge and edge.get("pattern"):
        narration += f"当前{cycle}周期，历史胜率最高的是{edge['pattern']}（约{edge['win_rate']}%）。"
    if plans:
        narration += f"今日精选{len(plans)}个计划，"
    if rg:
        narration += f"风控通过{rg.get('passed', 0)}、降级{rg.get('downgraded', 0)}、拒绝{rg.get('rejected', 0)}。"
    narration += "具体仓位以盘前竞价确认为准。"

    return {
        "key": "tomorrow",
        "title": "明日策略",
        "subtitle": cycle,
        "narration": narration,
        "caption": "明天怎么办",
        "stats": stats,
        "list": [],
        "duration": 9,
        "accent": accent,
    }


def _scene_cta(accent: str) -> Dict[str, Any]:
    return {
        "key": "cta",
        "title": "今日复盘结束",
        "subtitle": "数据说话 · 非荐股",
        "narration": "以上就是今天的A股情绪与策略复盘，所有结论均来自公开数据，请独立判断、控制仓位。",
        "caption": DISCLAIMER,
        "stats": [],
        "list": [],
        "duration": 4,
        "accent": accent,
    }


# ---------------------------------------------------------------- 主入口
def build_storyboard(snapshot: Dict[str, Any],
                     fmt: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """snapshot dict → 9:16 短视频 storyboard dict。"""
    meta = snapshot.get("meta") or {}
    date = str(meta.get("date") or "")
    market = snapshot.get("market") or {}
    sections = snapshot.get("sections") or []
    cycle = market.get("cycle_name") or ""
    accent = CYCLE_ACCENT.get(cycle, "emerald")

    scenes = [
        _scene_hook(date, cycle, accent),
        _scene_emotion(market, accent),
        _scene_mainline(sections, accent),
        _scene_dragon(sections, accent),
        _scene_capital(sections, accent),
    ]
    # 策略选股：总览 + 每个策略一幕（逐策略展示选出的个股）
    scenes += _build_strategy_scenes(snapshot, accent)
    scenes += [
        _scene_tomorrow(snapshot, market, accent),
        _scene_cta(accent),
    ]
    scenes = [s for s in scenes if s]

    # 逐幕时间轴：start 累加，便于 HyperFrames 直接铺 data-start/data-duration
    t = 0.0
    for s in scenes:
        s["start"] = round(t, 2)
        t += float(s.get("duration") or 0)

    hook = HOOK_BY_CYCLE.get(cycle, f"{cycle or 'A股'} · 今日情绪全复盘")
    return {
        "schema": SCHEMA,
        "date": date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "format": dict(fmt or DEFAULT_FORMAT),
        "cycle": cycle,
        "accent": accent,
        "title": f"{cycle or 'A股'}复盘 | {_fmt_date(date)}",
        "hook": hook,
        "disclaimer": DISCLAIMER,
        "scenes": scenes,
        "total_duration": round(t, 2),
    }


# ---------------------------------------------------------------- 落盘 / 读取
def _recap_dir() -> Path:
    from config.settings import WEB_DATA_DIR
    try:
        from config.settings import RECAP_DIR  # type: ignore
        return Path(RECAP_DIR)
    except Exception:  # noqa: BLE001
        return Path(WEB_DATA_DIR) / "recaps"


def build_and_save(date: Optional[str] = None,
                   snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构建指定日期的 storyboard 并落盘到 ``webdata/recaps/{date}.json``。

    传入 snapshot 时直接用之；否则按 date（缺省取最新）从快照目录加载。
    """
    from config.settings import SNAPSHOT_DIR
    from snapshot.reader import SnapshotReader

    reader = SnapshotReader(SNAPSHOT_DIR)
    if snapshot is None:
        date = date or reader.latest()
        if not date:
            raise FileNotFoundError("没有可用的快照")
        snapshot = reader.load(date)
        if snapshot is None:
            raise FileNotFoundError(f"找不到 {date} 的快照")
    else:
        date = date or str((snapshot.get("meta") or {}).get("date") or "")

    story = build_storyboard(snapshot)
    out_dir = _recap_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"
    out_path.write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest.txt").write_text(str(date), encoding="utf-8")
    return story


def load_recap(date: str) -> Optional[Dict[str, Any]]:
    """读取已落盘的 storyboard；不存在返回 None。"""
    path = _recap_dir() / f"{date}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
