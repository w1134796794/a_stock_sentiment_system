"""Authoritative daily trading-status evidence; missing quotes are not suspensions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

import pandas as pd
from loguru import logger

from core.data.data_manager_base import DataManagerBase

FIELDS = ['ts_code', 'trade_date', 'suspend_timing', 'suspend_type']
SHANGHAI = timezone(timedelta(hours=8))


def suspension_status(dm, code: str, date: str, *, allow_remote: bool | None = None) -> dict:
    getter = getattr(dm, 'get_suspension_status', None)
    try:
        options = {} if allow_remote is None else {'allow_remote': allow_remote}
        result = getter(code, date, **options) if callable(getter) else {}
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        return {'state': 'unknown', 'reason': str(exc)}


class SuspensionDataManager(DataManagerBase):
    def get_daily_suspensions(self, trade_date: str, *, force_refresh: bool = False,
                              allow_remote: bool | None = None) -> dict:
        date = str(trade_date).replace('-', '')
        datetime.strptime(date, '%Y%m%d')
        if len(date) != 8:
            raise ValueError('停复牌日期必须为YYYYMMDD')
        now = datetime.now(SHANGHAI)
        closed = date < now.strftime('%Y%m%d') or (date == now.strftime('%Y%m%d') and now.hour >= 16)
        path = self.stock_dir / 'suspend_d' / f'{date}.json'
        memory = getattr(self, '_suspension_days', None)
        if memory is None:
            memory = self._suspension_days = {}
        if (not force_refresh and date in memory
                and memory[date].get('closed_session') is closed):
            return memory[date]
        if not force_refresh and closed and path.exists():
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
                self._validate_suspensions(payload, date)
                memory[date] = payload
                return payload
            except (OSError, ValueError, TypeError, KeyError):
                pass
        payload = {'trade_date': date, 'source': 'tushare.suspend_d', 'ok': False,
                   'rows': [], 'reason': '', 'queried_at': now.isoformat(timespec='seconds'),
                   'closed_session': closed}
        remote = getattr(self, 'allow_remote_history', True) if allow_remote is None else allow_remote
        if not remote or self.ts_pro is None:
            payload['reason'] = '停复牌缓存未就绪，未允许远程取数或未配置Tushare'
            return payload
        try:
            frame = self.ts_pro.suspend_d(trade_date=date, fields=','.join(FIELDS))
            if not isinstance(frame, pd.DataFrame) or not set(FIELDS).issubset(frame.columns):
                raise ValueError('suspend_d响应缺少必要字段，不能认定为无停牌')
            if len(frame) >= 5000:
                raise ValueError('suspend_d达到单次上限，不能认定为完整日分区')
            payload.update(ok=True, rows=frame[FIELDS].fillna('').astype(str).to_dict('records'))
            self._validate_suspensions(payload, date)
            if closed:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                        json.dump(payload, stream, ensure_ascii=False, indent=2)
                    Path(temporary).replace(path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            memory[date] = payload
        except Exception as exc:
            payload.update(ok=False, rows=[], reason=str(exc))
            memory[date] = payload
            logger.warning(f'[停复牌] {date} 数据不可用，不能将无行情视为停牌: {exc}')
        return payload

    @staticmethod
    def _validate_suspensions(payload: dict, date: str) -> None:
        if (not isinstance(payload, dict) or payload.get('trade_date') != date or payload.get('source') != 'tushare.suspend_d'
                or payload.get('ok') is not True or not isinstance(payload.get('rows'), list)):
            raise ValueError('停复牌缓存不是完整的成功查询')
        for row in payload['rows']:
            if (not isinstance(row, dict) or not set(FIELDS).issubset(row) or str(row['trade_date']) != date
                    or row['suspend_type'] not in {'S', 'R'}
                    or not isinstance(row['suspend_timing'], str)
                    or len(str(row['ts_code']).split('.')[0]) != 6
                    or not str(row['ts_code']).split('.')[0].isdigit()):
                raise ValueError('停复牌记录日期、代码或类型不合法')

    def get_suspension_status(self, code: str, trade_date: str, *, allow_remote: bool | None = None) -> dict:
        day = self.get_daily_suspensions(trade_date, allow_remote=allow_remote)
        code = str(code).split('.')[0].zfill(6)
        result = {key: day.get(key) for key in ('trade_date', 'source', 'queried_at', 'reason')}
        result.update(code=code, state='unknown', suspend_timing='')
        if not day.get('ok'):
            return result
        rows = [row for row in day['rows'] if row['ts_code'].split('.')[0] == code]
        full = [row for row in rows if row['suspend_type'] == 'S' and not row['suspend_timing'].strip()]
        if full and any(row['suspend_type'] == 'R' or row['suspend_timing'].strip() for row in rows):
            result['reason'] = '停复牌记录存在冲突，不能认定为全天停牌'
        elif full:
            result.update(state='full_day', reason='Tushare suspend_d确认全天停牌')
        elif any(row['suspend_type'] == 'S' for row in rows):
            result.update(state='intraday', suspend_timing=';'.join(row['suspend_timing'] for row in rows),
                          reason='日内停牌，仍需实际可交易时段的分钟证据')
        else:
            result.update(state='trading', reason='当日未列为停牌或已复牌')
        return result
