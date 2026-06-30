"""Human-readable explanations for screening candidates."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


FACTOR_LABELS: Dict[str, str] = {
    "tech_score": "技术综合分",
    "volume_score": "量能综合分",
    "liquidity_score": "流动性分位",
    "sector_resonance_score": "板块共振",
    "board_score": "打板身位",
    "mkt_market_score": "市场综合分",
    "stk_total_score": "个股综合分",
    "stk_amount_ratio_5d": "成交额相对5日",
    "stk_vol_ratio_5d": "成交量相对5日",
    "stk_new_high_20d": "阶段强势位置",
    "stk_liquidity_percentile": "流动性分位",
    "stk_pct_chg_1d": "当日涨跌幅强度",
    "stk_limit_progress": "涨停进度",
    "stk_sector_resonance_score": "板块共振",
    "stk_sector_heat_score": "板块热度",
    "stk_sector_persistence_score": "板块持续性",
    "stk_sector_mainline_score": "主线强度",
    "stk_board_position": "打板身位",
    "stk_board_height": "连板高度",
    "stk_seal_time_quality": "封板时间质量",
    "stk_float_mv_fit": "流通市值适配",
    "stk_lhb_net_buy_score": "龙虎榜净买入强度",
    "stk_lhb_institution_score": "机构净买入强度",
    "stk_lhb_institution_consensus": "机构席位共识",
    "stk_lhb_repeat_persistence": "龙虎榜资金持续性",
    "stk_lhb_sector_resonance": "龙虎榜板块共振",
    "stk_lhb_composite_score": "龙虎榜综合分",
    "stk_lhb_crowding_risk": "龙虎榜拥挤安全度",
    "sec_lhb_resonance_score": "板块龙虎榜共振",
    "stk_capital_flow_consensus": "多源资金流共识",
    "stk_capital_flow_persistence": "资金流持续性",
    "stk_attention_consensus": "双平台热度共识",
    "stk_attention_crowding_risk": "热度拥挤安全度",
    "stk_kpl_leader_quality": "开盘啦龙头质量",
    "stk_margin_acceleration": "融资净买入加速度",
    "stk_block_trade_risk": "大宗交易安全度",
    "sec_capital_flow_score": "板块资金流强度",
    "sec_flow_price_resonance": "板块量价资金共振",
}

FACTOR_NOTE: Dict[str, str] = {
    "tech_score": "技术形态综合表现靠前",
    "volume_score": "量能状态较好",
    "liquidity_score": "流动性处于候选前列",
    "sector_resonance_score": "所属板块形成共振",
    "board_score": "涨停身位质量较好",
    "mkt_market_score": "市场环境可交易",
    "stk_total_score": "个股综合强度靠前",
    "stk_amount_ratio_5d": "资金关注度改善",
    "stk_vol_ratio_5d": "成交活跃度改善",
    "stk_new_high_20d": "走势接近阶段强势区",
    "stk_liquidity_percentile": "承接能力较好",
    "stk_pct_chg_1d": "当日修复力度较强",
    "stk_limit_progress": "涨幅接近对应板块涨停幅度",
    "stk_sector_resonance_score": "板块联动较强",
    "stk_sector_heat_score": "所属板块热度靠前",
    "stk_sector_persistence_score": "所属板块持续性较好",
    "stk_sector_mainline_score": "所属题材具备主线强度",
    "stk_board_position": "涨停身位质量较好",
    "stk_board_height": "连板高度具备辨识度",
    "stk_seal_time_quality": "封板时间质量较好",
    "stk_float_mv_fit": "流通市值适合短线交易",
    "stk_lhb_net_buy_score": "龙虎榜净买入相对成交额较强",
    "stk_lhb_institution_score": "机构资金方向偏正面",
    "stk_lhb_institution_consensus": "机构席位买卖方向较一致",
    "stk_lhb_repeat_persistence": "近5日上榜资金具有持续性",
    "stk_lhb_sector_resonance": "同板块龙虎榜资金形成共振",
    "stk_lhb_composite_score": "龙虎榜资金结构综合表现较好",
    "stk_lhb_crowding_risk": "龙虎榜席位集中风险较低",
    "sec_lhb_resonance_score": "板块上榜资金形成合力",
    "stk_capital_flow_consensus": "同花顺与东方财富资金方向形成共识",
    "stk_capital_flow_persistence": "五日资金流具备持续性",
    "stk_attention_consensus": "双平台热度排名形成交叉确认",
    "stk_attention_crowding_risk": "市场热度尚未形成明显拥挤",
    "stk_kpl_leader_quality": "开盘啦标签与封单质量确认龙头辨识度",
    "stk_margin_acceleration": "融资净买入边际改善",
    "stk_block_trade_risk": "未出现显著折价大宗交易风险",
    "sec_capital_flow_score": "所属板块获得资金净流入",
    "sec_flow_price_resonance": "板块价格与资金方向共振",
}


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_score(value: Any) -> str:
    num = _to_float(value)
    if num is None:
        return "--"
    return f"{num:.1f}"


def _compact_base_reasons(base_reasons: Iterable[Any]) -> str:
    texts = [str(x).strip() for x in base_reasons if str(x).strip()]
    if not texts:
        return "通过基础过滤"
    stage_words: List[str] = []
    if any("市场" in text for text in texts):
        stage_words.append("市场")
    if any("流动性" in text or "成交额分位" in text for text in texts):
        stage_words.append("流动性")
    if any("综合" in text or "强度" in text for text in texts):
        stage_words.append("强度")
    if any("量" in text or "成交额" in text for text in texts):
        stage_words.append("量能")
    if any("趋势" in text or "价格位置" in text or "强势区" in text for text in texts):
        stage_words.append("趋势")
    if not stage_words:
        return texts[0]
    return "通过" + "、".join(dict.fromkeys(stage_words)) + "过滤"


def build_screening_reasons(
    *,
    metrics: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    score: Any = None,
    rank: Any = None,
    base_reasons: Optional[Iterable[Any]] = None,
    max_parts: int = 5,
) -> List[str]:
    """Build per-stock explanations from factor metrics plus rule pass reasons."""
    parts: List[str] = []
    rank_num = _to_float(rank)
    score_num = _to_float(score)
    if score_num is not None:
        prefix = f"综合评分 {_fmt_score(score_num)}"
        if rank_num is not None:
            prefix += f"，候选排名第 {int(rank_num)}"
        parts.append(prefix)

    context = context or {}
    pct_chg = _to_float(context.get("pct_chg"))
    if pct_chg is not None:
        limit_pct = _to_float(context.get("limit_pct"))
        progress = _to_float(context.get("limit_progress"))
        if limit_pct and progress is not None:
            if progress >= 0.95:
                parts.append(f"当日接近{limit_pct:.0f}cm涨停，涨幅 {pct_chg:+.2f}%")
            elif progress >= 0.60:
                parts.append(f"当日强涨幅 {pct_chg:+.2f}%，约为{limit_pct:.0f}cm涨停进度 {progress * 100:.0f}%")
            else:
                parts.append(f"当日涨幅 {pct_chg:+.2f}%，{limit_pct:.0f}cm涨停进度 {progress * 100:.0f}%")
        else:
            parts.append(f"当日涨幅 {pct_chg:+.2f}%")

    amount_ratio = _to_float(context.get("amount_ratio"))
    vol_ratio = _to_float(context.get("vol_ratio"))
    if amount_ratio is not None and vol_ratio is not None:
        if amount_ratio >= 1.2 or vol_ratio >= 1.2:
            parts.append(f"量价活跃，成交额/成交量约为5日均值 {amount_ratio:.2f}/{vol_ratio:.2f} 倍")
        elif amount_ratio < 0.9 or vol_ratio < 0.9:
            parts.append(f"量能未明显放大，成交额/成交量约为5日均值 {amount_ratio:.2f}/{vol_ratio:.2f} 倍")

    numeric_metrics = []
    for key, value in (metrics or {}).items():
        num = _to_float(value)
        if num is None:
            continue
        numeric_metrics.append((key, num))

    for key, num in sorted(numeric_metrics, key=lambda item: item[1], reverse=True)[:2]:
        label = FACTOR_LABELS.get(key, key)
        note = FACTOR_NOTE.get(key, "指标表现靠前")
        parts.append(f"{label} {_fmt_score(num)}，{note}")

    weak = [(key, num) for key, num in numeric_metrics if num < 45]
    if weak:
        key, num = sorted(weak, key=lambda item: item[1])[0]
        label = FACTOR_LABELS.get(key, key)
        parts.append(f"{label} {_fmt_score(num)}偏弱，需盘中确认")

    base_summary = _compact_base_reasons(base_reasons or [])
    if base_summary and base_summary not in parts:
        parts.append(base_summary)

    deduped: List[str] = []
    for part in parts:
        if part and part not in deduped:
            deduped.append(part)
    return deduped[:max(1, int(max_parts or 5))]
