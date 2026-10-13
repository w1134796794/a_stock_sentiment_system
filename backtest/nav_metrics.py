"""Shared account-return conventions, including the first trading day."""
import numpy as np
import pandas as pd


def daily_returns(values, initial_capital):
    values = pd.Series(values, dtype=float).reset_index(drop=True)
    previous = values.shift(1)
    if not previous.empty:
        previous.iloc[0] = float(initial_capital)
    return values.div(previous).sub(1).replace([np.inf, -np.inf], np.nan).dropna()


def sharpe_ratio(returns, annual_risk_free_rate=0.03):
    deviation = returns.std()
    if not np.isfinite(deviation) or deviation <= 0:
        return 0.0
    daily_risk_free = (1.0 + annual_risk_free_rate) ** (1.0 / 252) - 1.0
    return float((returns.mean() - daily_risk_free) / deviation * np.sqrt(252))
