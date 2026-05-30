"""
问答（RAG）：检索历史知识块 + 注入定量工具结果，拼成给 LLM 的消息。

定量问题（出现次数 / 频率 / 历史记录）由 kb.tools 取真值注入，模型只做归纳表达。
流式回答在 Web 端点用 ``LLMClient.chat_stream`` 完成。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kb.retriever import Retriever
from kb.store import KBStore
from kb.tools import KBTools

SYSTEM_QA = (
    "你是A股短线情绪交易系统的问答助手。严格依据【检索片段】与【定量事实】回答，"
    "不要编造未提供的数字或标的；若资料不足，直说“现有数据不足以回答”。"
    "回答用中文，简洁、条理清晰，必要时引用日期。"
)

_PATTERNS = ["首板突破", "二板定龙", "弱转强", "龙头二波", "龙二波", "龙头二"]
_CYCLES = ["高潮期", "上升期", "震荡期", "回暖期", "退潮期", "冰点期"]
_CODE_RE = re.compile(r"\b(\d{6})(?:\.[A-Za-z]{2})?\b")
_QUANT_HINT = re.compile(r"几次|多少|频率|分布|占比|出现|统计|次数")
_WINRATE_HINT = re.compile(r"胜率|赢面|成功率|值得做|靠谱|edge|表现")


def _format_hits(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "（无相关历史片段）"
    return "\n".join(f"- [{h['date']}/{h['kind']}] {h['text']}" for h in hits)


def _format_facts(facts: Dict[str, Any]) -> str:
    if not facts:
        return "（无）"
    import json
    return json.dumps(facts, ensure_ascii=False)


def build_chat_messages(question: str,
                        kb_db_path: Path,
                        app_db_path: Path,
                        embedder: Optional[Any] = None,
                        date: Optional[str] = None,
                        k: int = 6) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    store = KBStore(kb_db_path)
    retriever = Retriever(store, embedder)
    tools = KBTools(app_db_path)

    # 跨历史检索（不按日期硬过滤，靠相似度排序）
    hits = retriever.search(question, k=k, date_to=date)

    # 定量工具注入
    facts: Dict[str, Any] = {}
    code_match = _CODE_RE.search(question)
    if code_match:
        # 兼容带后缀（600857.SH）与纯数字（600857）两种写法
        hist = tools.stock_history(code_match.group(0)) or tools.stock_history(code_match.group(1))
        if hist:
            facts["stock_history"] = hist[:20]
    matched_pattern = next((p for p in _PATTERNS if p in question), None)
    matched_cycle = next((c for c in _CYCLES if c in question), None)

    if matched_pattern:
        ph = tools.pattern_history(matched_pattern, days=30)
        if ph:
            facts.setdefault("pattern_history", {})[matched_pattern] = ph

    # 胜率类问题：注入周期×模式历史胜率（取真值，防模型瞎算）
    if _WINRATE_HINT.search(question) or matched_pattern or matched_cycle:
        if matched_cycle:
            wr = tools.cycle_winrate(matched_cycle)
            if wr:
                facts["winrate_by_pattern_in_cycle"] = {matched_cycle: wr}
        if matched_pattern:
            wr = tools.pattern_winrate(matched_pattern)
            if wr:
                facts["winrate_by_cycle_for_pattern"] = {matched_pattern: wr}
        if not matched_cycle and not matched_pattern and _WINRATE_HINT.search(question):
            top = tools.winrate_top(top=8)
            if top:
                facts["winrate_top_combos"] = top

    if _QUANT_HINT.search(question):
        sc = tools.signal_counts(days=20)
        if sc:
            facts["recent_signal_counts_20d"] = sc
        cd = tools.cycle_distribution(days=60)
        if cd:
            facts["cycle_distribution_60d"] = cd

    context = (
        f"【检索片段】\n{_format_hits(hits)}\n\n"
        f"【定量事实】\n{_format_facts(facts)}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_QA},
        {"role": "user", "content": f"{context}\n\n【问题】\n{question}"},
    ]
    debug = {"hits": len(hits), "facts_keys": list(facts.keys())}
    return messages, debug