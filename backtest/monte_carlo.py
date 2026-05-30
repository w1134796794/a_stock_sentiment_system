"""
蒙特卡洛交易重采样（C-4）

单条历史净值曲线只是"一次抽样"——交易的先后顺序、是否恰好避开连亏，都可能让回测
结果显得过好或过坏。本模块对**逐笔已实现盈亏**做有放回重采样（bootstrap），重建大量
等概率的"平行历史"，给出最终收益与最大回撤的分布、置信区间与亏损概率，作为稳健性护栏。

固定 ``seed`` → 结果可复现，离线可测。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from backtest.strategy_stats import _attr

_SELL_ACTIONS = ("SELL", "SELL_PARTIAL")


def extract_trade_pnls(trade_history: List) -> List[float]:
    """从成交记录抽取逐笔已实现盈亏（金额）。"""
    out = []
    for t in trade_history or []:
        if _attr(t, "action", "") in _SELL_ACTIONS:
            out.append(float(_attr(t, "pnl", 0.0) or 0.0))
    return out


def _path_max_drawdown(equity_path: np.ndarray) -> float:
    """权益路径的最大回撤（负数）。"""
    running_max = np.maximum.accumulate(equity_path)
    dd = (equity_path - running_max) / running_max
    return float(dd.min()) if len(dd) else 0.0


def monte_carlo_resample(trade_pnls: List[float],
                         initial_capital: float = 100_000.0,
                         n_sims: int = 2000,
                         seed: Optional[int] = 42) -> Dict:
    """
    对逐笔盈亏做有放回重采样，模拟 ``n_sims`` 条平行净值路径。

    Returns:
        含 final_return / max_drawdown 的分布分位数、均值、亏损概率等。
    """
    pnls = np.asarray([p for p in trade_pnls], dtype=float)
    n = len(pnls)
    if n == 0 or initial_capital <= 0:
        return {"n_trades": 0, "n_sims": 0, "note": "无成交记录，跳过蒙特卡洛"}

    rng = np.random.default_rng(seed)
    final_returns = np.empty(n_sims)
    max_dds = np.empty(n_sims)

    for s in range(n_sims):
        sample = rng.choice(pnls, size=n, replace=True)
        equity = initial_capital + np.cumsum(sample)
        equity = np.concatenate(([initial_capital], equity))
        final_returns[s] = (equity[-1] - initial_capital) / initial_capital
        max_dds[s] = _path_max_drawdown(equity)

    def _pct(arr, q):
        return float(np.percentile(arr, q))

    return {
        "n_trades": n,
        "n_sims": n_sims,
        "initial_capital": initial_capital,
        "final_return": {
            "mean": float(final_returns.mean()),
            "p5": _pct(final_returns, 5),
            "p25": _pct(final_returns, 25),
            "median": _pct(final_returns, 50),
            "p75": _pct(final_returns, 75),
            "p95": _pct(final_returns, 95),
        },
        "max_drawdown": {
            "mean": float(max_dds.mean()),
            "p5_worst": _pct(max_dds, 5),     # 最差 5% 的回撤
            "median": _pct(max_dds, 50),
            "p95_best": _pct(max_dds, 95),
        },
        # 风险概率
        "prob_loss": float((final_returns < 0).mean()),
        "prob_dd_gt_20pct": float((max_dds < -0.20).mean()),
        "ci95_return": [_pct(final_returns, 2.5), _pct(final_returns, 97.5)],
    }


def monte_carlo_from_report(report: Dict, n_sims: int = 2000, seed: Optional[int] = 42) -> Dict:
    """便捷入口：直接吃 ReplayEngine / BacktestEngine 的结果 dict。"""
    pnls = extract_trade_pnls(report.get("trade_history", []))
    initial = report.get("initial_capital", 100_000.0)
    return monte_carlo_resample(pnls, initial_capital=initial, n_sims=n_sims, seed=seed)


__all__ = ["extract_trade_pnls", "monte_carlo_resample", "monte_carlo_from_report"]
