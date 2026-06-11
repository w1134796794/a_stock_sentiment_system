"""
Layer3 个股因子计算（单一真源）

每个函数对应 FactorRegistry 中一个 stock_tech(D) / moneyflow(E) 因子 ID。
Layer3 按 registry 的 enabled 开关调度这些函数：默认全启用时，输出与历史
硬编码逐股逻辑**逐位一致**（见 tests/test_layer3_perstock_equivalence.py）。

设计要点：
- 纯函数：仅依赖传入的逐股上下文，不触网、不持有状态，便于单测与复现。
- 容错沿用旧实现：缺失/异常分支返回与旧代码相同的兜底值（如 50.0 / 0.0）。
"""
from typing import Dict, List, Optional, Callable, Any
import pandas as pd


# ---------- D：个股技术因子 ----------

def calc_D1_n_day_high_low(hist: pd.DataFrame, close: float, **_) -> Optional[float]:
    """D1: N日高低位 —— 当前价在窗口最高/最低之间的位置(0-100)。"""
    if hist is not None and not hist.empty and len(hist) >= 5:
        n_high = float(hist['high'].max())
        n_low = float(hist['low'].min())
        if n_high > n_low:
            return round((close - n_low) / (n_high - n_low) * 100, 1)
        return 50.0
    return 50.0


def calc_D2_vol_price_coord(hist: pd.DataFrame, vol: float, pre_close: float,
                            pct_chg: float, **_) -> Optional[float]:
    """D2: 量价配合度 —— 涨幅与量比的匹配程度。"""
    if vol > 0 and pre_close > 0:
        vol_ratio = vol / max(
            float(hist['vol'].tail(5).mean()) if hist is not None and not hist.empty else vol, 1
        )
        if pct_chg > 0 and vol_ratio > 1.0:
            return min(100, vol_ratio * 50 + 30)
        elif pct_chg > 0 and vol_ratio <= 1.0:
            return max(0, vol_ratio * 40)
        elif pct_chg < 0 and vol_ratio < 1.0:
            return 50.0
        else:
            return max(0, 50 - vol_ratio * 20)
    return 50.0


def calc_D3_seal_strength(zt_row: Optional[pd.Series], time_col: Optional[str], **_) -> Optional[float]:
    """D3: 封板强度 —— 由首次封板时间映射打分。"""
    seal_score = 50.0
    if zt_row is not None and time_col is not None:
        ft = str(zt_row.get(time_col, '')).strip()
        if ft <= '09:35:00':
            seal_score = 90.0
        elif ft <= '10:00:00':
            seal_score = 75.0
        elif ft <= '10:30:00':
            seal_score = 60.0
        elif ft <= '11:30:00':
            seal_score = 45.0
        elif ft <= '14:00:00':
            seal_score = 30.0
        else:
            seal_score = 15.0
    return seal_score


def calc_D4_turnover_health(row: pd.Series, amount: float, **_) -> Optional[float]:
    """D4: 换手率健康度 —— 换手率是否处于合理区间。"""
    turnover = float(row.get('turnover_rate', row.get('turnover', 0)))
    if turnover <= 0 and amount > 0:
        turnover = amount / 1e8
    if 3 <= turnover <= 15:
        return 80.0
    elif 1 <= turnover < 3:
        return 60.0
    elif 15 < turnover <= 25:
        return 50.0
    elif turnover > 25:
        return 30.0
    else:
        return 40.0


def calc_D5_ma_bull_align(hist: pd.DataFrame, close: float, **_) -> Optional[float]:
    """D5: 均线多头排列度 —— MA5>MA10>MA20>MA60 与价在均线上的程度。"""
    if hist is not None and not hist.empty and len(hist) >= 5:
        if 'close' in hist.columns:
            closes = hist['close'].astype(float)
            if len(closes) >= 60:
                ma5 = closes.tail(5).mean()
                ma10 = closes.tail(10).mean()
                ma20 = closes.tail(20).mean()
                ma60 = closes.tail(60).mean()
                align_score = 0
                if ma5 > ma10:
                    align_score += 25
                if ma10 > ma20:
                    align_score += 25
                if ma20 > ma60:
                    align_score += 25
                if close > ma5:
                    align_score += 25
                return align_score
            return 50.0
        # 旧实现：len>=5 但无 close 列时不写入该键
        return None
    return 50.0


# ---------- E：资金流向因子 ----------

def _money_amounts(summary_row) -> Dict[str, float]:
    """从资金流汇总行提取八类买卖额与总额（与旧实现一致）。"""
    buy_elg = float(summary_row.get('buy_elg_amount', 0) or 0)
    sell_elg = float(summary_row.get('sell_elg_amount', 0) or 0)
    buy_lg = float(summary_row.get('buy_lg_amount', 0) or 0)
    sell_lg = float(summary_row.get('sell_lg_amount', 0) or 0)
    buy_md = float(summary_row.get('buy_md_amount', 0) or 0)
    sell_md = float(summary_row.get('sell_md_amount', 0) or 0)
    buy_sm = float(summary_row.get('buy_sm_amount', 0) or 0)
    sell_sm = float(summary_row.get('sell_sm_amount', 0) or 0)
    total = buy_elg + sell_elg + buy_lg + sell_lg + buy_md + sell_md + buy_sm + sell_sm
    return {
        'buy_elg': buy_elg, 'sell_elg': sell_elg,
        'buy_lg': buy_lg, 'sell_lg': sell_lg,
        'buy_md': buy_md, 'sell_md': sell_md,
        'buy_sm': buy_sm, 'sell_sm': sell_sm,
        'total': total,
    }


