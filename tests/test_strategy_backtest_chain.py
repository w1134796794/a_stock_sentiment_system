import csv
import json

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine, TradeRecord
from backtest.minute_entry import ENTRY_ACCELERATION, ENTRY_CONTINUATION, ENTRY_WEAK
from backtest.plan_source import build_backtest_plan_dir
from run_backtest import save_backtest_results


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _screening(strategy_id, name, code, score):
    return {
        "strategy_id": strategy_id,
        "strategy_name": name,
        "strategy_version": f"{strategy_id}-v1",
        "strategy_execution": {
            "allowed_entry_modes": ["weak_to_strong"],
            "confirmation_deadline": "10:00:00",
            "candidate_max_age_days": 1,
        },
        "position_cap_pct": 8,
        "final": [{
            "code": code,
            "name": f"股票{code}",
            "score": score,
            "rank": 1,
            "metrics": {"tech_score": 80},
            "context": {"pct_chg": 5.0},
        }],
    }


def test_strategy_backtest_plan_uses_selected_combination_outputs_only(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    screening_dir = tmp_path / "screening"
    output_dir = tmp_path / "webdata"
    _write_json(snapshot_dir / "20260701.json", {"trade_date": "20260701"})
    _write_json(
        screening_dir / "combinations" / "weak_to_strong" / "screening_20260701.json",
        _screening("weak_to_strong", "弱转强修复", "000001", 81),
    )
    trend = _screening("trend_follow", "趋势主升", "000001", 76)
    trend["strategy_execution"]["allowed_entry_modes"] = ["continuation"]
    _write_json(
        screening_dir / "combinations" / "trend_follow" / "screening_20260701.json",
        trend,
    )
    _write_json(
        screening_dir / "screening_20260701.json",
        _screening("default", "不应被读取", "600000", 99),
    )

    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=output_dir,
        screening_dir=screening_dir,
        start_date="20260701",
        end_date="20260701",
        strategy_ids=["weak_to_strong", "trend_follow"],
    )

    assert file_count == 1
    assert row_count == 1
    with (plan_dir / "交易计划_20260701.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["代码"] == "000001"
    assert rows[0]["策略ID"] == "weak_to_strong"
    assert rows[0]["策略来源"] == "weak_to_strong,trend_follow"
    assert rows[0]["策略名称"] == "弱转强修复"
    execution = json.loads(rows[0]["策略执行"])
    assert execution["allowed_entry_modes"] == ["weak_to_strong", "continuation"]


def test_default_strategy_can_read_legacy_root_screening_artifact(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    screening_dir = tmp_path / "screening"
    output_dir = tmp_path / "webdata"
    _write_json(snapshot_dir / "20260701.json", {"trade_date": "20260701"})
    _write_json(
        screening_dir / "screening_20260701.json",
        _screening("default", "默认短线综合", "600000", 82),
    )

    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=output_dir,
        screening_dir=screening_dir,
        start_date="20260701",
        end_date="20260701",
        strategy_ids=["default"],
    )

    assert file_count == 1
    assert row_count == 1
    with (plan_dir / "交易计划_20260701.csv").open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["代码"] == "600000"
    assert row["策略ID"] == "default"


def test_production_backtest_uses_the_same_deduplicated_decision_pool(tmp_path):
    snapshot_dir = tmp_path / "snapshots"
    screening_dir = tmp_path / "screening"
    output_dir = tmp_path / "webdata"
    _write_json(snapshot_dir / "20260701.json", {"trade_date": "20260701"})
    strategy_ids = [
        "mainline_leader",
        "weak_to_strong",
        "first_board_launch",
    ]
    for index, strategy_id in enumerate(strategy_ids):
        payload = _screening(strategy_id, strategy_id, "000001", 86 - index)
        payload["weight_metadata"] = {
            "market_state_snapshot": {
                "score": 75,
                "regime": "strong",
                "phase": "active",
                "position_scale": 0.8,
            },
            "requested_weight_source": "manual",
        }
        payload["final"][0].update({
            "confidence_grade": "B",
            "expected_return_pct": 0.8,
            "decision_status": "usable",
            "resonance_sectors": "机器人,自动化",
            "context": {
                "sector_mainline_score": 75,
                "sector_resonance_score": 72,
            },
            "metrics": {
                "stk_sector_persistence_score": 70,
                "stk_capital_flow_consensus": 68,
            },
        })
        _write_json(
            screening_dir / "combinations" / strategy_id / "screening_20260701.json",
            payload,
        )

    plan_dir, file_count, row_count = build_backtest_plan_dir(
        snapshot_dir=snapshot_dir,
        output_dir=output_dir,
        screening_dir=screening_dir,
        start_date="20260701",
        end_date="20260701",
        strategy_ids=strategy_ids,
    )

    assert file_count == 1
    assert row_count == 1
    with (plan_dir / "交易计划_20260701.csv").open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["代码"] == "000001"
    assert row["模式"] == "生产决策池"
    assert row["行动分组"] in {"重点确认", "盘中观察"}
    assert set(row["策略来源"].split(",")) == {
        "mainline_leader", "weak_to_strong", "first_board_launch",
    }
    assert json.loads(row["策略执行"])["allowed_entry_modes"]


def test_backtest_result_keeps_strategy_provenance_in_trade_csv(tmp_path):
    trade = TradeRecord(
        date="20260702", stock_code="000001", stock_name="策略股", pattern_type="指标筛选/default",
        action="SELL", entry_price=10.0, exit_price=11.0, shares=100,
        position_size=1000, pnl=95, pnl_pct=0.095, holding_days=1,
        hot_resonance=False, resonance_sectors="芯片",
        strategy_id="weak_to_strong", strategy_name="弱转强修复",
        strategy_version="abc123", strategy_sources="weak_to_strong,default",
    )
    save_backtest_results(
        {
            "trade_history": [trade], "current_positions": {}, "daily_nav": [],
            "total_return": 0, "annualized_return": 0, "sharpe_ratio": 0,
            "max_drawdown": 0, "win_rate": 0, "profit_loss_ratio": 0,
            "total_trades": 1, "initial_capital": 1000, "final_capital": 1095,
        },
        str(tmp_path), timestamp="strategy_chain",
    )

    path = tmp_path / "backtest_results" / "backtest_trades_strategy_chain.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["strategy_id"] == "weak_to_strong"
    assert row["strategy_name"] == "弱转强修复"
    assert row["strategy_sources"] == "weak_to_strong,default"


def test_backtest_normalizes_strategy_entry_mode_aliases():
    engine = BacktestEngine(None, BacktestConfig(entry_mode="hybrid"))
    plan = pd.Series({
        "策略执行": json.dumps({
            "allowed_entry_modes": ["weak_to_strong", "continuation"],
        }),
    })

    assert engine._entry_mode_for_plan(plan, 0.005) == ENTRY_WEAK
    assert engine._entry_mode_for_plan(plan, 0.03) == ENTRY_CONTINUATION
    assert engine._entry_mode_for_plan(plan, 0.06) is None

    acceleration_plan = pd.Series({
        "策略执行": json.dumps({"allowed_entry_modes": ["acceleration"]}),
    })
    assert engine._entry_mode_for_plan(acceleration_plan, 0.06) == ENTRY_ACCELERATION
