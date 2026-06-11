"""
Phase 2 验收：Layer3 registry 驱动改造的「接线」正确性（无网络）。

用 FakeRepo 提供合成日线/资金流，运行 Layer3 的 _compute_stock_tech_factors /
_compute_moneyflow_factors，校验：
1. 默认全启用时，结果 dict 与「按相同上下文直接调用逐股纯函数」完全一致
   （即 row/zt_row/time_col/summary_map/hist_maps 等上下文构造无误）。
2. 通过 registry 禁用某因子后，对应键从结果中消失，其余键不变。
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.factors import layer3_perstock as ps
from core.factors.factor_registry import get_factor_registry
from core.pipeline.layer3_stock_selection import StockSelectionLayer, StockSelectionResult


class _DateUtils:
    def get_last_n_trade_dates(self, n, trade_date):
        return ["20260603", "20260604", "20260605"]


class FakeRepo:
    def __init__(self, hist_by_code, summary_by_date):
        self._hist = hist_by_code
        self._summary = summary_by_date
        self.date_utils = _DateUtils()

    def get_stocks_daily_batch(self, codes, start, end):
        return {c: self._hist[c] for c in codes if c in self._hist}

    def get_moneyflow_summary(self, date):
        return self._summary.get(date, pd.DataFrame())


def _hist_df(n, base=10.0):
    rows = []
    for i in range(n):
        c = base + i * 0.1
        rows.append({
            'high': c + 0.5, 'low': c - 0.5, 'close': c, 'pre_close': c - 0.1,
            'vol': 1000 + i * 10, 'amount': (1000 + i * 10) * c,
            'pct_chg': 1.0, 'turnover_rate': 8.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def layer():
    lyr = StockSelectionLayer(data_manager=None)
    lyr._factor_registry = get_factor_registry()
    return lyr


@pytest.fixture
def zt_pool():
    return pd.DataFrame([
        {'ts_code': '000001.SZ', 'first_time': '09:34:00'},
        {'ts_code': '600000.SH', 'first_time': '10:45:00'},
    ])


@pytest.fixture
def repo():
    hist = {'000001.SZ': _hist_df(65), '600000.SH': _hist_df(10)}
    summary = pd.DataFrame([
        {'ts_code': '000001.SZ', 'buy_elg_amount': 500, 'sell_elg_amount': 100,
         'buy_lg_amount': 300, 'sell_lg_amount': 150, 'buy_md_amount': 200,
         'sell_md_amount': 250, 'buy_sm_amount': 100, 'sell_sm_amount': 120},
        # 600000.SH 缺失 → 走默认分支
    ])
    hist_summary = pd.DataFrame([
        {'ts_code': '000001.SZ', 'buy_elg_amount': 400, 'sell_elg_amount': 100,
         'buy_lg_amount': 200, 'sell_lg_amount': 50},
    ])
    summary_by_date = {
        '20260608': summary,
        '20260603': hist_summary, '20260604': hist_summary, '20260605': hist_summary,
    }
    return FakeRepo(hist, summary_by_date)


def _expected_tech(zt_pool, repo, active_ids):
    """用相同上下文直接调用纯函数，得到期望结果（校验接线而非公式）。"""
    out = {}
    time_col = 'first_time'
    hist_map = repo.get_stocks_daily_batch(['000001.SZ', '600000.SH'], '', '')
    for code in ['000001.SZ', '600000.SH']:
        hist = hist_map.get(code)
        if hist is None or hist.empty:
            continue
        row = hist.iloc[-1]
        zt_match = zt_pool[zt_pool['ts_code'].astype(str) == code]
        zt_row = zt_match.iloc[0] if not zt_match.empty else None
        ctx = {
            'hist': hist, 'row': row,
            'close': float(row.get('close', 0)), 'pre_close': float(row.get('pre_close', 0)),
            'vol': float(row.get('vol', 0)), 'amount': float(row.get('amount', 0)),
            'pct_chg': float(row.get('pct_chg', 0)), 'zt_row': zt_row, 'time_col': time_col,
        }
        out[code] = ps.compute_stock_tech(active_ids, ctx)
    return out


def test_tech_wiring_matches_pure_functions(layer, zt_pool, repo):
    layer.repo = repo
    result = StockSelectionResult(trade_date="20260608")
    layer._compute_stock_tech_factors(result, zt_pool, "20260608")

    active = ps.active_stock_tech_factors(layer._factor_registry)
    expected = _expected_tech(zt_pool, repo, active)
    assert result.stock_tech_factors == expected
    # 健全性：长窗口股票应拿到全部 5 个 D 因子
    assert set(result.stock_tech_factors['000001.SZ']) == set(active)


def test_moneyflow_wiring(layer, zt_pool, repo):
    layer.repo = repo
    result = StockSelectionResult(trade_date="20260608")
    layer._compute_moneyflow_factors(result, zt_pool, "20260608")

    # 有资金流的票：E1-E4 齐全；缺失的票走默认值
    assert set(result.moneyflow_factors['000001.SZ']) == {
        'E1_main_net_ratio', 'E2_retail_net_ratio', 'E3_large_buy_ratio', 'E4_moneyflow_trend'
    }
    assert result.moneyflow_factors['600000.SH'] == {
        'E1_main_net_ratio': 0.0, 'E2_retail_net_ratio': 0.0,
        'E3_large_buy_ratio': 0.0, 'E4_moneyflow_trend': 50.0,
    }


def test_disable_factor_via_registry(layer, zt_pool, repo):
    """禁用 D3 后结果不含 D3 键，其余键不变。"""
    reg = layer._factor_registry
    try:
        reg.disable_factor('D3_seal_strength')
        result = StockSelectionResult(trade_date="20260608")
        layer.repo = repo
        layer._compute_stock_tech_factors(result, zt_pool, "20260608")
        assert 'D3_seal_strength' not in result.stock_tech_factors['000001.SZ']
        assert 'D1_n_day_high_low' in result.stock_tech_factors['000001.SZ']
    finally:
        reg.enable_factor('D3_seal_strength')  # 复位，避免污染其它测试