def calc_E1_main_net_ratio(summary_row, **_) -> Optional[float]:
    """E1: 主力净流入占比 = (大单+特大单净额)/总成交 × 100。"""
    if summary_row is None:
        return 0.0
    a = _money_amounts(summary_row)
    if a['total'] > 0:
        main_net = (a['buy_elg'] + a['buy_lg']) - (a['sell_elg'] + a['sell_lg'])
        return round(main_net / a['total'] * 100, 2)
    return 0.0


def calc_E2_retail_net_ratio(summary_row, **_) -> Optional[float]:
    """E2: 散户净流入占比 = (中单+小单净额)/总成交 × 100。"""
    if summary_row is None:
        return 0.0
    a = _money_amounts(summary_row)
    if a['total'] > 0:
        retail_net = (a['buy_md'] + a['buy_sm']) - (a['sell_md'] + a['sell_sm'])
        return round(retail_net / a['total'] * 100, 2)
    return 0.0


def calc_E3_large_buy_ratio(summary_row, **_) -> Optional[float]:
    """E3: 大单买入占比 = (大单买+特大单买)/总成交 × 100。"""
    if summary_row is None:
        return 0.0
    a = _money_amounts(summary_row)
    if a['total'] > 0:
        return round((a['buy_elg'] + a['buy_lg']) / a['total'] * 100, 2)
    return 0.0


def calc_E4_moneyflow_trend(summary_row, code: str,
                            hist_maps: Optional[List[dict]] = None, **_) -> Optional[float]:
    """E4: 资金流向趋势 —— 近 N 日主力净流入为正的天数占比 × 100。"""
    if summary_row is None:
        return 50.0
    a = _money_amounts(summary_row)
    if a['total'] <= 0:
        return 50.0
    net_flows = []
    for hist_map in (hist_maps or []):
        hr = hist_map.get(code)
        if hr is None:
            continue
        b_elg = float(hr.get('buy_elg_amount', 0) or 0)
        s_elg = float(hr.get('sell_elg_amount', 0) or 0)
        b_lg = float(hr.get('buy_lg_amount', 0) or 0)
        s_lg = float(hr.get('sell_lg_amount', 0) or 0)
        net_flows.append((b_elg + b_lg) - (s_elg + s_lg))
    if net_flows:
        positive_days = sum(1 for nf in net_flows if nf > 0)
        return round(positive_days / len(net_flows) * 100, 1)
    return 50.0


# ---------- 因子调度表（factor_id → 计算函数）----------

STOCK_TECH_FNS: Dict[str, Callable[..., Any]] = {
    'D1_n_day_high_low': calc_D1_n_day_high_low,
    'D2_vol_price_coord': calc_D2_vol_price_coord,
    'D3_seal_strength': calc_D3_seal_strength,
    'D4_turnover_health': calc_D4_turnover_health,
    'D5_ma_bull_align': calc_D5_ma_bull_align,
}

MONEYFLOW_FNS: Dict[str, Callable[..., Any]] = {
    'E1_main_net_ratio': calc_E1_main_net_ratio,
    'E2_retail_net_ratio': calc_E2_retail_net_ratio,
    'E3_large_buy_ratio': calc_E3_large_buy_ratio,
    'E4_moneyflow_trend': calc_E4_moneyflow_trend,
}

# 全部逐股因子 ID（用于默认/兜底启用集）
ALL_STOCK_TECH_IDS = list(STOCK_TECH_FNS.keys())
ALL_MONEYFLOW_IDS = list(MONEYFLOW_FNS.keys())


def _active_ids(registry, layer: str, sub_categories: List[str],
                fn_map: Dict[str, Callable], all_ids: List[str]) -> List[str]:
    """
    从 registry 解析某 Layer 下启用且有计算函数的因子 ID（保持 all_ids 的稳定顺序）。

    判定：因子在某子类的 enabled_factors 列表中 AND FactorDefinition.enabled 为真。
    registry 为 None 时退化为全启用（兜底，保证独立调用不崩）。
    """
    if registry is None:
        return list(all_ids)

    enabled_set = set()
    try:
        for sub in sub_categories:
            for fid in registry.get_enabled_factors(layer, sub):
                fdef = registry.get_factor(fid)
                if fid in fn_map and (fdef is None or fdef.enabled):
                    enabled_set.add(fid)
    except Exception:
        return list(all_ids)

    # 保持稳定顺序
    return [fid for fid in all_ids if fid in enabled_set]


def active_stock_tech_factors(registry) -> List[str]:
    """Layer3 当前启用的逐股技术因子 ID（稳定顺序）。"""
    return _active_ids(
        registry, 'layer3',
        ['trend', 'volume', 'quality', 'size'],
        STOCK_TECH_FNS, ALL_STOCK_TECH_IDS,
    )


def active_moneyflow_factors(registry) -> List[str]:
    """Layer3 当前启用的逐股资金流因子 ID（稳定顺序）。"""
    return _active_ids(
        registry, 'layer3',
        ['moneyflow'],
        MONEYFLOW_FNS, ALL_MONEYFLOW_IDS,
    )


def compute_stock_tech(active_ids: List[str], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """对单只股票计算启用的 D 因子；None 结果不写入（与旧实现一致）。"""
    out: Dict[str, Any] = {}
    for fid in active_ids:
        fn = STOCK_TECH_FNS.get(fid)
        if fn is None:
            continue
        val = fn(**ctx)
        if val is not None:
            out[fid] = val
    return out


def compute_moneyflow(active_ids: List[str], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """对单只股票计算启用的 E 因子；None 结果不写入。"""
    out: Dict[str, Any] = {}
    for fid in active_ids:
        fn = MONEYFLOW_FNS.get(fid)
        if fn is None:
            continue
        val = fn(**ctx)
        if val is not None:
            out[fid] = val
    return out