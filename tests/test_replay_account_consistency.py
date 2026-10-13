import json

import pandas as pd
import pytest

from backtest.backtest_engine import BacktestConfig, BacktestEngine, TradeRecord
from backtest.performance_analyzer import PerformanceAnalyzer
from backtest.plan_source import _to_backtest_row, build_backtest_plan_dir
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.paper_trading_service import PaperTradingService
from run_backtest import save_backtest_results


def test_production_gate_does_not_reapply_legacy_absolute_score(tmp_path):
    rows = []
    for code, source, allowed, market, score in [
        ('000001', 'production_decision_v1', True, 38, 46),
        ('000002', 'production_decision_v1', False, 75, 99),
        ('000003', 'legacy', True, 38, 99),
        ('000005', 'legacy', True, 75, 46),
    ]:
        rows.append({'代码': code, '名称': '测试股份', '动作': '买入', '综合评分': score,
                     '原始_mkt_market_score': market, '入场门控来源': source,
                     '生产决策允许入场': allowed, '策略执行': json.dumps({'allowed_entry_modes': ['weak_to_strong']})})
    pd.DataFrame(rows).to_csv(tmp_path / '交易计划_20260901.csv', index=False)
    engine = BacktestEngine(None, BacktestConfig(entry_mode='hybrid'))
    result = engine._load_trade_plans('20260901', str(tmp_path))
    assert result['代码'].tolist() == [1, 5]
    assert {x['reason_code'] for x in engine.entry_attempts} == {'production_entry_ineligible', 'market_too_weak'}


def test_plan_preserves_production_contract():
    row = _to_backtest_row({'code': '000001', 'name': '测试股份',
                            '入场门控来源': 'production_decision_v1', '生产决策允许入场': True})
    assert row['入场门控来源'] == 'production_decision_v1'
    assert row['生产决策允许入场'] is True


@pytest.mark.parametrize('code,expected', [('920298', '920298.BJ'), ('830001', '830001.BJ'),
                                         ('430001', '430001.BJ'), ('600001', '600001.SH')])
def test_backtest_uses_shared_exchange_codes(code, expected):
    assert BacktestEngine(None)._standardize_stock_code(code) == expected


def test_day_executes_signals_chronologically_without_future_exit_cash(monkeypatch):
    engine = BacktestEngine(None, BacktestConfig(entry_mode='hybrid', commission_rate=0,
                            stamp_duty_rate=0, slippage=0, time_stop_days=999))
    engine.cash = 0
    engine.total_capital = 1000
    engine.current_positions['000001'] = {'stock_name': '持仓股份', 'entry_date': '20260630',
        'entry_price': 10, 'shares': 100, 'cost_basis': 1000, 'market_value': 1000,
        'pattern_type': 'test', 'hot_resonance': False, 'resonance_sectors': '',
        'stop_loss_price': 9.5, 'highest_price': 10, 'last_close': 10}
    bars = pd.DataFrame({'time': ['09:31:00', '09:44:00', '13:00:00'],
                         'open': [10, 10, 9], 'high': [10, 10, 9],
                         'low': [10, 10, 9], 'close': [10, 10, 9]})
    monkeypatch.setattr(engine, '_load_exit_minute_bars', lambda *args: bars)
    monkeypatch.setattr(engine, '_get_stock_daily_bar', lambda *args: {'open': 10, 'high': 10, 'low': 9, 'close': 9, 'pre_close': 10})
    plans = pd.DataFrame([{'代码': '000003', '名称': '晚信号', 'at': '13:01:00'},
                          {'代码': '000002', '名称': '早信号', 'at': '09:45:00'}])
    monkeypatch.setattr(engine, '_load_trade_plans', lambda *args: plans)
    monkeypatch.setattr(engine, '_prefetch_entry_data', lambda *args: None)
    monkeypatch.setattr(engine, '_get_prev_trade_date', lambda *args: '20260630')
    monkeypatch.setattr(engine, '_calculate_daily_nav', lambda *args: None)
    def check(plan, date, code, name):
        engine._last_entry_meta[code] = {'entry_time': plan['at']}
        return True, 10
    monkeypatch.setattr(engine, '_check_buy_conditions', check)
    observations = []
    monkeypatch.setattr(engine, '_execute_buy', lambda plan, date, **kwargs: observations.append((plan['代码'], engine.cash, engine.total_capital)))
    engine._process_date('20260701', '')
    assert observations == [('000002', 0, 1000), ('000003', 900, 900)]
    assert engine.trade_history[0].exit_time == '13:00:00'


