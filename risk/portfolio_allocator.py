"""Risk-parity allocation and historical CVaR diagnostics."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd


def historical_cvar(returns: Sequence[Any], *, alpha: float = 0.95) -> Dict[str, float]:
    values = pd.to_numeric(pd.Series(returns), errors="coerce").dropna().to_numpy(dtype=float)
    if not len(values):
        return {"var": 0.0, "cvar": 0.0, "alpha": float(alpha)}
    cutoff = float(np.quantile(values, 1.0 - alpha))
    tail = values[values <= cutoff]
    return {"var": cutoff, "cvar": float(tail.mean()) if len(tail) else cutoff, "alpha": float(alpha)}


def risk_parity_weights(
    returns: pd.DataFrame,
    *,
    max_weight: float = 0.20,
    total_position: float = 0.80,
) -> Dict[str, float]:
    numeric = returns.apply(pd.to_numeric, errors="coerce").dropna(how="all")
    if numeric.empty or not len(numeric.columns):
        return {}
    volatility = numeric.std(ddof=1).replace(0, np.nan)
    inverse = (1.0 / volatility).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if inverse.sum() <= 0:
        raw = pd.Series(1.0 / len(numeric.columns), index=numeric.columns)
    else:
        raw = inverse / inverse.sum()
    cap = max(min(float(max_weight), 1.0), 0.0)
    weights = raw.copy()
    for _ in range(20):
        over = weights > cap
        if not over.any():
            break
        excess = float((weights[over] - cap).sum())
        weights[over] = cap
        under = weights < cap
        if not under.any() or excess <= 1e-12:
            break
        capacity = (cap - weights[under]).clip(lower=0)
        if capacity.sum() <= 0:
            break
        weights.loc[under] += excess * capacity / capacity.sum()
    weights = weights / max(float(weights.sum()), 1e-12) * min(max(float(total_position), 0.0), 1.0)
    return {str(key): round(float(value), 6) for key, value in weights.items()}


def portfolio_risk_report(returns: pd.DataFrame, weights: Mapping[str, float]) -> Dict[str, Any]:
    if returns.empty or not weights:
        return {"status": "insufficient_data", "cvar": historical_cvar([])}
    columns = [column for column in returns.columns if column in weights]
    if not columns:
        return {"status": "insufficient_data", "cvar": historical_cvar([])}
    vector = np.asarray([float(weights[column]) for column in columns], dtype=float)
    portfolio = returns[columns].fillna(0.0).to_numpy(dtype=float) @ vector
    correlation = returns[columns].corr().fillna(0.0).to_numpy(dtype=float)
    upper = correlation[np.triu_indices_from(correlation, k=1)]
    return {
        "status": "ok",
        "cvar": historical_cvar(portfolio),
        "annualized_volatility": float(np.std(portfolio, ddof=1) * np.sqrt(252)) if len(portfolio) > 1 else 0.0,
        "max_pair_correlation": float(upper.max()) if len(upper) else 0.0,
    }


__all__ = ["historical_cvar", "portfolio_risk_report", "risk_parity_weights"]
