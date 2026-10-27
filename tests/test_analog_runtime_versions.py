from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from core.operations.ledger import stable_id
from core.signals.analog_runtime import query_record
from core.signals.board_feedback import FEEDBACK_VERSION, feedback_policy


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('production', [False, True])
def test_query_versions_match_initialized_engine_without_constructing_another(monkeypatch, enabled, production):
    config = BacktestConfig(production_rules=production, three_board_feedback_enabled=enabled,
                            three_board_feedback_score_penalty=35, commission_rate=.0005)
    execution = {'exit': {'hard_stop_loss': .04, 'time_stop_days': 3}}
    engine = BacktestEngine(None, config, frozen_context={'calendar': None, 'profiles': {}, 'risk_config': {}})
    expected_cost = engine._cost_version()
    expected_exit = engine._exit_rule_version({'strategy_execution': execution})

    def forbidden(*args, **kwargs):
        pytest.fail('Version hashing must not initialize an engine or market dependencies')

    monkeypatch.setattr(BacktestEngine, '__init__', forbidden)
    import backtest.backtest_engine as module
    monkeypatch.setattr(module, 'TradeCalendar', forbidden)
    monkeypatch.setattr(module, 'MinuteEntryEvaluator', forbidden)
    monkeypatch.setattr(module, 'MinuteAmountProfileRepository', forbidden)
    case = {'backtest_config': asdict(config), 'strategy_execution': execution}
    result = query_record(case)
    assert result['case'] is case
    assert result['versions']['cost_version'] == expected_cost
    assert result['versions']['exit_rule_version'] == expected_exit


@pytest.mark.parametrize('evaluator', [None, SimpleNamespace()])
def test_exit_version_accepts_missing_evaluator_or_missing_feedback_policy(evaluator):
    engine = object.__new__(BacktestEngine)
    engine.config = BacktestConfig(three_board_feedback_enabled=True)
    if evaluator is not None:
        engine.minute_evaluator = evaluator
    assert engine._exit_rule_version({}) == stable_id(
        'engine_exit_v2', engine._position_exit_config({}), FEEDBACK_VERSION,
        feedback_policy(engine.config), engine.config.exit_policy_mode, engine.config.daily_ohlc_path_policy)


def test_exit_version_prefers_frozen_evaluator_policy_without_evaluating_config_fallback(monkeypatch):
    engine = object.__new__(BacktestEngine)
    engine.config = BacktestConfig(three_board_feedback_enabled=False)
    frozen = feedback_policy(replace(engine.config, three_board_feedback_enabled=True))
    engine.minute_evaluator = SimpleNamespace(board_feedback_policy=frozen)

    def forbidden(*args, **kwargs):
        pytest.fail('An existing frozen policy must not consult the fallback configuration')

    monkeypatch.setattr('core.signals.board_feedback.feedback_policy', forbidden)
    assert engine._exit_rule_version({}) == stable_id(
        'engine_exit_v2', engine._position_exit_config({}), FEEDBACK_VERSION, frozen,
        engine.config.exit_policy_mode, engine.config.daily_ohlc_path_policy)


@pytest.mark.parametrize('resolved', ['strategy', ''])
def test_query_version_resolves_oos_exit_policy_without_mutating_case(resolved):
    config = BacktestConfig(exit_policy_mode='oos_selected')
    case = {'backtest_config': asdict(config), 'resolved_exit_policy': {'policy': resolved}}
    expected = query_record({'backtest_config': asdict(replace(config, exit_policy_mode='strategy'))})
    assert query_record(case)['versions'] == expected['versions']
    assert case['backtest_config']['exit_policy_mode'] == 'oos_selected'


def test_query_without_config_keeps_empty_versions():
    case = {'case_id': 'without-config'}
    assert query_record(case) == {'case': case, 'versions': {}}