def test_historical_reference_adjustment_preserves_cash_and_fractional_quantity(tmp_path):
    repository = HoldingRepository(tmp_path / 'adjustment.sqlite')
    service = PaperTradingService(repository, initial_capital=100000)
    repository.ensure_account('history_adjustment', name='History', initial_capital=100000)
    imported = dict(buy=0, sell=0, skipped=0)
    buy = dict(stock_code='000001', stock_name='Test', action='BUY', date='20260701',
               entry_date='20260701', entry_time='09:45:00', entry_price=10,
               shares=100, position_size=1000.3, pnl=0)
    service._import_trade(buy, 'adjustment', imported, account_key='history_adjustment')
    adjusted_shares = 140.25
    gross = adjusted_shares * 8
    pnl = gross - 1.5 - 1000.3
    sell = {**buy, 'action': 'SELL', 'date': '20260702', 'exit_time': '10:15:00',
            'shares': adjusted_shares, 'exit_price': 8, 'pnl': pnl}
    service._import_trade(sell, 'adjustment', imported, account_key='history_adjustment')
    assert imported == dict(buy=1, sell=1, skipped=0)
    assert repository.account('history_adjustment')['cash'] == pytest.approx(100000 + pnl)
    assert not repository.list_positions('history_adjustment')
    with repository._connect() as conn:
        row = dict(conn.execute("SELECT * FROM portfolio_trades WHERE account_key=? AND action='sell'",
                                ('history_adjustment',)).fetchone())
    assert row['shares'] == adjusted_shares
    assert row['price'] == 8
    assert row['amount'] == pytest.approx(gross)
    assert row['realized_pnl'] == pytest.approx(pnl)
    assert json.loads(row['metadata_json'])['original_entry_shares'] == 100


def test_reference_adjusted_exit_cannot_bypass_t_plus_one(tmp_path):
    repository = HoldingRepository(tmp_path / 'adjustment.sqlite')
    repository.ensure_account('history_adjustment', name='History', initial_capital=100000)
    position = repository.open_position(dict(code='000001', name='Test', entry_date='20260701',
                                            entry_price=10, shares=100), 'history_adjustment')
    with pytest.raises(ValueError, match='T\\+1'):
        repository.sell_replay_position(position['id'], dict(trade_date='20260701', price=8,
            shares=140.25, fees=0, cost_basis=1000, expected_pnl=122))
    assert repository.account('history_adjustment')['cash'] == 99000


def test_export_import_preserves_exact_costs_and_sell_time(tmp_path, monkeypatch):
    import core.portfolio.paper_trading_service as module
    trade_base = dict(stock_code='000001', stock_name='测试股份', pattern_type='test',
                      entry_price=10, shares=100, position_size=1000.3, holding_days=1,
                      hot_resonance=False, resonance_sectors='', entry_date='20260701', entry_time='09:45:00')
    buys = TradeRecord(**trade_base, date='20260701', action='BUY', exit_price=0, pnl=0, pnl_pct=0)
    sells = TradeRecord(**trade_base, date='20260702', action='SELL', exit_price=11,
                       pnl=98.27, pnl_pct=98.27/1000.3, exit_time='10:15:00')
    save_backtest_results({'trade_history': [buys, sells], 'initial_capital': 100000,
                          'final_capital': 100098.27}, str(tmp_path), timestamp='cost_contract')
    monkeypatch.setattr(module, 'OUTPUT_DIR', tmp_path)
    repository = HoldingRepository(tmp_path / 'portfolio.sqlite')
    service = PaperTradingService(repository, initial_capital=100000)
    result = service.import_backtest_run('cost_contract', account_key='history_test')
    assert result['costs_verified'] is True
    assert repository.account('history_test')['cash'] == pytest.approx(100098.27)
    trades = repository.list_trades('history_test')
    assert sum(t['fees'] for t in trades) == pytest.approx(1.73)
    assert trades[0]['trade_time'] == '10:15:00'


def test_bad_cost_contract_rejected_before_account_reset(tmp_path, monkeypatch):
    import core.portfolio.paper_trading_service as module
    result_dir = tmp_path / 'backtest_results'
    result_dir.mkdir()
    pd.DataFrame([{'stock_code': '000001', 'action': 'BUY'}]).to_csv(result_dir / 'backtest_trades_bad.csv', index=False)
    (result_dir / 'backtest_manifest_bad.json').write_text('{}')
    monkeypatch.setattr(module, 'OUTPUT_DIR', tmp_path)
    repository = HoldingRepository(tmp_path / 'portfolio.sqlite')
    service = PaperTradingService(repository, initial_capital=100000)
    with pytest.raises(ValueError, match='成本金额'):
        service.import_backtest_run('bad', account_key='history_new')
    assert not repository.account('history_new')


def test_first_day_loss_and_sharpe_have_identical_conventions():
    engine = BacktestEngine(None, BacktestConfig(initial_capital=1000))
    engine.daily_nav = [{'date': '20260701', 'total_value': 900}, {'date': '20260702', 'total_value': 950}]
    engine.total_capital = 950
    base = dict(stock_code='000001', stock_name='测试股份', pattern_type='test', entry_price=10,
                shares=100, position_size=1000, holding_days=1, hot_resonance=False, resonance_sectors='')
    engine.trade_history = [TradeRecord(**base, date='20260702', action='SELL', exit_price=9.5, pnl=-50, pnl_pct=-.05)]
    result = engine._generate_backtest_report()
    analysis = PerformanceAnalyzer().calculate_all_metrics(result)
    assert result['max_drawdown'] == pytest.approx(-.1)
    assert analysis['risk']['max_drawdown'] == pytest.approx(-.1)
    assert analysis['returns']['sharpe_ratio'] == pytest.approx(result['sharpe_ratio'])


