import json
from pathlib import Path

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine, TradeRecord
from backtest.attribution import build_attribution_frames
from backtest.plan_source import build_backtest_plan_dir
from desktop import backtest as backtest_view


def test_plan_source_keeps_only_top_three_for_backtest(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    output_dir = tmp_path / "webdata"

    final = []
    for rank in range(1, 6):
        final.append({
            "code": f"00000{rank}",
            "name": f"股票{rank}",
            "rank": rank,
            "score": 70 - rank,
            "reasons": [f"rank {rank}"],
            "metrics": {
                "stk_total_score": 70 - rank,
                "stk_amount_ratio_5d": 80 - rank,
            },
        })
    payload = {
        "meta": {"date": "20260618", "engine": "etl"},
        "etl": {
            "screening": {"profile": "default", "final": final},
            "gold_summary": {"market": {"market_score": 62, "width_score": 55, "emotion_score": 58}},
        },
    }
    (snapshot_dir / "20260618.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=output_dir,
        start_date="20260618",
        end_date="20260618",
    )

    assert file_count == 1
    assert row_count == 3
    df = pd.read_csv(plan_dir / "交易计划_20260618.csv")
    assert list(df["优先级"]) == [1, 2, 3]
    assert "因子指标" in df.columns
    assert list(df["原始_mkt_market_score"].unique()) == [62.0]


def test_plan_source_supports_top_one_comparison(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    payload = {
        "meta": {"date": "20260618"},
        "etl": {"screening": {"profile": "default", "final": [
            {"code": "000001", "name": "第一名", "rank": 1, "score": 90},
            {"code": "000002", "name": "第二名", "rank": 2, "score": 89},
        ]}},
    }
    (snapshot_dir / "20260618.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    plan_dir, _, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=tmp_path / "webdata",
        max_rank=1,
    )

    assert row_count == 1
    result = pd.read_csv(plan_dir / "交易计划_20260618.csv")
    assert result.iloc[0]["名称"] == "第一名"


def test_backtest_view_distinguishes_closed_trades_and_execution_rows(tmp_path, monkeypatch):
    run = "20260627_120000"
    monkeypatch.setattr(backtest_view, "RESULTS_DIR", tmp_path)
    (tmp_path / f"backtest_summary_{run}.csv").write_text(
        "initial_capital,final_capital,total_trades,total_return,win_rate\n"
        "100000,101000,1,0.01,1\n",
        encoding="utf-8",
    )
    (tmp_path / f"backtest_nav_{run}.csv").write_text(
        "date,total_value\n20260626,100000\n20260627,101000\n",
        encoding="utf-8",
    )
    (tmp_path / f"backtest_trades_{run}.csv").write_text(
        "date,stock_code,stock_name,pattern_type,action,entry_price,exit_price,shares,pnl,pnl_pct\n"
        "20260626,000001,A,default,BUY,10.126,0,1000,0,0\n"
        "20260627,000001,A,default,SELL,10.126,11.239,1000,1000,0.1\n"
        "20260627,000002,B,default,BUY,20.555,0,500,0,0\n",
        encoding="utf-8",
    )

    overview = backtest_view.backtest_overview(run)

    assert overview["closed_count"] == 1
    assert overview["buy_count"] == 2
    assert overview["execution_count"] == 3
    assert overview["open_count"] == 1
    assert len(overview["trade_rows"]) == 2
    closed = next(row for row in overview["trade_rows"] if row["退出"] != "持仓中")
    opened = next(row for row in overview["trade_rows"] if row["退出"] == "持仓中")
    assert closed["买入价"] == "10.13"
    assert closed["卖出价"] == "11.24"
    assert opened["买入价"] == "20.55"
    assert opened["卖出价"] == ""


def test_plan_source_prefers_external_screening_over_snapshot(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    screening_dir = tmp_path / "screening"
    output_dir = tmp_path / "webdata"
    snapshot_dir.mkdir()
    screening_dir.mkdir()

    snapshot_payload = {
        "meta": {"date": "20260618"},
        "etl": {"screening": {"profile": "default", "final": [{
            "code": "000001", "name": "旧候选", "rank": 1, "score": 99, "metrics": {}
        }]}},
    }
    external_payload = {
        "trade_date": "20260618",
        "profile": "default",
        "ok": True,
        "final": [{
            "code": "000002", "name": "新候选", "rank": 1, "score": 88, "metrics": {}
        }],
    }
    (snapshot_dir / "20260618.json").write_text(json.dumps(snapshot_payload, ensure_ascii=False), encoding="utf-8")
    (screening_dir / "screening_20260618.json").write_text(json.dumps(external_payload, ensure_ascii=False), encoding="utf-8")

    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=output_dir,
        screening_dir=screening_dir,
        start_date="20260618",
        end_date="20260618",
    )

    assert file_count == 1
    assert row_count == 1
    df = pd.read_csv(plan_dir / "交易计划_20260618.csv")
    assert df.iloc[0]["名称"] == "新候选"


def test_backtest_report_counts_only_closed_trades():
    engine = BacktestEngine(data_manager=None)
    engine.config.initial_capital = 100_000
    engine.total_capital = 101_000
    engine.daily_nav = [
        {"date": "20260617", "total_value": 100_000},
        {"date": "20260618", "total_value": 101_000},
    ]
    engine.trade_history = [
        TradeRecord(
            date="20260617", stock_code="000001", stock_name="A", pattern_type="指标筛选/default",
            action="BUY", entry_price=10, exit_price=0, shares=1000, position_size=10_000,
            pnl=0, pnl_pct=0, holding_days=0, hot_resonance=False, resonance_sectors="",
        ),
        TradeRecord(
            date="20260618", stock_code="000001", stock_name="A", pattern_type="指标筛选/default",
            action="SELL", entry_price=10, exit_price=11, shares=1000, position_size=10_000,
            pnl=1_000, pnl_pct=0.1, holding_days=1, hot_resonance=False, resonance_sectors="",
        ),
        TradeRecord(
            date="20260618", stock_code="000002", stock_name="B", pattern_type="指标筛选/default",
            action="SELL", entry_price=10, exit_price=9.5, shares=1000, position_size=10_000,
            pnl=-500, pnl_pct=-0.05, holding_days=1, hot_resonance=False, resonance_sectors="",
        ),
    ]

    report = engine._generate_backtest_report()

    assert report["total_trades"] == 2
    assert report["buy_trades"] == 1
    assert report["closed_trades"] == 2
    assert report["win_rate"] == 0.5


def test_market_regime_controls_daily_entry_rank(tmp_path):
    engine = BacktestEngine(data_manager=None, config=BacktestConfig(max_plan_rank=3))

    def write_plan(date, market_score):
        pd.DataFrame([
            {"动作": "买入", "代码": f"00000{rank}", "名称": str(rank), "优先级": rank,
             "综合评分": 100 - rank, "原始_mkt_market_score": market_score}
            for rank in (1, 2, 3)
        ]).to_csv(tmp_path / f"交易计划_{date}.csv", index=False)

    write_plan("20260601", 40)
    write_plan("20260602", 60)
    write_plan("20260603", 80)

    assert engine._load_trade_plans("20260601", str(tmp_path)).empty
    assert list(engine._load_trade_plans("20260602", str(tmp_path))["优先级"]) == [1]
    assert list(engine._load_trade_plans("20260603", str(tmp_path))["优先级"]) == [1, 2, 3]


def test_backtest_engine_state_roundtrip_for_daily_continuation():
    engine = BacktestEngine(data_manager=None)
    engine.config.initial_capital = 100_000
    engine.cash = 80_000
    engine.total_capital = 101_000
    engine.current_positions = {
        "000001": {
            "stock_name": "A",
            "entry_date": "20260622",
            "entry_price": 10,
            "shares": 2000,
            "cost_basis": 20_000,
            "market_value": 21_000,
            "pattern_type": "指标筛选/default",
            "highest_price": 10.5,
        }
    }
    engine.daily_nav = [{"date": "20260622", "cash": 80_000, "position_value": 21_000, "total_value": 101_000}]
    engine.trade_history = [
        TradeRecord(
            date="20260622", stock_code="000001", stock_name="A", pattern_type="指标筛选/default",
            action="BUY", entry_price=10, exit_price=0, shares=2000, position_size=20_000,
            pnl=0, pnl_pct=0, holding_days=0, hot_resonance=False, resonance_sectors="",
            entry_date="20260622", exit_reason="buy",
        )
    ]

    state = engine.export_state()
    restored = BacktestEngine(data_manager=None)
    restored.import_state(state)

    assert restored.cash == 80_000
    assert restored.total_capital == 101_000
    assert restored.daily_nav[-1]["date"] == "20260622"
    assert restored.current_positions["000001"]["shares"] == 2000
    assert restored.trade_history[0].stock_code == "000001"


def test_factor_feedback_uses_strong_weak_buckets():
    result = {
        "trade_history": [
            {
                "date": "20260618", "stock_code": "000001", "stock_name": "强A",
                "action": "SELL", "pnl": 1000, "pnl_pct": 0.1,
                "stop_loss_triggered": False, "take_profit_triggered": True,
                "plan_rank": 1, "plan_score": 80,
                "factor_metrics_json": json.dumps({"stk_amount_ratio_5d": 82}),
            },
            {
                "date": "20260618", "stock_code": "000002", "stock_name": "强B",
                "action": "SELL", "pnl": 800, "pnl_pct": 0.08,
                "stop_loss_triggered": False, "take_profit_triggered": True,
                "plan_rank": 2, "plan_score": 78,
                "factor_metrics_json": json.dumps({"stk_amount_ratio_5d": 78}),
            },
            {
                "date": "20260618", "stock_code": "000003", "stock_name": "弱A",
                "action": "SELL", "pnl": -500, "pnl_pct": -0.05,
                "stop_loss_triggered": True, "take_profit_triggered": False,
                "plan_rank": 3, "plan_score": 70,
                "factor_metrics_json": json.dumps({"stk_amount_ratio_5d": 30}),
            },
            {
                "date": "20260618", "stock_code": "000004", "stock_name": "弱B",
                "action": "SELL", "pnl": -400, "pnl_pct": -0.04,
                "stop_loss_triggered": True, "take_profit_triggered": False,
                "plan_rank": 3, "plan_score": 69,
                "factor_metrics_json": json.dumps({"stk_amount_ratio_5d": 35}),
            },
            {
                "date": "20260618", "stock_code": "000005", "stock_name": "弱C",
                "action": "SELL", "pnl": -300, "pnl_pct": -0.03,
                "stop_loss_triggered": True, "take_profit_triggered": False,
                "plan_rank": 3, "plan_score": 68,
                "factor_metrics_json": json.dumps({"stk_amount_ratio_5d": 42}),
            },
        ]
    }

    feedback = build_attribution_frames(result)["factor_feedback"]
    amount = feedback[feedback["factor_id"] == "stk_amount_ratio_5d"].iloc[0]

    assert amount["strong_count"] == 2
    assert amount["strong_total_pnl"] == 1800
    assert amount["weak_count"] == 3
    assert amount["weak_total_pnl"] == -1200
    assert amount["weak_stop_loss_rate"] == 1
    assert amount["feedback"] == "弱项导致止损"
