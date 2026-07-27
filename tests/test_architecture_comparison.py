from __future__ import annotations

from dataclasses import dataclass

import pytest

from backtest.architecture_comparison import summarize_architecture_result


@dataclass
class _Trade:
    action: str
    pnl: float


def test_architecture_summary_reports_coverage_profit_factor_and_risk():
    result = {
        "trade_history": [
            _Trade("BUY", 0),
            _Trade("SELL", 1200),
            _Trade("SELL_PARTIAL", -400),
        ],
        "entry_candidate_count": 20,
        "buy_trades": 5,
        "win_rate": 0.5,
        "total_return": 0.12,
        "max_drawdown": -0.08,
    }

    row = summarize_architecture_result("精简链路", result)

    assert row["architecture"] == "精简链路"
    assert row["coverage_rate"] == pytest.approx(0.25)
    assert row["closed_trades"] == 2
    assert row["profit_factor"] == pytest.approx(3.0)
    assert row["total_return"] == pytest.approx(0.12)
    assert row["max_drawdown"] == pytest.approx(-0.08)