def test_state_restore_clears_intraday_exit_cursor():
    engine = BacktestEngine(None)
    engine._exit_scan_times[('20260701', '000001')] = '13:00:00'
    engine.import_state(engine.export_state())
    assert not engine._exit_scan_times


def test_entry_day_nav_marks_close_without_t_plus_zero_sale(monkeypatch):
    engine = BacktestEngine(None, BacktestConfig(initial_capital=1000))
    engine.cash = 499.85
    engine.current_positions['000001'] = {'entry_date': '20260701', 'entry_price': 5,
        'shares': 100, 'market_value': 500, 'last_close': 5}
    monkeypatch.setattr(engine, '_is_full_day_suspended', lambda *args, **kwargs: False)
    monkeypatch.setattr(engine, '_get_stock_daily_bar', lambda *args: {'close': 4})
    engine._calculate_daily_nav('20260701')
    assert engine.daily_nav[-1]['total_value'] == pytest.approx(899.85)
    assert engine.current_positions['000001']['market_value'] == 400
    assert engine.current_positions['000001']['last_close'] == 4
    assert not engine.trade_history


def test_nav_keeps_suspended_value_and_blocks_unknown_close(monkeypatch):
    from backtest.evidence_policy import ReplayEvidenceBlocked
    engine = BacktestEngine(None, BacktestConfig(replay_missing_evidence_policy='strict'))
    engine.current_positions['000001'] = {'shares': 140.25, 'market_value': 1122}
    monkeypatch.setattr(engine, '_is_full_day_suspended', lambda *args, **kwargs: True)
    monkeypatch.setattr(engine, '_get_stock_daily_bar', lambda *args: None)
    engine._calculate_daily_nav('20260701')
    assert engine.daily_nav[-1]['position_value'] == 1122
    monkeypatch.setattr(engine, '_is_full_day_suspended', lambda *args, **kwargs: False)
    with pytest.raises(ReplayEvidenceBlocked):
        engine._calculate_daily_nav('20260702')
    assert len(engine.daily_nav) == 1
    monkeypatch.setattr(engine, '_get_stock_daily_bar', lambda *args: {'close': 8.5})
    engine._calculate_daily_nav('20260702')
    assert engine.daily_nav[-1]['position_value'] == pytest.approx(140.25 * 8.5)


def test_no_closed_trades_does_not_hide_imported_holdings_nav_loss():
    engine = BacktestEngine(None, BacktestConfig(initial_capital=1000))
    engine.daily_nav = [{'date': '20260701', 'total_value': 900}]
    engine.total_capital = 900
    result = engine._generate_backtest_report()
    assert result['total_return'] == pytest.approx(-.1)
    assert result['max_drawdown'] == pytest.approx(-.1)


def test_first_replay_date_includes_previous_candidate_plan(tmp_path):
    snapshots = tmp_path / 'snapshots'
    snapshots.mkdir()
    for date in ['20260629', '20260630', '20260701']:
        payload = {'trade_date': date, 'etl': {'screening': {'final': [
            {'code': '000001', 'name': '测试股份', 'score': 90, 'rank': 1}]}}}
        (snapshots / f'{date}.json').write_text(json.dumps(payload), encoding='utf-8')
    plan_dir, count, _ = build_backtest_plan_dir(snapshot_dir=snapshots, output_dir=tmp_path / 'out',
                                              start_date='20260701', end_date='20260701')
    assert count == 2
    assert (plan_dir / '交易计划_20260630.csv').exists()
    assert not (plan_dir / '交易计划_20260629.csv').exists()


def test_old_account_exposes_unverified_cost_warning(tmp_path, monkeypatch):
    import core.portfolio.paper_trading_service as module
    directory = tmp_path / 'backtest_results'
    directory.mkdir()
    (directory / 'backtest_data_quality_old.json').write_text(json.dumps({'status': 'complete', 'full_sample_metrics_valid': True}))
    pd.DataFrame([{'action': 'BUY', 'entry_price': 10, 'shares': 100}]).to_csv(directory / 'backtest_trades_old.csv', index=False)
    monkeypatch.setattr(module, 'OUTPUT_DIR', tmp_path)
    service = object.__new__(PaperTradingService)
    quality = service.rebuild_data_quality('old')
    assert quality['status'] == 'incomplete'
    assert not quality['costs_verified']
    assert not quality['full_sample_metrics_valid']
    assert '手续费' in quality['cost_warning']
