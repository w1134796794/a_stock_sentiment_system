"""SQLite persistence for live holdings, executions and exit decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List

from config.settings import HOLDING_DB_PATH
from core.portfolio.protection_price import resolve_protection_price
from core.realtime.models import normalize_stock_code


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


class HoldingRepository:
    """Operational store kept separate from factor and snapshot warehouses."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or HOLDING_DB_PATH)
        self.ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    initial_capital REAL NOT NULL DEFAULT 0,
                    cash REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS portfolio_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    strategy_id TEXT NOT NULL DEFAULT '',
                    strategy_name TEXT NOT NULL DEFAULT '',
                    sector_names TEXT NOT NULL DEFAULT '',
                    entry_date TEXT NOT NULL,
                    entry_time TEXT NOT NULL DEFAULT '',
                    entry_price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    cost_amount REAL NOT NULL,
                    last_price REAL NOT NULL DEFAULT 0,
                    high_watermark REAL NOT NULL DEFAULT 0,
                    structural_stop REAL NOT NULL DEFAULT 0,
                    emergency_loss_pct REAL NOT NULL DEFAULT 6,
                    status TEXT NOT NULL DEFAULT 'open',
                    latest_action TEXT NOT NULL DEFAULT 'hold',
                    latest_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS portfolio_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    position_id INTEGER,
                    trade_date TEXT NOT NULL,
                    trade_time TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    shares INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    realized_pnl_pct REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    strategy_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(position_id) REFERENCES portfolio_positions(id)
                );

                CREATE TABLE IF NOT EXISTS position_daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    snapshot_at TEXT NOT NULL,
                    last_price REAL NOT NULL DEFAULT 0,
                    market_value REAL NOT NULL DEFAULT 0,
                    unrealized_pnl REAL NOT NULL DEFAULT 0,
                    unrealized_pnl_pct REAL NOT NULL DEFAULT 0,
                    high_watermark REAL NOT NULL DEFAULT 0,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(position_id) REFERENCES portfolio_positions(id)
                );

                CREATE TABLE IF NOT EXISTS exit_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    signal_date TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    action TEXT NOT NULL,
                    action_label TEXT NOT NULL,
                    current_price REAL NOT NULL DEFAULT 0,
                    protect_price REAL NOT NULL DEFAULT 0,
                    pnl_pct REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    policy_version TEXT NOT NULL DEFAULT '',
                    notified_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(position_id) REFERENCES portfolio_positions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON portfolio_positions(account_key, status, updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_open
                    ON portfolio_positions(account_key, code) WHERE status='open';
                CREATE INDEX IF NOT EXISTS idx_trades_date
                    ON portfolio_trades(account_key, trade_date DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_exit_signals_position
                    ON exit_signals(position_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_position_snapshot_minute
                    ON position_daily_snapshots(position_id, snapshot_at);
                """
            )
            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO portfolio_accounts "
                "(account_key, name, initial_capital, cash, created_at, updated_at) "
                "VALUES ('default', '默认账户', 0, 0, ?, ?)",
                (now, now),
            )

    def ensure_account(
        self,
        account_key: str,
        *,
        name: str,
        initial_capital: float = 0.0,
    ) -> Dict[str, Any]:
        capital = max(float(initial_capital or 0), 0.0)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO portfolio_accounts "
                "(account_key, name, initial_capital, cash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account_key, name, capital, capital, now, now),
            )
            account = conn.execute(
                "SELECT initial_capital FROM portfolio_accounts WHERE account_key=?",
                (account_key,),
            ).fetchone()
            if account and capital > 0 and float(account["initial_capital"] or 0) <= 0:
                open_cost = conn.execute(
                    "SELECT COALESCE(SUM(cost_amount), 0) FROM portfolio_positions "
                    "WHERE account_key=? AND status='open'",
                    (account_key,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE portfolio_accounts SET name=?, initial_capital=?, cash=?, updated_at=? "
                    "WHERE account_key=?",
                    (name, capital, max(capital - float(open_cost or 0), 0.0), now, account_key),
                )
        return self.account(account_key)

    def reset_account(self, account_key: str, *, initial_capital: float | None = None) -> None:
        with self._connect() as conn:
            position_ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM portfolio_positions WHERE account_key=?", (account_key,)
                ).fetchall()
            ]
            if position_ids:
                marks = ",".join("?" for _ in position_ids)
                conn.execute(f"DELETE FROM exit_signals WHERE position_id IN ({marks})", position_ids)
                conn.execute(
                    f"DELETE FROM position_daily_snapshots WHERE position_id IN ({marks})",
                    position_ids,
                )
            conn.execute("DELETE FROM portfolio_trades WHERE account_key=?", (account_key,))
            conn.execute("DELETE FROM portfolio_positions WHERE account_key=?", (account_key,))
            if initial_capital is not None:
                capital = max(float(initial_capital), 0.0)
                conn.execute(
                    "UPDATE portfolio_accounts SET initial_capital=?, cash=?, updated_at=? "
                    "WHERE account_key=?",
                    (capital, capital, _now(), account_key),
                )

    def has_buy_trade(self, account_key: str, code: str, trade_date: str) -> bool:
        pure = normalize_stock_code(code, add_suffix=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM portfolio_trades WHERE account_key=? AND code=? "
                "AND trade_date=? AND action='buy' LIMIT 1",
                (account_key, pure, str(trade_date).replace("-", "")),
            ).fetchone()
        return bool(row)

    def account(self, account_key: str = "default") -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_accounts WHERE account_key=?", (account_key,)
            ).fetchone()
        return dict(row) if row else {}

    def list_positions(self, account_key: str = "default", *, status: str = "open") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM portfolio_positions WHERE account_key=?"
        params: List[Any] = [account_key]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._position_row(row) for row in rows]

    def get_position(self, position_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_positions WHERE id=?", (int(position_id),)
            ).fetchone()
        return self._position_row(row) if row else {}

    def get_open_position(self, code: str, account_key: str = "default") -> Dict[str, Any]:
        pure = normalize_stock_code(code, add_suffix=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_positions "
                "WHERE account_key=? AND code=? AND status='open'",
                (account_key, pure),
            ).fetchone()
        return self._position_row(row) if row else {}

    def open_position(self, payload: Dict[str, Any], account_key: str = "default") -> Dict[str, Any]:
        code = normalize_stock_code(payload.get("code"), add_suffix=False)
        if not code:
            raise ValueError("股票代码不能为空")
        price = float(payload.get("entry_price") or payload.get("price") or 0)
        shares = int(payload.get("shares") or 0)
        if price <= 0 or shares <= 0:
            raise ValueError("买入价格和股数必须大于0")
        trade_date = str(payload.get("entry_date") or payload.get("trade_date") or "").replace("-", "")
        if len(trade_date) != 8 or not trade_date.isdigit():
            raise ValueError("买入日期必须为YYYYMMDD")
        now = _now()
        emergency_loss_pct = float(payload.get("emergency_loss_pct") or 6)
        structural_stop, protection_source = resolve_protection_price({
            **payload,
            "entry_price": price,
            "emergency_loss_pct": emergency_loss_pct,
        })
        metadata = dict(payload.get("metadata") or {})
        metadata.setdefault("protection_price_source", protection_source)
        existing = self.get_open_position(code, account_key)
        with self._connect() as conn:
            account = conn.execute(
                "SELECT initial_capital, cash FROM portfolio_accounts WHERE account_key=?",
                (account_key,),
            ).fetchone()
            tracked_cash = bool(account and float(account["initial_capital"] or 0) > 0)
            buy_amount = price * shares + float(payload.get("fees") or 0)
            if tracked_cash and float(account["cash"] or 0) + 1e-6 < buy_amount:
                raise ValueError("模拟账户可用资金不足")
            if existing:
                old_shares = int(existing["shares"])
                total_shares = old_shares + shares
                total_cost = float(existing["cost_amount"]) + price * shares
                entry_price = total_cost / total_shares
                position_id = int(existing["id"])
                conn.execute(
                    "UPDATE portfolio_positions SET shares=?, cost_amount=?, entry_price=?, "
                    "last_price=?, high_watermark=?, updated_at=? WHERE id=?",
                    (
                        total_shares,
                        total_cost,
                        entry_price,
                        price,
                        max(float(existing.get("high_watermark") or 0), price),
                        now,
                        position_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO portfolio_positions (
                        account_key, code, name, strategy_id, strategy_name, sector_names,
                        entry_date, entry_time, entry_price, shares, cost_amount, last_price,
                        high_watermark, structural_stop, emergency_loss_pct, metadata_json,
                        opened_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_key, code, str(payload.get("name") or ""),
                        str(payload.get("strategy_id") or ""), str(payload.get("strategy_name") or ""),
                        str(payload.get("sector_names") or ""), trade_date,
                        str(payload.get("entry_time") or ""), price, shares, price * shares,
                        price, price, structural_stop,
                        emergency_loss_pct,
                        _json(metadata), now, now,
                    ),
                )
                position_id = int(cur.lastrowid)
            self._insert_trade(
                conn,
                account_key=account_key,
                position_id=position_id,
                payload={**payload, "code": code, "price": price, "shares": shares, "action": "buy"},
            )
            if tracked_cash:
                conn.execute(
                    "UPDATE portfolio_accounts SET cash=cash-?, updated_at=? WHERE account_key=?",
                    (buy_amount, now, account_key),
                )
        return self.get_position(position_id)

    def sell_position(self, position_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        position = self.get_position(position_id)
        if not position or position.get("status") != "open":
            raise ValueError("持仓不存在或已关闭")
        price = float(payload.get("price") or 0)
        shares = int(payload.get("shares") or position.get("shares") or 0)
        shares = min(shares, int(position.get("shares") or 0))
        if price <= 0 or shares <= 0:
            raise ValueError("卖出价格和股数必须大于0")
        remaining = int(position["shares"]) - shares
        allocated_cost = float(position["cost_amount"]) * shares / int(position["shares"])
        fees = float(payload.get("fees") or 0)
        pnl = price * shares - fees - allocated_cost
        pnl_pct = pnl / allocated_cost * 100.0 if allocated_cost else 0.0
        now = _now()
        with self._connect() as conn:
            self._insert_trade(
                conn,
                account_key=str(position["account_key"]),
                position_id=int(position_id),
                payload={
                    **payload,
                    "code": position["code"],
                    "name": position["name"],
                    "price": price,
                    "shares": shares,
                    "action": "sell",
                    "realized_pnl": pnl,
                    "realized_pnl_pct": pnl_pct,
                    "strategy_id": position.get("strategy_id"),
                },
            )
            if remaining <= 0:
                conn.execute(
                    "UPDATE portfolio_positions SET shares=0, cost_amount=0, last_price=?, "
                    "status='closed', latest_action='sold', latest_reason=?, updated_at=?, closed_at=? WHERE id=?",
                    (price, str(payload.get("reason") or "手工卖出"), now, now, int(position_id)),
                )
            else:
                conn.execute(
                    "UPDATE portfolio_positions SET shares=?, cost_amount=?, last_price=?, "
                    "latest_reason=?, updated_at=? WHERE id=?",
                    (remaining, float(position["cost_amount"]) - allocated_cost, price,
                     str(payload.get("reason") or "部分卖出"), now, int(position_id)),
                )
            account = conn.execute(
                "SELECT initial_capital FROM portfolio_accounts WHERE account_key=?",
                (str(position["account_key"]),),
            ).fetchone()
            if account and float(account["initial_capital"] or 0) > 0:
                conn.execute(
                    "UPDATE portfolio_accounts SET cash=cash+?, updated_at=? WHERE account_key=?",
                    (price * shares - fees, now, str(position["account_key"])),
                )
        return self.get_position(position_id)

    def update_position_market(
        self,
        position_id: int,
        *,
        last_price: float,
        high_watermark: float,
        action: str,
        reason: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolio_positions SET last_price=?, high_watermark=?, latest_action=?, "
                "latest_reason=?, updated_at=? WHERE id=?",
                (last_price, high_watermark, action, reason, _now(), int(position_id)),
            )

    def update_position(self, position_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "name": str,
            "strategy_id": str,
            "strategy_name": str,
            "sector_names": str,
            "structural_stop": float,
            "emergency_loss_pct": float,
        }
        assignments: List[str] = []
        values: List[Any] = []
        for field, converter in allowed.items():
            if field not in payload:
                continue
            assignments.append(f"{field}=?")
            values.append(converter(payload.get(field) or 0) if converter is float else converter(payload.get(field) or ""))
        if not assignments:
            return self.get_position(position_id)
        assignments.append("updated_at=?")
        values.extend([_now(), int(position_id)])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE portfolio_positions SET {', '.join(assignments)} WHERE id=?",
                values,
            )
        return self.get_position(position_id)

    def save_snapshot(self, position_id: int, payload: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO position_daily_snapshots (
                    position_id, snapshot_at, last_price, market_value, unrealized_pnl,
                    unrealized_pnl_pct, high_watermark, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(position_id), str(payload.get("snapshot_at") or _now()),
                    float(payload.get("last_price") or 0), float(payload.get("market_value") or 0),
                    float(payload.get("unrealized_pnl") or 0), float(payload.get("unrealized_pnl_pct") or 0),
                    float(payload.get("high_watermark") or 0), _json(payload.get("evidence")),
                ),
            )
            return int(cur.lastrowid or 0)

    def save_exit_signal(self, position_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action") or "hold")
        with self._connect() as conn:
            previous = conn.execute(
                "SELECT * FROM exit_signals WHERE position_id=? ORDER BY id DESC LIMIT 1",
                (int(position_id),),
            ).fetchone()
            changed = not previous or str(previous["action"]) != action
            if not changed:
                now = _now()
                conn.execute(
                    "UPDATE exit_signals SET signal_date=?, signal_time=?, action_label=?, "
                    "current_price=?, protect_price=?, pnl_pct=?, reason=?, evidence_json=?, "
                    "policy_version=? WHERE id=?",
                    (
                        str(payload.get("signal_date") or now[:10].replace("-", "")),
                        str(payload.get("signal_time") or now[11:19]),
                        str(payload.get("action_label") or action),
                        float(payload.get("current_price") or 0),
                        float(payload.get("protect_price") or 0),
                        float(payload.get("pnl_pct") or 0),
                        str(payload.get("reason") or ""),
                        _json(payload.get("evidence")),
                        str(payload.get("policy_version") or ""),
                        int(previous["id"]),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM exit_signals WHERE id=?", (int(previous["id"]),),
                ).fetchone()
                return {**self._signal_row(row), "changed": False}
            now = _now()
            cur = conn.execute(
                """INSERT INTO exit_signals (
                    position_id, signal_date, signal_time, action, action_label, current_price,
                    protect_price, pnl_pct, reason, evidence_json, policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(position_id), str(payload.get("signal_date") or now[:10].replace("-", "")),
                    str(payload.get("signal_time") or now[11:19]), action,
                    str(payload.get("action_label") or action), float(payload.get("current_price") or 0),
                    float(payload.get("protect_price") or 0), float(payload.get("pnl_pct") or 0),
                    str(payload.get("reason") or ""), _json(payload.get("evidence")),
                    str(payload.get("policy_version") or ""), now,
                ),
            )
            row = conn.execute("SELECT * FROM exit_signals WHERE id=?", (cur.lastrowid,)).fetchone()
        return {**self._signal_row(row), "changed": changed}

    def mark_signal_notified(self, signal_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE exit_signals SET notified_at=? WHERE id=?", (_now(), int(signal_id)))

    def list_exit_signals(
        self, account_key: str | None = None, *, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        where = "WHERE p.account_key=?" if account_key else ""
        params: List[Any] = [account_key] if account_key else []
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.*, p.code, p.name, p.strategy_name, p.entry_date
                   FROM exit_signals s JOIN portfolio_positions p ON p.id=s.position_id
                   """ + where + " ORDER BY s.id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._signal_row(row) for row in rows]

    def list_trades(self, account_key: str = "default", *, limit: int = 300) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio_trades WHERE account_key=? ORDER BY trade_date DESC, id DESC LIMIT ?",
                (account_key, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [self._trade_row(row) for row in rows]

    def _insert_trade(
        self,
        conn: sqlite3.Connection,
        *,
        account_key: str,
        position_id: int,
        payload: Dict[str, Any],
    ) -> None:
        price = float(payload.get("price") or 0)
        shares = int(payload.get("shares") or 0)
        trade_date = str(payload.get("trade_date") or payload.get("entry_date") or datetime.now().strftime("%Y%m%d")).replace("-", "")
        conn.execute(
            """INSERT INTO portfolio_trades (
                account_key, position_id, trade_date, trade_time, code, name, action, price,
                shares, amount, fees, realized_pnl, realized_pnl_pct, reason, strategy_id,
                source, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_key, int(position_id), trade_date, str(payload.get("trade_time") or payload.get("entry_time") or ""),
                normalize_stock_code(payload.get("code"), add_suffix=False), str(payload.get("name") or ""),
                str(payload.get("action") or "buy"), price, shares, price * shares,
                float(payload.get("fees") or 0), float(payload.get("realized_pnl") or 0),
                float(payload.get("realized_pnl_pct") or 0), str(payload.get("reason") or ""),
                str(payload.get("strategy_id") or ""), str(payload.get("source") or "manual"),
                _json(payload.get("metadata")), _now(),
            ),
        )

    @staticmethod
    def _position_row(row: sqlite3.Row | None) -> Dict[str, Any]:
        if not row:
            return {}
        data = dict(row)
        data["metadata"] = _loads(data.pop("metadata_json", "{}"))
        return data

    @staticmethod
    def _trade_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        data["metadata"] = _loads(data.pop("metadata_json", "{}"))
        return data

    @staticmethod
    def _signal_row(row: sqlite3.Row | None) -> Dict[str, Any]:
        if not row:
            return {}
        data = dict(row)
        data["evidence"] = _loads(data.pop("evidence_json", "{}"))
        return data


__all__ = ["HoldingRepository"]
