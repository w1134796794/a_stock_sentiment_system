"""Rebuild a separate paper account using the currently enabled strategies."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from config.settings import OUTPUT_DIR, PAPER_INITIAL_CAPITAL
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.screening.strategy_profiles import StrategyProfileRepository
    from desktop.runner import BacktestController

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    profiles = StrategyProfileRepository().frozen()
    selected = profiles.default_selection()
    service = PaperTradingService()

    def default_digest() -> str:
        with service.repository._connect() as conn:
            rows = {table: [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE account_key=? ORDER BY rowid", (service.ACCOUNT_KEY,)
            )] for table in ('portfolio_accounts', 'portfolio_positions', 'portfolio_trades')}
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

    before = default_digest()
    controller = BacktestController()
    ok, message = controller.start(
        args.start, args.end, PAPER_INITIAL_CAPITAL, mode='range',
        strategy_ids=selected, entry_mode='hybrid', position_sizing_mode='fixed_risk',
        exit_policy_mode='strategy', account_profile='aggressive_three',
        missing_evidence_policy='strict',
    )
    if not ok:
        raise RuntimeError(message)
    print(message, flush=True)
    while controller._thread.is_alive():
        status = controller.status()
        temporary = args.output / 'progress.tmp'
        temporary.write_text(json.dumps(status, ensure_ascii=False, default=str), encoding='utf-8')
        temporary.replace(args.output / 'progress.json')
        print(json.dumps({'state': status['state'], 'progress': status.get('progress')}, ensure_ascii=False), flush=True)
        controller._thread.join(30)
    status = controller.status()
    (args.output / 'progress.json').write_text(json.dumps(status, ensure_ascii=False, default=str), encoding='utf-8')
    if controller.state != 'done':
        raise RuntimeError(controller.error or controller.state)
    failures = service.rebuild_evidence_failures(controller.run_id)
    if failures:
        raise RuntimeError(f'Missing evidence: {failures}')
    import pandas as pd

    result_dir = Path(OUTPUT_DIR) / 'backtest_results'
    trades = pd.read_csv(result_dir / f'backtest_trades_{controller.run_id}.csv', dtype=str)
    disabled = {row['id'] for row in profiles.list_profiles() if not row['enabled']}
    if set(trades['strategy_id']).intersection(disabled):
        raise RuntimeError('Disabled strategy found in trades')
    account = f'history_{controller.run_id}'
    imported = service.import_backtest_run(controller.run_id, account_key=account)
    if imported['skipped']:
        raise RuntimeError(f'Account import skipped trades: {imported}')
    after = default_digest()
    if before != after:
        raise RuntimeError('Default account changed during independent rebuild')
    summary = pd.read_csv(result_dir / f'backtest_summary_{controller.run_id}.csv').iloc[0].to_dict()
    account_cash = float(service.repository.account(account)['cash'])
    equity = account_cash + sum(float(row['last_price']) * float(row['shares'])
                                for row in service.repository.list_positions(account))
    if abs(equity - float(summary['final_capital'])) > 1e-5:
        raise RuntimeError('Imported account equity differs from the replay report')
    report = {'run_id': controller.run_id, 'account_key': account, 'start': args.start,
              'end': args.end, 'strategy_ids': selected, 'imported': imported,
              'summary': summary, 'data_quality': controller.data_quality,
              'default_account_unchanged': True,
              'account_cash': account_cash, 'account_equity_reconciled': True,
              'profiles': {sid: profiles.get_profile(sid) for sid in selected}}
    (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('run_id', 'account_key', 'summary', 'imported')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
