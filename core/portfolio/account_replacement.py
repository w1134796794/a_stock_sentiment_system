"""Explicit, transactional adoption of a completed independent replay account."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from core.signals.production_rules import RULESET_VERSION


class AccountReplacement:
    def __init__(self, repository):
        self.repository = repository
        with repository._write() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS portfolio_rebuild_runs ('
                         'account_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, '
                         'end_date TEXT NOT NULL, config_version TEXT NOT NULL, quality_json TEXT NOT NULL)')

    def register(self, account_key, run_id, end_date, config_version, quality):
        with self.repository._write() as conn:
            conn.execute('INSERT OR REPLACE INTO portfolio_rebuild_runs VALUES (?,?,?,?,?)',
                         (account_key, run_id, end_date, config_version, json.dumps(quality, ensure_ascii=False)))

    @staticmethod
    def _revision(conn, keys):
        data = []
        for key in keys:
            account = conn.execute('SELECT * FROM portfolio_accounts WHERE account_key=?', (key,)).fetchone()
            if not account:
                raise ValueError('账户不存在')
            data.append(dict(account))
            for table in ('portfolio_positions', 'portfolio_trades', 'portfolio_replay_checkpoints'):
                rows = conn.execute(f'SELECT * FROM {table} WHERE account_key=? ORDER BY rowid', (key,)).fetchall()
                data.extend({k: v for k, v in dict(row).items() if k not in {
                    'updated_at', 'last_price', 'high_watermark', 'latest_action', 'latest_reason'}} for row in rows)
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _validate(self, conn, source):
        if not str(source).startswith('history_'):
            raise ValueError('只能采用独立历史回测账户，不能使用当前账户或备份账户')
        row = conn.execute('SELECT * FROM portfolio_rebuild_runs WHERE account_key=?', (source,)).fetchone()
        if not row:
            raise ValueError('该账户缺少新口径完成证明，请先按最新规则独立回测')
        record = dict(row)
        quality = json.loads(record['quality_json'])
        if quality.get('ruleset_version') != RULESET_VERSION or quality.get('status') != 'complete':
            raise ValueError('旧口径或缺证据样本不能覆盖当前模拟账户')
        if not quality.get('costs_verified') or not quality.get('full_sample_metrics_valid'):
            raise ValueError('费用或完整样本检查未通过，不能采用')
        from core.portfolio.incremental_replay import IncrementalPaperReplay
        if record['config_version'] != IncrementalPaperReplay.config_version():
            raise ValueError('配置或代码已变化，请按当前版本重新回测')
        return record

    def preview(self, source):
        with self.repository._connect() as conn:
            record = self._validate(conn, source)
            revision = self._revision(conn, ['default', source])
            counts = {}
            for key in ('default', source):
                counts[key] = {'positions': conn.execute(
                    "SELECT COUNT(*) FROM portfolio_positions WHERE account_key=? AND status='open'", (key,)).fetchone()[0],
                    'trades': conn.execute('SELECT COUNT(*) FROM portfolio_trades WHERE account_key=?', (key,)).fetchone()[0]}
            return {'revision': revision, 'source': source, 'end_date': record['end_date'], 'counts': counts}

    @staticmethod
    def _copy(conn, source, target, *, name):
        account = dict(conn.execute('SELECT * FROM portfolio_accounts WHERE account_key=?', (source,)).fetchone())
        conn.execute('INSERT INTO portfolio_accounts (account_key,name,initial_capital,cash,created_at,updated_at) '
                     'VALUES (?,?,?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET '
                     'initial_capital=excluded.initial_capital,cash=excluded.cash,updated_at=excluded.updated_at',
                     (target, name, account['initial_capital'], account['cash'], account['created_at'], account['updated_at']))
        mapping = {}
        for table in ('portfolio_positions', 'portfolio_trades', 'portfolio_executions',
                      'portfolio_replay_days', 'portfolio_replay_checkpoints'):
            for source_row in conn.execute(f'SELECT * FROM {table} WHERE account_key=? ORDER BY rowid', (source,)).fetchall():
                row = dict(source_row)
                original_id = row.pop('id', None)
                row['account_key'] = target
                if 'position_id' in row:
                    row['position_id'] = mapping.get(row['position_id'])
                if 'event_key' in row:
                    row['event_key'] = hashlib.sha256((target + row['event_key']).encode()).hexdigest()
                columns = list(row)
                cursor = conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(row.values()))
                if table == 'portfolio_positions':
                    mapping[original_id] = cursor.lastrowid
        for table in ('exit_signals', 'position_daily_snapshots'):
            for old, new in mapping.items():
                for source_row in conn.execute(f'SELECT * FROM {table} WHERE position_id=?', (old,)).fetchall():
                    row = dict(source_row)
                    row.pop('id')
                    row['position_id'] = new
                    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        last = conn.execute('SELECT COALESCE(MAX(id),0) FROM portfolio_trades WHERE account_key=?', (target,)).fetchone()[0]
        conn.execute('UPDATE portfolio_replay_checkpoints SET account_trade_id=? WHERE account_key=?', (last, target))

    def replace(self, source, revision):
        if not revision:
            raise ValueError('请先预览并确认账户替换')
        with self.repository._write() as conn:
            record = self._validate(conn, source)
            if revision != self._revision(conn, ['default', source]):
                raise ValueError('账户已发生变化，请重新预览；当前账户未修改')
            backup = 'backup_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8]
            self._copy(conn, 'default', backup, name='替换前模拟账户备份')
            ids = [r[0] for r in conn.execute('SELECT id FROM portfolio_positions WHERE account_key=?', ('default',))]
            for identifier in ids:
                conn.execute('DELETE FROM exit_signals WHERE position_id=?', (identifier,))
                conn.execute('DELETE FROM position_daily_snapshots WHERE position_id=?', (identifier,))
            for table in ('portfolio_trades', 'portfolio_executions', 'portfolio_replay_days',
                          'portfolio_replay_checkpoints', 'portfolio_positions'):
                conn.execute(f'DELETE FROM {table} WHERE account_key=?', ('default',))
            self._copy(conn, source, 'default', name='模拟交易账户')
            last = conn.execute("SELECT COALESCE(MAX(id),0) FROM portfolio_trades WHERE account_key='default'").fetchone()[0]
            conn.execute('INSERT OR REPLACE INTO portfolio_replay_checkpoints '
                         '(account_key,last_completed_date,config_version,state_json,account_trade_id,updated_at) '
                         "VALUES ('default',?,?, '{}',?,?)",
                         (record['end_date'], record['config_version'], last, datetime.now().isoformat(timespec='seconds')))
            return {'account_key': 'default', 'backup_account': backup, 'source_account': source,
                    'last_completed_date': record['end_date']}
