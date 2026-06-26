"""
每日 AI 解读：把当天结构化快照浓缩成一段「今日复盘 + 明日策略」叙事，落 SQLite 缓存。

强约束：只能基于传入的真实字段叙述，不得编造数字/标的。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from kb.llm_client import get_llm_client

SYSTEM_BRIEF = (
    "你是A股短线情绪交易的资深助手。请仅依据用户提供的【当日数据】用中文写一段简洁解读，"
    "禁止编造未给出的数字或标的。结构：①今日情绪与主线（1-2句）②明日策略与重点计划"
    "（结合给出的计划、风控判定与当前市场状态判断是否值得执行）"
    "③风险提示。总字数控制在 240 字以内，不用 markdown 标题。"
)


def _init_cache(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS daily_brief(
                 date TEXT PRIMARY KEY, model TEXT, content TEXT,
                 created_at TEXT DEFAULT (datetime('now')))"""
        )


def _get_cached(db_path: Path, date: str) -> Optional[str]:
    if not Path(db_path).exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT content FROM daily_brief WHERE date=?", (date,)).fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _save_cache(db_path: Path, date: str, model: str, content: str) -> None:
    _init_cache(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO daily_brief(date, model, content) VALUES(?,?,?)
               ON CONFLICT(date) DO UPDATE SET content=excluded.content,
                 model=excluded.model, created_at=datetime('now')""",
            (date, model, content),
        )


def _winrate_lines(cycle: str) -> List[str]:
    """保留兼容接口：旧策略胜率矩阵已移除，暂无历史胜率参考。"""
    try:
        from config.settings import WINRATE_PATH
        from kb.winrate import load_matrix, winrate_for_cycle
    except Exception:
        return []
    data = load_matrix(WINRATE_PATH)
    cells = winrate_for_cycle(data, cycle)
    if not cells:
        return []
    parts = [f"{c['pattern']} {c['win_rate']*100:.0f}%(样本{c['n']},均收益{c['avg_return']:+.1f}%)"
             for c in cells[:5]]
    return [f"历史胜率参考（{cycle}周期，T+1）：" + "；".join(parts)]


def _facts_from_snapshot(snapshot: Dict[str, Any]) -> str:
    market = snapshot.get("market", {}) or {}
    cycle = market.get("cycle_name") or ""
    lines: List[str] = [
        f"日期：{(snapshot.get('meta') or {}).get('date', '')}",
        f"情绪周期：{cycle or '未知'}",
        f"建议仓位：{market.get('position') or '未知'}；策略基调：{market.get('strategy') or '未知'}",
    ]
    lines += _winrate_lines(cycle)
    metrics = market.get("metrics") or {}
    if isinstance(metrics, dict) and metrics:
        lines.append("关键指标：" + "，".join(f"{k}={v}" for k, v in list(metrics.items())[:6]))

    rg = snapshot.get("risk_gate") or {}
    if rg:
        lines.append(
            f"风控闸门：通过{rg.get('passed', 0)}/降级{rg.get('downgraded', 0)}/拒绝{rg.get('rejected', 0)}；"
            f"{rg.get('summary', '')}")

    plans = (snapshot.get("trade_plans") or {}).get("rows", []) or []
    if plans:
        lines.append(f"交易计划（共{len(plans)}条，列前几条）：")
        for p in plans[:6]:
            lines.append(
                f"- {p.get('股票名称','')}({p.get('股票代码','')}) {p.get('模式类型','')} "
                f"评分{p.get('综合评分','--')} 仓位{p.get('建议仓位') or p.get('仓位等级','--')} "
                f"入场{p.get('入场区间','--')} 止损{p.get('止损','--')} 止盈{p.get('止盈','--')} "
                f"风控{p.get('风控动作','--')}；依据：{p.get('仓位依据','')}；风险：{p.get('风险提示','')}")
    else:
        lines.append("交易计划：今日无（信号为0或大盘风险过高）。")
    return "\n".join(lines)


def generate_brief(snapshot: Dict[str, Any], kb_db_path: Path, force: bool = False) -> Dict[str, Any]:
    date = str((snapshot.get("meta") or {}).get("date") or "")
    client = get_llm_client()

    if not force:
        cached = _get_cached(kb_db_path, date)
        if cached:
            return {"configured": True, "cached": True, "content": cached, "date": date}

    if not client.is_configured:
        return {
            "configured": False, "cached": False, "date": date,
            "content": "未配置大模型 API key。在 .env 设置 LLM_API_KEY（或 DEEPSEEK_API_KEY）后，"
                       "这里会自动生成「今日复盘 + 明日策略」解读。",
        }

    facts = _facts_from_snapshot(snapshot)
    messages = [
        {"role": "system", "content": SYSTEM_BRIEF},
        {"role": "user", "content": f"【当日数据】\n{facts}\n\n请按要求输出解读。"},
    ]
    content = client.chat(messages, temperature=0.5)
    if content and not content.startswith("（"):  # 非错误占位才缓存
        _save_cache(kb_db_path, date, client.model, content)
        return {"configured": True, "cached": False, "content": content, "date": date}
    return {"configured": True, "cached": False, "content": content, "date": date}
