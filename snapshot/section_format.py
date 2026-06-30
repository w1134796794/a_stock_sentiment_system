"""Section 富格式化（前端可读性优化）。

把 LHB / 资金流向 / 复盘总结 / 周期模式胜率 / 涨停梯队 / 概念连板梯队 等结构化数据，
规整成前端可直接渲染的「干净表格」section，替代原先 `tabulate` 把整段嵌套 JSON
塞进单个单元格、可读性极差的写法。

约定
====
- 每个 formatter 接收对应数据的 **jsonable** 形式（即 `to_jsonable` 之后的纯
  dict / list），返回一个 section dict：

      {
        "name": "龙虎榜",
        "kind": "table",
        "columns": [...],          # 主表列（无 blocks 时渲染 + 左侧导航计数）
        "rows": [...],             # 主表行
        "blocks": [                # 可选：多张子表，前端逐个渲染
            {"title": "...", "columns": [...], "rows": [...]},
            ...
        ],
        "summary": "...",          # 可选：顶部一句话摘要
        "note": "...",             # 可选：补充说明（灰字）
      }

- formatter 都是**纯函数**：既供 `build_snapshot`（写快照时）用，也供
  `scripts/rebuild_snapshot_sections.py`（重建存量快照时）复用，保证两条路径
  产出完全一致。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "SECTION_FORMATTERS",
    "format_limit_up_hierarchy",
    "format_concept_hierarchy",
    "format_lhb",
    "format_moneyflow",
    "format_review",
    "format_cycle_matrix",
]


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "" or v == "--":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _round(v: Any, ndigits: int = 0) -> Any:
    """安全四舍五入；非数值原样返回。ndigits=0 时返回 int。"""
    try:
        if v is None or v == "":
            return None
        r = round(float(v), ndigits)
        return int(r) if ndigits == 0 else r
    except (TypeError, ValueError):
        return v


def _wan(v: Any) -> Any:
    """元 → 万元（取整）。"""
    try:
        return int(round(_f(v) / 1e4, 0))
    except (TypeError, ValueError):
        return None


def _pct(frac: Any, ndigits: int = 1) -> Optional[str]:
    """0.266 → '26.6%'。"""
    try:
        if frac is None or frac == "":
            return None
        return f"{float(frac) * 100:.{ndigits}f}%"
    except (TypeError, ValueError):
        return None


def _signed_pct(v: Any, ndigits: int = 2) -> Optional[str]:
    """-2.42 → '-2.42%'（值本身已是百分数）。"""
    try:
        if v is None or v == "":
            return None
        return f"{float(v):+.{ndigits}f}%"
    except (TypeError, ValueError):
        return None


def _fmt_zt_time(v: Any) -> str:
    """涨停时间 HHMMSS（93554 / 103921）→ 'HH:MM:SS'。"""
    try:
        if v is None or v == "" or v == "--":
            return ""
        s = str(int(float(v))).zfill(6)
        if len(s) != 6:
            return str(v)
        return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
    except (TypeError, ValueError):
        return str(v) if v is not None else ""


# ----------------------------------------------------------------------
# 1. 涨停梯队：合并一级/二级行业为单一「行业」列
# ----------------------------------------------------------------------
def format_limit_up_hierarchy(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = rows or []

    def _industry(r: Dict[str, Any]) -> str:
        for key in ("L1_Industry", "L2_Industry", "行业", "industry"):
            v = r.get(key)
            if v not in (None, "", "--"):
                return str(v)
        return ""

    out_rows: List[Dict[str, Any]] = []
    for r in rows:
        out_rows.append({
            "行业": _industry(r),
            "股票代码": r.get("Code") or r.get("股票代码") or "",
            "股票名称": r.get("Name") or r.get("股票名称") or "",
            "连板数": r.get("BoardHeight") if r.get("BoardHeight") is not None else r.get("连板数"),
            "涨幅%": r.get("ChangePct") if r.get("ChangePct") is not None else r.get("涨幅%"),
            "首次涨停时间": _fmt_zt_time(r.get("LimitUpTime") if r.get("LimitUpTime") is not None else r.get("首次涨停时间")),
            "最后涨停时间": _fmt_zt_time(r.get("LastLimitUpTime")),
            "炸板次数": r.get("OpenTimes") if r.get("OpenTimes") is not None else r.get("炸板次数"),
            "概念": r.get("Concept") or "",
        })

    out_rows.sort(key=lambda x: _f(x.get("连板数")), reverse=True)

    columns = ["行业", "股票代码", "股票名称", "连板数", "涨幅%",
               "首次涨停时间", "最后涨停时间", "炸板次数", "概念"]
    # 概念整列为空则隐藏
    if not any(r.get("概念") for r in out_rows):
        columns = [c for c in columns if c != "概念"]
        for r in out_rows:
            r.pop("概念", None)
    return {"name": "涨停梯队", "kind": "table", "columns": columns, "rows": out_rows}


# ----------------------------------------------------------------------
# 2. 概念连板梯队
# ----------------------------------------------------------------------
def format_concept_hierarchy(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    items = list(data.values()) if isinstance(data, dict) else list(data)

    def _total(h: Dict[str, Any]) -> float:
        return _f(h.get("total_limit_up"))

    items.sort(key=_total, reverse=True)

    rows: List[Dict[str, Any]] = []
    for h in items:
        if not isinstance(h, dict):
            continue
        dist = h.get("board_distribution") or {}
        parts = []
        for board in sorted(dist.keys(), key=lambda k: _f(k), reverse=True):
            parts.append(f"{board}板{dist[board]}家")
        leader = h.get("leader_stock") or {}
        leader_str = f"{leader.get('name', '')}({leader.get('code', '')})" if leader else "-"
        rows.append({
            "概念名称": h.get("concept_name", ""),
            "涨停总数": h.get("total_limit_up", 0),
            "最高连板": h.get("max_board_count", 0),
            "梯队分布": ", ".join(parts),
            "龙头股": leader_str,
            "板块代码": h.get("ts_code", ""),
        })

    columns = ["概念名称", "涨停总数", "最高连板", "梯队分布", "龙头股", "板块代码"]
    return {"name": "概念连板梯队", "kind": "table", "columns": columns, "rows": rows}


# ----------------------------------------------------------------------
# 3. 龙虎榜（个股席位画像 + 板块共识度，两张子表）
# ----------------------------------------------------------------------
def _seat_desc(seat: Dict[str, Any]) -> str:
    net = _f(seat.get("net_amount"))
    side = "买" if net > 0 else "卖"
    return f"{seat.get('hm_name', '')}{side}{net / 1e4:+.0f}万"


def _seats_summary(seats: List[Dict[str, Any]], top: int = 4) -> str:
    ordered = sorted(seats or [], key=lambda s: -abs(_f(s.get("net_amount"))))
    return " / ".join(_seat_desc(s) for s in ordered[:top]) if ordered else "--"


def _consensus_level(distinct_hm: float, net_total: float) -> str:
    if distinct_hm >= 3 and net_total > 0:
        return "高"
    if distinct_hm >= 2:
        return "中"
    return "低"


def format_lhb(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    trade_date = data.get("trade_date", "")
    available = bool(data.get("available"))
    stock_profiles = data.get("stock_profiles") or {}
    sector_profiles = data.get("sector_profiles") or {}
    if not available or not stock_profiles:
        return {
            "name": "龙虎榜", "kind": "table",
            "columns": ["提示"],
            "rows": [{"提示": "今日无游资明细数据（账户积分不足 / 当日无龙虎榜 / 已降级跳过）"}],
        }

    # ---- 子表一：上榜个股（按实际净买入排序）----
    stock_rows: List[Dict[str, Any]] = []
    for prof in stock_profiles.values():
        seats = prof.get("seats") or []
        total_net = sum(_f(s.get("net_amount")) for s in seats)
        # —— 席位结构 ——
        buy_seats = sum(1 for s in seats if _f(s.get("net_amount")) > 0)
        sell_seats = sum(1 for s in seats if _f(s.get("net_amount")) < 0)
        buy_amt = sum(_f(s.get("buy_amount")) for s in seats)
        sell_amt = sum(_f(s.get("sell_amount")) for s in seats)
        tot_amt = buy_amt + sell_amt
        # —— 机构专用席位方向 ——
        inst_seats = [s for s in seats if "机构专用" in str(s.get("hm_name", ""))]
        inst_net = sum(_f(s.get("net_amount")) for s in inst_seats)
        inst = "" if not inst_seats else ("机构买" if inst_net > 0 else "机构卖")
        stock_rows.append({
            "股票代码": prof.get("ts_code", ""),
            "股票名称": prof.get("ts_name", ""),
            "净买入(万)": _wan(total_net),
            "净向": "净买" if total_net > 0 else ("净卖" if total_net < 0 else "持平"),
            "买席": buy_seats,
            "卖席": sell_seats,
            "买盘占比": round(buy_amt / tot_amt * 100, 1) if tot_amt > 0 else 0,
            "机构": inst,
            "席位数": len(seats),
            "主要席位": _seats_summary(seats, top=4),
            "_net": total_net,
        })
    stock_rows.sort(key=lambda x: x.get("_net", 0.0), reverse=True)
    stock_rows = stock_rows[:40]
    for r in stock_rows:
        r.pop("_net", None)

    stock_cols = ["股票代码", "股票名称", "净向", "买席", "卖席",
                  "买盘占比", "机构", "席位数", "主要席位", "净买入(万)"]
    blocks = [{
        "title": f"上榜个股（按净买入排序 · {trade_date}）",
        "columns": stock_cols, "rows": stock_rows,
    }]

    # ---- 子表二：板块游资共识度 ----
    if sector_profiles:
        sec_rows: List[Dict[str, Any]] = []
        for sp in sector_profiles.values():
            distinct = _f(sp.get("distinct_hm_count"))
            net_total = _f(sp.get("net_buy_total"))
            sec_rows.append({
                "板块": sp.get("sector", ""),
                "上榜票数": sp.get("stock_count", 0),
                "涉及游资数": sp.get("distinct_hm_count", 0),
                "合计净买入(万)": _wan(net_total),
                "共识度": _consensus_level(distinct, net_total),
                "_d": distinct, "_n": net_total,
            })
        sec_rows.sort(key=lambda x: (-x.get("_d", 0), -x.get("_n", 0)))
        for r in sec_rows:
            r.pop("_d", None)
            r.pop("_n", None)
        blocks.append({
            "title": "板块游资共识度（多游资同买 = 主线确认）",
            "columns": ["板块", "上榜票数", "涉及游资数", "合计净买入(万)", "共识度"],
            "rows": sec_rows,
        })

    return {
        "name": "龙虎榜", "kind": "table",
        "columns": stock_cols, "rows": stock_rows, "blocks": blocks,
        "note": "游资名称、席位和买卖金额来自已落库的龙虎榜明细，不使用人工信誉评分。",
    }


# ----------------------------------------------------------------------
# 4. 资金流向
# ----------------------------------------------------------------------
def _flow_structure(main_net: float, retail_net: float) -> str:
    """主力/散户资金的相对结构信号（不依赖金额绝对值）。"""
    if main_net > 0 and retail_net <= 0:
        return "主力吸筹·散户离场"
    if main_net > 0 and retail_net > 0:
        return "主力散户齐买"
    if main_net < 0 and retail_net > 0:
        return "主力派发·散户接盘"
    if main_net < 0 and retail_net < 0:
        return "主力散户齐卖"
    return "—"


def format_moneyflow(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    rows: List[Dict[str, Any]] = []
    for code, info in data.items():
        info = info or {}
        main_net = _f(info.get("main_net"))
        retail_net = _f(info.get("retail_net"))
        rows.append({
            "股票代码": code,
            "股票名称": info.get("name", ""),
            "方向": info.get("direction", ""),
            "主力净占比": info.get("main_net_ratio"),   # %（采集层新增；旧快照为空）
            "买盘占比": info.get("buy_ratio"),            # %（采集层新增；旧快照为空）
            "结构": _flow_structure(main_net, retail_net),
            "主力净流入(万)": _round(info.get("main_net"), 0),
            "散户净流入(万)": _round(info.get("retail_net"), 0),
            "_m": main_net,
        })
    rows.sort(key=lambda x: x.get("_m", 0.0), reverse=True)
    for r in rows:
        r.pop("_m", None)

    columns = ["股票代码", "股票名称", "方向", "主力净占比", "买盘占比", "结构",
               "主力净流入(万)", "散户净流入(万)"]
    return {
        "name": "资金流向", "kind": "table", "columns": columns, "rows": rows,
        "note": "主力净占比 = 主力净流入 / 当日总成交额；买盘占比 = 总买额 / 总成交额；结构看主力与散户资金的相对方向。",
    }


# ----------------------------------------------------------------------
# 5. 复盘总结
# ----------------------------------------------------------------------
_STATS_SOURCE_LABEL = {
    "today": "今日 T+1",
    "history": "历史回溯",
    "pending": "待确认（T+1 未到）",
}


def format_review(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    blocks: List[Dict[str, Any]] = []

    # 子表一：模式表现统计
    pattern_stats = data.get("pattern_stats") or {}
    ps_rows: List[Dict[str, Any]] = []
    for name, s in pattern_stats.items():
        s = s or {}
        ps_rows.append({
            "模式": name,
            "信号数": s.get("total_signals", 0),
            "盈利数": s.get("profitable_signals", 0),
            "胜率": _pct(s.get("win_rate")),
            "平均收益": _signed_pct(s.get("avg_return")),
            "最大收益": _signed_pct(s.get("max_return")),
            "最小收益": _signed_pct(s.get("min_return")),
            "平均置信度": _round(s.get("avg_confidence"), 2),
            "_w": _f(s.get("win_rate")),
        })
    ps_rows.sort(key=lambda x: x.get("_w", 0.0), reverse=True)
    for r in ps_rows:
        r.pop("_w", None)
    if ps_rows:
        blocks.append({
            "title": "模式表现统计",
            "columns": ["模式", "信号数", "盈利数", "胜率", "平均收益",
                        "最大收益", "最小收益", "平均置信度"],
            "rows": ps_rows,
        })

    # 子表二：情绪周期趋势
    trends = data.get("emotion_trends") or []
    tr_rows = [{
        "日期": t.get("date", ""),
        "情绪周期": t.get("cycle_name", ""),
        "涨停家数": t.get("limit_up_count", 0),
        "炸板率%": _round(t.get("broken_rate"), 1),
        "溢价率%": _round(t.get("premium_rate"), 1),
        "最高板": t.get("max_board_height", 0),
    } for t in trends if isinstance(t, dict)]
    if tr_rows:
        blocks.append({
            "title": "情绪周期趋势",
            "columns": ["日期", "情绪周期", "涨停家数", "炸板率%", "溢价率%", "最高板"],
            "rows": tr_rows,
        })

    # 子表三：参数建议
    sens = data.get("sensitivity_analysis") or {}
    suggestions = sens.get("suggestions") or []
    sg_rows = [{
        "方向": s.get("direction", ""),
        "模式": s.get("pattern", "全局"),
        "原因": s.get("reason", ""),
        "建议动作": s.get("action", ""),
    } for s in suggestions if isinstance(s, dict)]
    if sg_rows:
        blocks.append({
            "title": "参数敏感度建议",
            "columns": ["方向", "模式", "原因", "建议动作"],
            "rows": sg_rows,
        })

    # 顶部摘要 + 来源说明
    src = data.get("stats_source", "")
    src_label = _STATS_SOURCE_LABEL.get(src, src)
    window = data.get("stats_window") or ["", ""]
    note_parts = [f"统计来源：{src_label}"] if src_label else []
    if src == "history" and window and window[0]:
        note_parts.append(f"窗口 {window[0]}~{window[1]}")
    if data.get("pending_signal_count"):
        note_parts.append(f"待确认信号 {data.get('pending_signal_count')} 个")

    main_cols = blocks[0]["columns"] if blocks else ["提示"]
    main_rows = blocks[0]["rows"] if blocks else [{"提示": "暂无复盘数据"}]
    return {
        "name": "复盘总结", "kind": "table",
        "columns": main_cols, "rows": main_rows, "blocks": blocks,
        "summary": data.get("review_summary", ""),
        "note": " · ".join(note_parts),
    }


# ----------------------------------------------------------------------
# 6. 周期模式胜率矩阵（胜率视图 + 平均收益视图）
# ----------------------------------------------------------------------
def _matrix_block(cells: Dict[str, Any], cycles: List[str], patterns: List[str],
                  title: str, value: str, min_n: int = 3) -> Dict[str, Any]:
    first_col = "情绪周期 \\ 模式"
    columns = [first_col] + list(patterns)
    rows: List[Dict[str, Any]] = []
    for cyc in cycles:
        row: Dict[str, Any] = {first_col: cyc}
        for pat in patterns:
            key = str((cyc, pat))  # 与 to_jsonable 后的 str(tuple) 键一致
            cell = cells.get(key)
            if not cell:
                row[pat] = "—"
                continue
            n = int(_f(cell.get("n")))
            if n < min_n:
                row[pat] = f"N/A(n={n})"
            elif value == "win_rate":
                row[pat] = f"{_f(cell.get('win_rate')) * 100:.0f}%(n={n})"
            else:
                row[pat] = f"{_f(cell.get('avg_return')):+.2f}%(n={n})"
        rows.append(row)
    return {"title": title, "columns": columns, "rows": rows}


def format_cycle_matrix(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    cells = data.get("cells") or {}
    cycles = data.get("cycles") or []
    patterns = data.get("patterns") or []

    if not cells or not cycles or not patterns:
        return {
            "name": "周期模式胜率", "kind": "table",
            "columns": ["提示"],
            "rows": [{"提示": "周期 × 模式矩阵尚无足够数据（需回填近 30 天 factor_results）"}],
        }

    win_block = _matrix_block(cells, cycles, patterns, "胜率视图（n=有效样本数）", "win_rate")
    ret_block = _matrix_block(cells, cycles, patterns, "平均 T+1 收益视图（n=有效样本数）", "avg_return")

    window = data.get("sample_window") or ["", ""]
    total = data.get("sample_count_total", 0)
    return {
        "name": "周期模式胜率", "kind": "table",
        "columns": win_block["columns"], "rows": win_block["rows"],
        "blocks": [win_block, ret_block],
        "note": (f"样本窗口 {window[0]}~{window[1]} · 共 {total} 个历史信号；"
                 f"样本数 < 3 显示 N/A，避免小样本误导。历史统计不代表未来。"),
    }


# 按 section 名分发：build_snapshot / 重建脚本共用
SECTION_FORMATTERS = {
    "涨停梯队": format_limit_up_hierarchy,
    "概念连板梯队": format_concept_hierarchy,
    "龙虎榜": format_lhb,
    "资金流向": format_moneyflow,
    "复盘总结": format_review,
    "周期模式胜率": format_cycle_matrix,
}
