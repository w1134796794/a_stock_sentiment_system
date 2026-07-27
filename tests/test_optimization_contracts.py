from __future__ import annotations

import math
import json

from backtest.backtest_engine import BacktestConfig
from backtest.exit_policy import ExitPolicyRepository, resolve_exit_config
from backtest.run_audit import (
    build_entry_funnel,
    build_entry_opportunity_summary,
    build_run_manifest,
    stable_hash,
)
from core.factors.factor_library import FactorLibraryTrainer, _FACTOR_WIDE_COLUMNS
from core.factors.jobs.gold_utils import make_long_record
from core.factors.strategy_training import STRATEGY_TRAINING_SPECS
from core.etl.factor_store_maintenance import FactorStoreMaintenance
from core.models.market_state import MarketStateSnapshot
from core.portfolio.strategy_allocator import AllocationConfig, StrategyPortfolioAllocator
from core.screening.strategy_profiles import StrategyProfileRepository
from risk.risk_config import RiskConfig


def test_backtest_projects_unified_risk_values_without_hidden_clamps():
    risk = RiskConfig(
        hard_stop_loss=0.071,
        trailing_stop=0.123,
        trailing_activation=0.067,
        market_entry_threshold=47.0,
        market_active_threshold=63.0,
        market_strong_threshold=72.0,
        min_open_gap=-0.01,
        max_open_gap=0.06,
    )

    config = BacktestConfig.from_risk_config(risk)

    assert config.stop_loss_pct == 0.071
    assert config.trailing_stop_pct == 0.123
    assert config.trailing_activation_pct == 0.067
    assert config.market_entry_threshold == 47.0
    assert config.market_strong_threshold == 63.0
    assert config.market_hot_threshold == 72.0
    assert config.min_open_gap == -0.01
    assert config.max_open_gap == 0.06


def test_market_state_uses_one_threshold_contract():
    assert MarketStateSnapshot.resolve(20, weak_threshold=45, strong_threshold=70).regime == "weak"
    assert MarketStateSnapshot.resolve(60, weak_threshold=45, strong_threshold=70).regime == "neutral"
    assert MarketStateSnapshot.resolve(75, weak_threshold=45, strong_threshold=70).regime == "strong"


def test_relative_regime_model_cannot_label_an_18_point_market_as_strong():
    model = {
        "status": "trained",
        "method": "hmm",
        "feature_mean": [18.2, 0.0],
        "feature_std": [1.0, 1.0],
        "means": [[0.0, 0.0], [10.0, 10.0], [-10.0, -10.0]],
        "covars": [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]],
        "transmat": [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        "last_state": 0,
        "state_labels": {"0": "strong", "1": "neutral", "2": "weak"},
    }

    state = MarketStateSnapshot.resolve(
        18.2, weak_threshold=50, strong_threshold=70, regime_model=model,
    )

    assert state.regime == "weak"
    assert state.context["model_regime"] == "strong"
    assert state.context["model_regime_disagrees"] is True


def test_long_factor_record_preserves_missing_value_and_freshness():
    row = make_long_record(
        trade_date="20260717", entity_type="stock", entity_id="000001",
        factor_id="test_missing", raw_value=float("nan"), score=0,
    )
    assert row["raw_value"] is None
    assert row["score"] is None
    assert row["is_missing"] == 1
    assert row["freshness_date"] == "20260717"


def test_strategy_profiles_have_independent_exit_rules():
    repository = StrategyProfileRepository()
    board = repository.get_profile("ultra_short_board")["execution"]["exit"]
    trend = repository.get_profile("trend_follow")["execution"]["exit"]
    assert board["time_stop_days"] < trend["time_stop_days"]
    assert board["trailing_stop"] < trend["trailing_stop"]


def test_entry_funnel_and_manifest_are_reproducible():
    attempts = [
        {"strategy_id": "weak_to_strong", "stage": "market_gate", "status": "rejected", "reason_code": "weak_market"},
        {"strategy_id": "weak_to_strong", "stage": "entry_signal", "status": "confirmed", "reason_code": "confirmed"},
        {"strategy_id": "trend_follow", "stage": "fill", "status": "filled", "reason_code": "filled"},
    ]
    funnel = build_entry_funnel(attempts)
    assert sum(int(row["count"]) for row in funnel) == 3
    manifest = build_run_manifest(
        result={"backtest_config": {"slippage": 0.002}, "entry_attempts": attempts},
        metadata={"start_date": "20260101", "end_date": "20260701"},
    )
    semantic = {key: manifest[key] for key in (
        "backtest_config", "config_files", "strategy_versions", "entry_mode",
        "start_date", "end_date", "model_versions", "data_cutoff", "costs",
    )}
    assert manifest["configuration_hash"] == stable_hash(semantic)
    assert manifest["strategy_versions"] == [
        ("trend_follow", ""), ("weak_to_strong", ""),
    ]


