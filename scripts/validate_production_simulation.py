"""Compare real cached minute evidence and exercise an isolated paper account."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def resume_execution(args):
    import pandas as pd
    from backtest.minute_entry import normalize_minute_bars
    from backtest.trade_calendar import TradeCalendar
    from config.settings import CACHE_DIR, PAPER_INITIAL_CAPITAL, TUSHARE_TOKEN
    from core.data import DataManager
    from core.data.minute_quality import inspect_minute_bars
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.incremental_replay import IncrementalPaperReplay
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.portfolio.position_monitor import PositionMonitor
    from core.utils.stock_code_utils import StockCodeUtils

    path = args.output / 'simulation_validation.json'
    report = json.loads(path.read_text(encoding='utf-8'))
    db = Path(report['isolated_database']).resolve()
    if not db.is_relative_to(args.output.resolve()) or not db.name.startswith('simulation_'):
        raise ValueError('Execution regression must use its own isolated database')
    if report['date'] != args.date or report['config_version'] != IncrementalPaperReplay.config_version():
        raise ValueError('Execution evidence date or configuration changed')
    repository = HoldingRepository(db)
    position = repository.list_positions()[0]
    metadata = position['metadata']
    code = position['code']
    ts_code = StockCodeUtils.standardize_code(code, add_suffix=True)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
    calendar = TradeCalendar()
    previous = dm.get_stock_daily_data(ts_code, calendar.prev(args.date)) or {}
    quotes = {code: dict(code=code, name=position['name'], date=args.date,
        time=position['entry_time'], last_price=metadata['reference_signal_price'],
        pre_close=previous.get('close'), is_stale=False, source='historical_minute_adapter_not_live')}
    adapter = SimpleNamespace(get_quotes=lambda codes: {'quotes': [quotes[c] for c in codes if c in quotes]})
    paper = PaperTradingService(repository, quote_service=adapter)
    repeated = paper.process_realtime_payload({'profile': 'production', 'market_date': args.date,
        'rows': [dict(code=code, name=position['name'], confirm_status='confirmed',
            entry_price=metadata['reference_signal_price'], entry_time=position['entry_time'],
            signal_id=metadata['signal_id'])]})
    report['paper_repeat'] = repeated
    report['checks'].update(buy_executed=True, duplicate_not_executed=repeated['opened'] == 0,
        t_plus_one=repository.sellable_shares(position['id'], args.date) == 0,
        same_buy_price=position['entry_price'] == next(r['historical_price'] for r in report['comparisons'] if r['code'] == code))
    monitor = PositionMonitor(repository, quote_service=adapter,
        notifier=SimpleNamespace(notify_exit_signal=lambda *a, **kw: {'sent': 0, 'all_delivered': True, 'test_sink': True}),
        backend=MemoryStateBackend(), market_context_provider=lambda: {},
        sector_breadth_provider=SimpleNamespace(evaluate=lambda *a, **kw: (None, {'data_completeness': 0})))
    end = (datetime.strptime(args.date, '%Y%m%d') + timedelta(days=14)).strftime('%Y%m%d')
    for date in calendar.get_trade_dates(args.date, end)[1:]:
        if date >= datetime.now().strftime('%Y%m%d'):
            break
        minute_file = Path(CACHE_DIR) / 'stock' / 'tick' / f'{ts_code}_{date}.csv'
        raw = pd.read_csv(minute_file) if minute_file.exists() else pd.DataFrame()
        if args.fetch_holding_minutes and not inspect_minute_bars(raw, date, expected_code=code)['valid']:
            dm.allow_remote_minutes = True
            try:
                raw = dm.get_stock_tick(ts_code, date, require_full_day=True)
            finally:
                dm.allow_remote_minutes = False
        if not inspect_minute_bars(raw, date, expected_code=code)['valid']:
            report['exit_missing_date'] = date
            break
        daily = dm.get_stock_daily_data(ts_code, date) or {}
        high = position['entry_price']
        for bar in normalize_minute_bars(raw).to_dict('records'):
            high = max(high, bar['high'])
            quotes[code] = dict(code=code, name=position['name'], date=date, time=bar['time'],
                last_price=bar['close'], high_price=high, pre_close=daily.get('pre_close'),
                open_price=daily.get('open'), is_stale=False, source='historical_minute_adapter_not_live')
            outcome = monitor.run_once(signal_date=date, auto_execute=True)
            if not outcome['ok']:
                raise RuntimeError(outcome)
            if outcome.get('executed'):
                report['paper_exit'] = outcome
            if not repository.list_positions():
                break
        if not repository.list_positions():
            break
    trades = repository.list_trades(limit=1000)
    report['checks'].update(sell_executed=any(t['action'] == 'sell' for t in trades),
        fees_recorded=all(t['fees'] > 0 for t in trades),
        cash_reconciled=abs(repository.account()['cash'] - (PAPER_INITIAL_CAPITAL
            + sum((t['amount'] if t['action'] == 'sell' else -t['amount']) - t['fees'] for t in trades))) < 1e-6)
    report['paper_trades'] = trades
    report['candidate_count'] = len(report['comparisons'])
    report['verified_buy_samples'] = sum(r.get('historical_eligible') and r.get('paper_minute_eligible') for r in report['comparisons'])
    report['mismatches'] = [r for r in report['comparisons'] if r.get('same_verdict') is False
        or any(r.get(k) is False for k in ('same_confirm_time', 'same_entry_time', 'same_price'))]
    report['passed'] = not report['mismatches'] and all(report['checks'].values())
    report['resumed_execution'] = True
    report['limitations'] = ['Historical minute adapters do not verify live arrival, lease gates or WeChat delivery.',
        'Exit regression uses actual stock minutes with missing market/sector context explicitly unavailable.']
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('candidate_count', 'verified_buy_samples', 'checks', 'passed')}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report['passed'] else 2)


def main():
    import pandas as pd
    from backtest.backtest_engine import BacktestEngine
    from backtest.evidence_policy import ReplayEvidenceBlocked
    from backtest.minute_entry import normalize_minute_bars
    from backtest.plan_source import _to_backtest_row
    from config.settings import CACHE_DIR, PAPER_INITIAL_CAPITAL, TUSHARE_TOKEN
    from core.data import DataManager
    from core.data.minute_quality import inspect_minute_bars
    from core.infrastructure.shared_state import MemoryStateBackend
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.incremental_replay import IncrementalPaperReplay
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.portfolio.position_monitor import PositionMonitor
    from core.realtime.day_replay import DayReplayLoader, _ReplayEntryService
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    from core.signals.production_rules import RULESET_VERSION, execution_defaults
    from core.utils.price_limit import round_price

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fetch-holding-minutes', action='store_true')
    parser.add_argument('--resume-execution', action='store_true')
    args = parser.parse_args()
    if args.resume_execution:
        return resume_execution(args)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'date': args.date, 'ruleset': RULESET_VERSION,
              'classification': 'historical_minute_adapter_regression',
              'live_runtime_verified': False, 'wechat_sent': False,
              'default_account_modified': False, 'comparisons': [], 'checks': {}}

    def save():
        (args.output / 'simulation_validation.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    db = args.output / f'simulation_{uuid.uuid4().hex[:12]}.sqlite'
    repository = HoldingRepository(db)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
    dm.allow_remote_minutes = False
    replay = IncrementalPaperReplay(repository, data_manager=dm)
    config = replay._config(PAPER_INITIAL_CAPITAL)
    report['config_version'] = replay.config_version()
    report['config'] = asdict(config)
    report['isolated_database'] = str(db.resolve())
    engine = BacktestEngine(dm, config)
    engine._replay_progress = lambda item: print(json.dumps(item, ensure_ascii=False), flush=True) if item.get('completed', 0) % 100 == 0 else None
    engine._prepare_production_day(args.date)
    inputs = engine._production_inputs
    report['input_basis'] = inputs['input_basis']
    report['bundle_id'] = inputs['bundle_id']
    rows = inputs['rows']
    frames = {code: normalize_minute_bars(raw) for code, raw in inputs['frames'].items()}
    engine._minute_frames.update({(args.date, c): f for c, f in frames.items()})
    engine._auction_rows.update({(args.date, c): a for c, a in inputs['auctions'].items()})
    service = _ReplayEntryService(inputs['previous'], inputs['auctions'], inputs['members'],
                                  frames, engine.amount_profiles, inputs['opening_prices'], evaluator=engine.minute_evaluator,
                                  opening_evidence=inputs.get('opening_evidence'))
    verified = []
    for row in rows:
        code = row['code']
        raw = inputs['frames'].get(code, pd.DataFrame())
        quality = inspect_minute_bars(raw, args.date, expected_code=code)
        if not quality['valid']:
            report['comparisons'].append({'code': code, 'status': 'missing_minutes', 'reason': quality['reason']})
            continue
        frame = frames[code]
        prior = inputs['previous'].get(code, {})
        opening = inputs.get('opening_evidence', {}).get(code, {})
        quote = {'code': code, 'name': row.get('name'), 'date': args.date, 'time': '15:01:00',
                 'pre_close': opening.get('pre_close') or prior.get('close'),
                 'open_price': inputs['opening_prices'].get(code),
                 'last_price': float(frame.iloc[-1]['close']), 'is_stale': False,
                 'source': 'historical_minute_adapter_not_live'}
        plan = _to_backtest_row({**row, '综合评分': row.get('screening_score') or row.get('score'),
                                '共振板块': ','.join(RealtimeEntrySignalService._sectors(row)),
                                '入场门控来源': 'production_decision_v1', '生产决策允许入场': True})
        plan['pool_type'] = row.get('pool_type') or ''
        blocked = []
        try:
            eligible, price = engine._check_buy_conditions(pd.Series(plan), args.date, code, row.get('name', ''))
        except ReplayEvidenceBlocked as exc:
            eligible, price, blocked = False, 0, exc.details
        signal = None
        first_confirm = ''
        # Advance actual closed-minute prefixes; a full-day quote must not revive an old signal.
        for count in range(6, len(frame) + 1):
            prefix = frame.iloc[:count]
            evaluated_at = (datetime.strptime(str(prefix.iloc[-1]['time']), '%H:%M:%S')
                            + timedelta(minutes=1)).strftime('%H:%M:%S')
            observed_quote = {**quote, 'time': evaluated_at, 'last_price': float(prefix.iloc[-1]['close'])}
            signal = service._evaluate_one(row, observed_quote, prefix, {}, {}, prior, args.date, rows, {})
            if signal['signal_status'] == 'confirmed':
                first_confirm = first_confirm or signal.get('confirm_time', '')
                if signal.get('entry_price'):
                    break
            if evaluated_at > str((row.get('strategy_execution') or {}).get('confirmation_deadline') or config.entry_confirm_deadline):
                break
        live_eligible = signal['signal_status'] == 'confirmed' and bool(signal.get('entry_price'))
        comparison = {'code': code, 'name': row.get('name'), 'historical_eligible': eligible,
                      'paper_minute_eligible': live_eligible, 'same_verdict': eligible == live_eligible,
                      'paper_reason': signal.get('reason'), 'confirm_time': signal.get('confirm_time'),
                      'entry_time': signal.get('entry_time'), 'historical_price': price,
                      'data_status': signal.get('data_status'),
                      'first_prefix_confirm_time': first_confirm,
                      'historical_blocked_evidence': blocked,
                      'sector': signal.get('sector_detail')}
        if eligible and live_eligible:
            meta = engine._last_entry_meta[code]
            comparison['same_confirm_time'] = meta['confirm_time'] == signal['confirm_time']
            comparison['same_entry_time'] = meta['entry_time'] == signal['entry_time']
            comparison['same_price'] = price == round_price(signal['entry_price'] * (1 + config.slippage))
            verified.append((row, signal))
        report['comparisons'].append(comparison)
        save()
        print(json.dumps({k: v for k, v in comparison.items() if k != 'sector'}, ensure_ascii=False), flush=True)

    class MinuteQuoteAdapter:
        """Test adapter only: minute prices never acquire fake receive timestamps."""
        quotes = {}

        def get_quotes(self, codes):
            return {'quotes': [self.quotes[c] for c in codes if c in self.quotes]}

    adapter = MinuteQuoteAdapter()
    paper = PaperTradingService(repository, quote_service=adapter)
    if verified:
        row, signal = verified[0]
        code = row['code']
        entry_quote = {**signal, 'code': code, 'name': row.get('name'), 'date': args.date,
                       'time': signal['entry_time'], 'last_price': signal['entry_price'],
                       'pre_close': inputs['previous'][code]['close'], 'is_stale': False,
                       'source': 'historical_minute_adapter_not_live'}
        adapter.quotes = {code: entry_quote}
        payload = {'profile': 'production', 'market_date': args.date,
                   'candidate_date': engine.calendar.prev(args.date),
                   'rows': [{**row, **signal, 'confirm_status': 'confirmed', 'is_stale': False}]}
        opened = paper.process_realtime_payload(payload)
        repeated = paper.process_realtime_payload(payload)
        report['paper_buy'] = opened
        report['paper_repeat'] = repeated
        if opened['opened']:
            position = repository.list_positions()[0]
            report['checks']['buy_executed'] = opened['opened'] == 1
            report['checks']['duplicate_not_executed'] = repeated['opened'] == 0
            report['checks']['t_plus_one'] = repository.sellable_shares(position['id'], args.date) == 0
            report['checks']['same_buy_price'] = position['entry_price'] == round_price(signal['entry_price'] * (1 + config.slippage))
            sink = SimpleNamespace(notify_exit_signal=lambda *a, **kw: {'sent': 0, 'all_delivered': True, 'test_sink': True})
            monitor = PositionMonitor(repository, quote_service=adapter, notifier=sink,
                backend=MemoryStateBackend(), market_context_provider=lambda: {},
                sector_breadth_provider=SimpleNamespace(evaluate=lambda *a, **kw: (None, {'data_completeness': 0})))
            dates = engine.calendar.get_trade_dates(args.date, (datetime.strptime(args.date, '%Y%m%d') + timedelta(days=14)).strftime('%Y%m%d'))
            for date in dates[1:]:
                if date >= datetime.now().strftime('%Y%m%d'):
                    break
                path = Path(CACHE_DIR) / 'stock' / 'tick' / f'{engine._standardize_stock_code(code)}_{date}.csv'
                raw = pd.read_csv(path) if path.exists() else pd.DataFrame()
                if args.fetch_holding_minutes and not inspect_minute_bars(raw, date, expected_code=code)['valid']:
                    dm.allow_remote_minutes = True
                    try:
                        raw = dm.get_stock_tick(engine._standardize_stock_code(code), date, require_full_day=True)
                    finally:
                        dm.allow_remote_minutes = False
                if not inspect_minute_bars(raw, date, expected_code=code)['valid']:
                    report['exit_missing_date'] = date
                    break
                daily = dm.get_stock_daily_data(engine._standardize_stock_code(code), date) or {}
                high = position['entry_price']
                for bar in normalize_minute_bars(raw).to_dict('records'):
                    high = max(high, bar['high'])
                    adapter.quotes = {code: {'code': code, 'name': row.get('name'), 'date': date,
                        'time': bar['time'], 'last_price': bar['close'], 'high_price': high,
                        'pre_close': daily.get('pre_close'), 'open_price': daily.get('open'),
                        'is_stale': False, 'source': 'historical_minute_adapter_not_live'}}
                    outcome = monitor.run_once(signal_date=date, auto_execute=True)
                    if not outcome['ok']:
                        raise RuntimeError(outcome)
                    if outcome.get('executed'):
                        report['paper_exit'] = outcome
                    if not repository.list_positions():
                        break
                if not repository.list_positions():
                    break
            trades = repository.list_trades(limit=1000)
            report['checks']['cash_reconciled'] = abs(repository.account()['cash'] - (
                PAPER_INITIAL_CAPITAL + sum((t['amount'] if t['action'] == 'sell' else -t['amount']) - t['fees'] for t in trades))) < 1e-6
            report['checks']['sell_executed'] = any(t['action'] == 'sell' for t in trades)
            report['checks']['fees_recorded'] = all(t['fees'] > 0 for t in trades)
            report['paper_trades'] = trades
        else:
            report['paper_flow_status'] = 'confirmed_but_execution_quote_rejected'
    else:
        report['paper_flow_status'] = 'no_executable_confirmation_in_cached_evidence'
    report['candidate_count'] = len(rows)
    report['verified_buy_samples'] = len(verified)
    report['mismatches'] = [r for r in report['comparisons'] if r.get('same_verdict') is False
                            or any(r.get(k) is False for k in ('same_confirm_time', 'same_entry_time', 'same_price'))]
    report['passed'] = not report['mismatches'] and bool(report['checks']) and all(report['checks'].values())
    report['limitations'] = ['Minute adapter checks are not actual live quote arrival, health leases or WeChat delivery.',
        'Close-only minute exit observations do not reproduce every intraminute tick or OHLC backtest exit.']
    save()
    print(json.dumps({k: report[k] for k in ('candidate_count', 'verified_buy_samples', 'checks', 'passed')}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report['passed'] else 2)


if __name__ == '__main__':
    main()
