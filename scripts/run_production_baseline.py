"""Run a strict production-rule baseline without replacing the paper account."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from backtest.backtest_engine import BacktestEngine
    from backtest.evidence_policy import ReplayEvidenceBlocked
    from config.settings import CACHE_DIR, HOLDING_DB_PATH, OUTPUT_DIR, PAPER_INITIAL_CAPITAL, TUSHARE_TOKEN
    from core.data import DataManager
    from core.operations.replay_research import atomic_json
    from core.portfolio.holding_repository import HoldingRepository
    from core.portfolio.incremental_replay import IncrementalPaperReplay
    from core.portfolio.paper_trading_service import PaperTradingService
    from core.signals.production_rules import RULESET_VERSION
    from run_backtest import save_backtest_results

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fetch-missing', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def default_digest():
        if not Path(HOLDING_DB_PATH).exists():
            return 'absent'
        with sqlite3.connect(Path(HOLDING_DB_PATH).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = {table: [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE account_key='default' ORDER BY rowid")]
                for table in ('portfolio_accounts', 'portfolio_positions', 'portfolio_trades')}
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

    before = default_digest()
    version = IncrementalPaperReplay.config_version()
    repository = HoldingRepository(args.output / 'configuration_probe.sqlite')
    replay = IncrementalPaperReplay(repository)
    config = replay._config(PAPER_INITIAL_CAPITAL)
    config.exit_minute_data_policy = 'cache_or_fetch' if args.fetch_missing else 'cache_only'
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
    dm.allow_remote_minutes = args.fetch_missing
    engine = BacktestEngine(dm, config)
    report = {'start': args.start, 'end': args.end, 'ruleset': RULESET_VERSION,
              'config_version': version, 'config': asdict(config), 'state': 'running',
              'live_runtime_verified': False, 'wechat_sent': False, 'fetch_missing': args.fetch_missing,
              'default_account_digest_before': before}
    atomic_json(args.output / 'baseline_result.json', report)
    last_print = 0

    def progress(item):
        nonlocal last_print
        atomic_json(args.output / 'baseline_progress.json', item)
        if item.get('unit') == '交易日' and item.get('completed'):
            atomic_json(args.output / 'baseline_checkpoint.json', {
                'config_version': version, 'last_completed_date': item['trade_date'],
                'account_state': engine.export_state()})
        now = time.monotonic()
        if now - last_print >= 15 or item.get('stage') == '生成回测报告':
            print(json.dumps(item, ensure_ascii=False), flush=True)
            last_print = now

    exit_code = 0
    try:
        result = engine.run_backtest(args.start, args.end, '', progress=progress)
        if IncrementalPaperReplay.config_version() != version:
            raise RuntimeError('Effective configuration or rule source changed during validation')
        run_id = time.strftime('%Y%m%d_%H%M%S') + '_production_validation'
        save_backtest_results(result, str(OUTPUT_DIR), timestamp=run_id,
            metadata={'ruleset_version': RULESET_VERSION, 'config_version': version,
                      'config': asdict(config), 'entry_mode': 'hybrid', 'account_profile': 'aggressive_three'})
        quality = engine.replay_data_quality()
        if not quality.get('full_sample_metrics_valid'):
            raise RuntimeError('Incomplete evidence cannot be published as a complete baseline')
        service = PaperTradingService()
        failures = service.rebuild_evidence_failures(run_id)
        if failures:
            raise ReplayEvidenceBlocked(failures, quality)
        account = f'history_{run_id}'
        imported = service.import_backtest_run(run_id, account_key=account)
        if imported['skipped']:
            raise RuntimeError(f'Account import rejected trades: {imported}')
        report.update(state='completed', run_id=run_id, account_key=account, imported=imported,
            summary={k: v for k, v in result.items() if not isinstance(v, (dict, list))}, data_quality=quality)
    except ReplayEvidenceBlocked as exc:
        exit_code = 2
        report.update(state='blocked', blocked_evidence=exc.details, data_quality=engine.replay_data_quality(),
                      completed_dates=[r['date'] for r in engine.daily_nav],
                      classification='incomplete_no_full_period_performance')
    except Exception as exc:
        exit_code = 1
        report.update(state='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        report['default_account_unchanged'] = default_digest() == before
        atomic_json(args.output / 'baseline_result.json', report)
        print(json.dumps({k: v for k, v in report.items() if k not in ('config', 'data_quality')},
                         ensure_ascii=False, default=str), flush=True)
    if not report['default_account_unchanged']:
        raise RuntimeError('Default paper account changed during validation')
    raise SystemExit(exit_code)


if __name__ == '__main__':
    main()
