from datetime import datetime, timedelta

from backtest.backtest_engine import TradeRecord
from backtest.walk_forward import build_walk_forward_frames


def test_walk_forward_selects_on_train_and_reports_unseen_windows():
    start = datetime(2026, 1, 1)
    trades = []
    for index in range(100):
        date = (start + timedelta(days=index)).strftime("%Y%m%d")
        profitable = index % 3 != 0
        trades.append(TradeRecord(
            date=date,
            stock_code=f"{index:06d}",
            stock_name="测试",
            pattern_type="指标筛选/default",
            action="SELL",
            entry_price=10,
            exit_price=11 if profitable else 9.5,
            shares=100,
            position_size=1000,
            pnl=100 if profitable else -50,
            pnl_pct=0.1 if profitable else -0.05,
            holding_days=1,
            hot_resonance=False,
            resonance_sectors="",
            entry_date=date,
            plan_rank=1,
            stop_loss_triggered=not profitable,
            market_score=75,
            open_gap_pct=0.01,
            amount_ratio=1.1,
        ))

    folds, summary = build_walk_forward_frames(
        {"trade_history": trades}, train_days=60, validation_days=20, min_train_samples=8
    )

    assert len(folds) == 2
    assert folds.iloc[0]["train_end"] < folds.iloc[0]["validation_start"]
    assert summary.iloc[0]["oos_samples"] == 40
    assert 0 < summary.iloc[0]["oos_win_rate"] < 1
