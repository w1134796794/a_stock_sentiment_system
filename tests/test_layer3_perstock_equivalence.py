"""
Phase 2 验收单测：
1. normalize_weights 行为（启用子集→权重和为 1、等权兜底、空集）。
2. 逐股 D/E 因子的 registry 驱动路径在「全启用」时与旧硬编码公式逐位一致。
3. 禁用某因子后，对应键从结果中消失（开关真正生效）。

不触网：全部用合成 DataFrame / dict。
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.factors.weight_utils import normalize_weights
from core.factors import layer3_perstock as ps


# ============================================================
# normalize_weights
# ============================================================

def test_normalize_full_set_sums_to_one():
    raw = {"a": 0.2, "b": 0.3, "c": 0.5}
    out = normalize_weights(raw)
    assert pytest.approx(sum(out.values())) == 1.0
    assert pytest.approx(out["a"]) == 0.2


def test_normalize_subset_renormalizes():
    raw = {"a": 0.2, "b": 0.3, "c": 0.5}
    out = normalize_weights(raw, ["a", "b"])
    assert set(out) == {"a", "b"}
    assert pytest.approx(sum(out.values())) == 1.0
    # 0.2 : 0.3 → 0.4 : 0.6
    assert pytest.approx(out["a"]) == 0.4
    assert pytest.approx(out["b"]) == 0.6


def test_normalize_order_independent():
    raw = {"a": 0.2, "b": 0.3, "c": 0.5}
    assert normalize_weights(raw, ["a", "b"]) == normalize_weights(raw, ["b", "a"])


def test_normalize_empty_and_zero():
    raw = {"a": 0.2, "b": 0.3}
    assert normalize_weights(raw, []) == {}
    zero = normalize_weights({"a": 0.0, "b": 0.0})
    assert pytest.approx(zero["a"]) == 0.5 and pytest.approx(zero["b"]) == 0.5


# ============================================================
# 旧硬编码逻辑的参考实现（逐位复刻 layer3_stock_selection.py 改造前公式）
# ============================================================

def _old_tech(hist, row, zt_row, time_col):
    close = float(row.get('close', 0))
    pre_close = float(row.get('pre_close', 0))
    vol = float(row.get('vol', 0))
    amount = float(row.get('amount', 0))
    pct_chg = float(row.get('pct_chg', 0))
    factors = {}

    if hist is not None and not hist.empty and len(hist) >= 5:
        n_high = float(hist['high'].max())
        n_low = float(hist['low'].min())
        if n_high > n_low:
            factors['D1_n_day_high_low'] = round((close - n_low) / (n_high - n_low) * 100, 1)
        else:
            factors['D1_n_day_high_low'] = 50.0
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
                factors['D5_ma_bull_align'] = align_score
            else:
                factors['D5_ma_bull_align'] = 50.0
    else:
        factors['D1_n_day_high_low'] = 50.0
        factors['D5_ma_bull_align'] = 50.0

    if vol > 0 and pre_close > 0:
        vol_ratio = vol / max(float(hist['vol'].tail(5).mean()) if hist is not None and not hist.empty else vol, 1)
        if pct_chg > 0 and vol_ratio > 1.0:
            factors['D2_vol_price_coord'] = min(100, vol_ratio * 50 + 30)
        elif pct_chg > 0 and vol_ratio <= 1.0:
            factors['D2_vol_price_coord'] = max(0, vol_ratio * 40)
        elif pct_chg < 0 and vol_ratio < 1.0:
            factors['D2_vol_price_coord'] = 50.0
        else:
            factors['D2_vol_price_coord'] = max(0, 50 - vol_ratio * 20)
    else:
        factors['D2_vol_price_coord'] = 50.0

    seal_score = 50.0
    if zt_row is not None and time_col:
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
    factors['D3_seal_strength'] = seal_score

    turnover = float(row.get('turnover_rate', row.get('turnover', 0)))
    if turnover <= 0 and amount > 0:
        turnover = amount / 1e8
    if 3 <= turnover <= 15:
        factors['D4_turnover_health'] = 80.0
    elif 1 <= turnover < 3:
        factors['D4_turnover_health'] = 60.0
    elif 15 < turnover <= 25:
        factors['D4_turnover_health'] = 50.0
    elif turnover > 25:
        factors['D4_turnover_health'] = 30.0
    else:
        factors['D4_turnover_health'] = 40.0
    return factors


def _old_money(summary_row, code, hist_maps):
    factors = {}
    row = summary_row
    if row is not None:
        buy_elg = float(row.get('buy_elg_amount', 0) or 0)
        sell_elg = float(row.get('sell_elg_amount', 0) or 0)
        buy_lg = float(row.get('buy_lg_amount', 0) or 0)
        sell_lg = float(row.get('sell_lg_amount', 0) or 0)
        buy_md = float(row.get('buy_md_amount', 0) or 0)
        sell_md = float(row.get('sell_md_amount', 0) or 0)
        buy_sm = float(row.get('buy_sm_amount', 0) or 0)
        sell_sm = float(row.get('sell_sm_amount', 0) or 0)
        total_amount = buy_elg + sell_elg + buy_lg + sell_lg + buy_md + sell_md + buy_sm + sell_sm
        if total_amount > 0:
            main_net = (buy_elg + buy_lg) - (sell_elg + sell_lg)
            retail_net = (buy_md + buy_sm) - (sell_md + sell_sm)
            factors['E1_main_net_ratio'] = round(main_net / total_amount * 100, 2)
            factors['E2_retail_net_ratio'] = round(retail_net / total_amount * 100, 2)
            factors['E3_large_buy_ratio'] = round((buy_elg + buy_lg) / total_amount * 100, 2)
            net_flows = []
            for hist_map in hist_maps:
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
                factors['E4_moneyflow_trend'] = round(positive_days / len(net_flows) * 100, 1)
            else:
                factors['E4_moneyflow_trend'] = 50.0
        else:
            factors['E1_main_net_ratio'] = 0.0
            factors['E2_retail_net_ratio'] = 0.0
            factors['E3_large_buy_ratio'] = 0.0
            factors['E4_moneyflow_trend'] = 50.0
    else:
        factors['E1_main_net_ratio'] = 0.0
        factors['E2_retail_net_ratio'] = 0.0
        factors['E3_large_buy_ratio'] = 0.0
        factors['E4_moneyflow_trend'] = 50.0
    return factors


# ============================================================
# 合成数据
# ============================================================

def _make_hist(n, base=10.0, rising=True):
    rows = []
    for i in range(n):
        c = base + (i * 0.1 if rising else -i * 0.1)
        rows.append({
            'high': c + 0.5, 'low': c - 0.5, 'close': c,
            'pre_close': c - 0.1, 'vol': 1000 + i * 10,
            'amount': (1000 + i * 10) * c, 'pct_chg': 1.0 if rising else -1.0,
            'turnover_rate': 8.0,
        })
    return pd.DataFrame(rows)


def _tech_ctx(hist, zt_row, time_col):
    row = hist.iloc[-1]
    return {
        'hist': hist,
        'row': row,
        'close': float(row.get('close', 0)),
        'pre_close': float(row.get('pre_close', 0)),
        'vol': float(row.get('vol', 0)),
        'amount': float(row.get('amount', 0)),
        'pct_chg': float(row.get('pct_chg', 0)),
        'zt_row': zt_row,
        'time_col': time_col,
    }


# 覆盖多分支：不同窗口长度 / 涨跌 / 封板时间 / 换手
@pytest.mark.parametrize("n,rising,seal_time", [
    (3, True, '09:30:00'),    # len<5 → D1/D5=50
    (10, True, '10:15:00'),   # 5<=len<60 → D5=50
    (65, True, '09:34:00'),   # len>=60 多头
    (65, False, '14:35:00'),  # len>=60 下跌 + 尾盘板
    (65, True, None),         # 无封板时间列
])
def test_stock_tech_equivalence(n, rising, seal_time):
    hist = _make_hist(n, rising=rising)
    if seal_time is not None:
        zt_row = pd.Series({'first_time': seal_time})
        time_col = 'first_time'
    else:
        zt_row = None
        time_col = None
    ctx = _tech_ctx(hist, zt_row, time_col)

    expected = _old_tech(hist, ctx['row'], zt_row, time_col)
    got = ps.compute_stock_tech(ps.ALL_STOCK_TECH_IDS, ctx)
    assert got == expected


@pytest.mark.parametrize("summary,has_hist", [
    (None, False),
    ({'buy_elg_amount': 0, 'sell_elg_amount': 0, 'buy_lg_amount': 0, 'sell_lg_amount': 0,
      'buy_md_amount': 0, 'sell_md_amount': 0, 'buy_sm_amount': 0, 'sell_sm_amount': 0}, False),
    ({'buy_elg_amount': 500, 'sell_elg_amount': 100, 'buy_lg_amount': 300, 'sell_lg_amount': 150,
      'buy_md_amount': 200, 'sell_md_amount': 250, 'buy_sm_amount': 100, 'sell_sm_amount': 120}, True),
    ({'buy_elg_amount': 500, 'sell_elg_amount': 100, 'buy_lg_amount': 300, 'sell_lg_amount': 150,
      'buy_md_amount': 200, 'sell_md_amount': 250, 'buy_sm_amount': 100, 'sell_sm_amount': 120}, False),
])
def test_moneyflow_equivalence(summary, has_hist):
    code = '000001.SZ'
    summary_row = pd.Series(summary) if summary is not None else None
    if has_hist:
        hist_maps = [
            {code: {'buy_elg_amount': 500, 'sell_elg_amount': 100, 'buy_lg_amount': 300, 'sell_lg_amount': 50}},
            {code: {'buy_elg_amount': 100, 'sell_elg_amount': 400, 'buy_lg_amount': 50, 'sell_lg_amount': 200}},
            {code: {'buy_elg_amount': 300, 'sell_elg_amount': 100, 'buy_lg_amount': 200, 'sell_lg_amount': 100}},
        ]
    else:
        hist_maps = []
    ctx = {'summary_row': summary_row, 'code': code, 'hist_maps': hist_maps}

    expected = _old_money(summary_row, code, hist_maps)
    got = ps.compute_moneyflow(ps.ALL_MONEYFLOW_IDS, ctx)
    assert got == expected


def test_disable_factor_drops_key():
    hist = _make_hist(65, rising=True)
    zt_row = pd.Series({'first_time': '09:34:00'})
    ctx = _tech_ctx(hist, zt_row, 'first_time')

    active = [fid for fid in ps.ALL_STOCK_TECH_IDS if fid != 'D3_seal_strength']
    got = ps.compute_stock_tech(active, ctx)
    assert 'D3_seal_strength' not in got
    assert 'D1_n_day_high_low' in got