def test_entry_opportunity_summary_separates_signal_from_account_acceptance():
    attempts = [
        {"date": "20260701", "stock_code": "000001", "strategy_id": "first_board_launch",
         "stage": "entry_signal", "status": "filled"},
        {"date": "20260701", "stock_code": "000001", "strategy_id": "first_board_launch",
         "stage": "portfolio_gate", "status": "rejected"},
        {"date": "20260701", "stock_code": "000002", "strategy_id": "mainline_leader",
         "stage": "entry_signal", "status": "signal_unfilled"},
    ]

    summary = build_entry_opportunity_summary(
        attempts, candidate_count=10, executed_buys=0, closed_trades=0,
    )

    assert summary == {
        "candidate_count": 10,
        "signal_count": 2,
        "fillable_signal_count": 1,
        "signal_unfilled_count": 1,
        "account_rejected_after_signal_count": 1,
        "executed_buy_count": 0,
        "closed_trade_count": 0,
    }


def test_allocator_counts_independent_evidence_and_caps_tail_loss():
    allocator = StrategyPortfolioAllocator(AllocationConfig(
        max_positions=4, max_total_weight=0.8, max_stock_weight=0.4,
        max_sector_weight=0.8, min_position_weight=0.01,
        max_expected_tail_loss=0.02,
    ))
    rows = [
        {"代码": "000001", "策略ID": "ultra_short_board", "综合评分": 90, "优先级": 1, "平均MAE%": -10},
        {"代码": "000001", "策略ID": "first_board_launch", "综合评分": 88, "优先级": 1, "平均MAE%": -10},
        {"代码": "000001", "策略ID": "capital_resonance", "综合评分": 86, "优先级": 1, "平均MAE%": -10},
    ]
    result = allocator.allocate(rows)[0]
    assert result["策略共识数"] == 3
    assert result["独立证据共识数"] == 2
    assert result["组合累计压力损失%"] <= 2.01
    assert math.isclose(result["组合建议仓位%"], 20.0, abs_tol=0.01)


def test_all_strategy_training_factors_use_materialized_wide_columns():
    trainer = FactorLibraryTrainer()
    missing = {}
    for strategy_id in STRATEGY_TRAINING_SPECS:
        factors = trainer.prior_weights(strategy_id)
        unavailable = sorted(
            factor for factor in factors
            if factor != "tech_score" and factor not in _FACTOR_WIDE_COLUMNS
        )
        if unavailable:
            missing[strategy_id] = unavailable
    assert missing == {}


def test_exit_policy_repository_only_uses_prior_oos_passed_versions(tmp_path):
    path = tmp_path / "exit.json"
    path.write_text(json.dumps({"policies": [
        {"strategy_id": "trend_follow", "effective_date": "20260601", "policy": "atr_stop", "oos_passed": True},
        {"strategy_id": "trend_follow", "effective_date": "20260720", "policy": "fixed_stop", "oos_passed": True},
        {"strategy_id": "trend_follow", "effective_date": "20260501", "policy": "structure_stop", "oos_passed": False},
    ]}), encoding="utf-8")

    selected = ExitPolicyRepository(path).resolve("trend_follow", "20260701")
    assert selected["policy"] == "atr_stop"
    assert resolve_exit_config({"hard_stop_loss": 0.05}, policy="atr_stop", factor_context={"atr_14_pct": 4})["hard_stop_loss"] == 0.05


def test_factor_store_maintenance_dry_run_is_non_destructive(tmp_path):
    import duckdb

    db = tmp_path / "factors.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE factor_value_long(trade_date VARCHAR, factor_id VARCHAR, score DOUBLE)")
    con.execute("INSERT INTO factor_value_long VALUES ('20250101','tech_score',80),('20260701','tech_score',90)")
    con.close()
    service = FactorStoreMaintenance(db_path=db, archive_dir=tmp_path / "archive")

    result = service.archive_before("20260101", dry_run=True, prune=True)

    assert result.rows_selected == 1
    assert not (tmp_path / "archive").exists()
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM factor_value_long").fetchone()[0] == 2
    con.close()
