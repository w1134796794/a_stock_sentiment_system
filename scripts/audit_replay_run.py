"""Read-only diagnosis of a saved replay; never rebuilds or mutates an account."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def audit_run(result_dir: Path, run_id: str, account_db: Path | None = None):
    trades = pd.read_csv(result_dir / f'backtest_trades_{run_id}.csv', dtype={'date': str, 'stock_code': str})
    attempts = pd.read_csv(result_dir / f'backtest_entry_attempts_{run_id}.csv', dtype={'date': str, 'stock_code': str})
    manifest = json.loads((result_dir / f'backtest_manifest_{run_id}.json').read_text(encoding='utf-8'))
    summary = pd.read_csv(result_dir / f'backtest_summary_{run_id}.csv').iloc[0].to_dict()
    buys = trades[trades.action == 'BUY'].copy()
    sells = trades[trades.action == 'SELL'].copy()
    wins, losses = sells[sells.pnl > 0], sells[sells.pnl < 0]
    payoff = float(wins.pnl.mean() / abs(losses.pnl.mean())) if len(wins) and len(losses) else None
    config = manifest.get('backtest_config') or {}
    commission, stamp = float(config.get('commission_rate') or 0), float(config.get('stamp_duty_rate') or 0)
    configured_fees = float((buys.entry_price * buys.shares).sum() * commission
                            + (sells.exit_price * sells.shares).sum() * (commission + stamp))
    accounts = []
    if account_db and account_db.exists():
        with sqlite3.connect(f'file:{account_db.resolve().as_posix()}?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            accounts = [dict(row) for row in connection.execute(
                "SELECT a.account_key,a.cash,a.initial_capital,COUNT(t.id) trade_count,SUM(t.fees) fees "
                "FROM portfolio_accounts a JOIN portfolio_trades t ON a.account_key=t.account_key "
                "WHERE json_extract(t.metadata_json,'$.run_id')=? GROUP BY a.account_key", (run_id,))]
    daily = attempts.groupby(['date', 'stage', 'reason_code'], dropna=False).size().rename('count').reset_index()
    strategies = sells.groupby('strategy_id').agg(
        closed=('pnl', 'size'), net_pnl=('pnl', 'sum'), mean_return=('pnl_pct', 'mean'),
        wins=('pnl', lambda values: int((values > 0).sum())),
    ).reset_index()
    exits = sells.groupby('exit_reason').agg(closed=('pnl', 'size'), net_pnl=('pnl', 'sum')).reset_index()
    months = sells.assign(month=sells.date.str[:6]).groupby('month').agg(
        closed=('pnl', 'size'), net_pnl=('pnl', 'sum')).reset_index()
    nonchronological_days = []
    for date, group in buys.groupby('date', sort=True):
        times = group.entry_time.fillna('').astype(str).tolist()
        if times != sorted(times):
            nonchronological_days.append({'date': date, 'entry_times': times})
    september = attempts[(attempts.date >= '20260901') & (attempts.date <= '20260918')]
    legacy_reasons = {'market_too_weak', 'score_below_neutral_threshold', 'score_below_active_threshold'}
    gate_counts = september[september.reason_code.isin(legacy_reasons)].reason_code.value_counts().to_dict()
    audit = {
        'run_id': run_id, 'account_mutated': False,
        'saved_total_return': float(summary['total_return']),
        'closed_trades': len(sells), 'wins': len(wins), 'losses': len(losses),
        'win_rate': len(wins) / len(sells) if len(sells) else None,
        'net_pnl': float(sells.pnl.sum()), 'average_win_loss_ratio': payoff,
        'profit_factor': float(wins.pnl.sum() / abs(losses.pnl.sum())) if len(losses) else None,
        'break_even_win_rate_at_observed_payoff': 1 / (1 + payoff) if payoff is not None else None,
        'configured_fee_estimate': configured_fees, 'account_reconciliation': accounts,
        'missing_cost_basis_rows': len(trades) if 'position_size' not in trades else int(pd.to_numeric(trades.position_size, errors='coerce').isna().sum()),
        'missing_sell_time_rows': len(sells) if 'exit_time' not in sells else int(sells.exit_time.fillna('').eq('').sum()),
        'nonchronological_buy_days': nonchronological_days,
        'september_01_18_candidate_attempts': int((september.stage == 'entry_signal').sum() + september.stage.eq('market_gate').sum()),
        'september_01_18_legacy_gate_counts': {str(k): int(v) for k, v in gate_counts.items()},
        'interpretation': '既有运行的描述性审计，不是修复后表现或样本外预测；手续费估计依赖该批配置，除权等特殊事件须另核验',
    }
    return audit, {'daily_funnel': daily, 'strategy_pnl': strategies, 'exit_pnl': exits, 'monthly_pnl': months}


def main():
    from config.settings import HOLDING_DB_PATH, OUTPUT_DIR, WEB_DATA_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id')
    parser.add_argument('--result-dir', type=Path, default=Path(OUTPUT_DIR) / 'backtest_results')
    parser.add_argument('--output-dir', type=Path, default=Path(WEB_DATA_DIR) / 'portfolio_replay_diagnostics')
    args = parser.parse_args()
    audit, frames = audit_run(args.result_dir, args.run_id, Path(HOLDING_DB_PATH))
    destination = args.output_dir / f'audit_{args.run_id}'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, frame in frames.items():
        frame.to_csv(destination / f'{name}.csv', index=False, encoding='utf-8-sig')
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    print(f'Audit artifacts: {destination}')


if __name__ == '__main__':
    main()
