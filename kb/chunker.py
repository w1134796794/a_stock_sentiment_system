"""
把每日快照拆成知识块。

块设计（粒度服务于"问为什么 / 查历史"）：
- market：当日情绪周期 + 仓位 + 策略 + 关键指标（一天一块）
- plan：每条交易计划的完整理由（含评分依据、入场、止损止盈、次日预期、风控判定）
- signal：四大模式的每条信号（轻量，附置信度与描述）
- review：盘后复盘总结（计划 vs 兑现）

块 id 用 ``{date}:{kind}:{key}`` 保证可重复灌库时幂等覆盖。
"""
from __future__ import annotations

from typing import Any, Dict, List

from kb.store import Chunk


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in {"--", "nan", "None", "0", "0.0"} else s


def _kv_line(label: str, value: Any) -> str:
    v = _clean(value)
    return f"{label}：{v}" if v else ""


def _join(parts: List[str], sep: str = "；") -> str:
    return sep.join(p for p in parts if p)


def chunk_market(date: str, market: Dict[str, Any]) -> Chunk:
    metrics = market.get("metrics") or {}
    metric_str = "，".join(f"{k}={v}" for k, v in metrics.items()) if isinstance(metrics, dict) else ""
    text = _join([
        f"{date} 市场情绪",
        _kv_line("情绪周期", market.get("cycle_name")),
        _kv_line("建议仓位", market.get("position")),
        _kv_line("策略基调", market.get("strategy")),
        _kv_line("关键指标", metric_str),
    ])
    return Chunk(id=f"{date}:market", date=date, kind="market", text=text)


def chunk_plans(date: str, plans: List[Dict[str, Any]]) -> List[Chunk]:
    out: List[Chunk] = []
    for p in plans or []:
        code = _clean(p.get("股票代码"))
        name = _clean(p.get("股票名称"))
        pattern = _clean(p.get("模式类型"))
        text = _join([
            f"{date} 交易计划 {name}({code}) {pattern}",
            _kv_line("优先级", p.get("优先级")),
            _kv_line("综合评分", p.get("综合评分")),
            _kv_line("建议仓位", p.get("建议仓位") or p.get("仓位等级")),
            _kv_line("仓位依据", p.get("仓位依据")),
            _kv_line("竞价条件", p.get("竞价条件")),
            _kv_line("入场区间", p.get("入场区间")),
            _kv_line("止损", p.get("止损")),
            _kv_line("止盈", p.get("止盈")),
            _kv_line("次日预期", p.get("次日预期")),
            _kv_line("风险提示", p.get("风险提示")),
            _kv_line("风控判定", p.get("风控动作")),
            _kv_line("风控提示", p.get("风控提示")),
        ])
        cid = f"{date}:plan:{code}:{pattern}" if code else f"{date}:plan:{len(out)}"
        out.append(Chunk(id=cid, date=date, kind="plan", text=text, stock_code=code))
    return out


def chunk_signals(date: str, patterns: Dict[str, Any]) -> List[Chunk]:
    out: List[Chunk] = []
    if not isinstance(patterns, dict):
        return out
    for pattern, blk in patterns.items():
        for r in (blk or {}).get("rows", []) or []:
            code = _clean(r.get("stock_code"))
            name = _clean(r.get("stock_name"))
            desc = _clean(r.get("description"))
            conf = _clean(r.get("confidence"))
            text = _join([
                f"{date} {pattern}信号 {name}({code})",
                _kv_line("置信度", conf),
                _kv_line("描述", desc),
            ])
            cid = f"{date}:signal:{pattern}:{code}" if code else f"{date}:signal:{pattern}:{len(out)}"
            out.append(Chunk(id=cid, date=date, kind="signal", text=text, stock_code=code, sector=str(pattern)))
    return out


def chunk_review(date: str, sections: List[Dict[str, Any]]) -> List[Chunk]:
    for s in sections or []:
        if s.get("name") == "复盘总结" and s.get("rows"):
            parts = []
            for row in s["rows"]:
                # 复盘 section 是「字段/值」键值表
                k = _clean(row.get("字段"))
                v = _clean(row.get("值"))
                if k and v:
                    parts.append(f"{k}：{v}")
            if parts:
                return [Chunk(id=f"{date}:review", date=date, kind="review",
                              text=f"{date} 盘后复盘 " + "；".join(parts))]
    return []


def snapshot_to_chunks(snapshot: Dict[str, Any]) -> List[Chunk]:
    date = str((snapshot.get("meta") or {}).get("date") or "")
    if not date:
        return []
    chunks: List[Chunk] = [chunk_market(date, snapshot.get("market") or {})]
    chunks += chunk_plans(date, (snapshot.get("trade_plans") or {}).get("rows", []))
    chunks += chunk_signals(date, snapshot.get("patterns") or {})
    chunks += chunk_review(date, snapshot.get("sections") or [])
    # 去掉空文本块
    return [c for c in chunks if c.text and c.text.strip()]
