"""Daily, as-of account valuation from immutable trades and Silver closing prices."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from config.settings import FACTOR_DB_PATH
from core.operations.ledger import TradingLedger


def _date(value: Any) -> str:
    return str(value or "").replace("-", "")[:8]


class AccountEquityEvaluator:
    def __init__(self, ledger: TradingLedger | None = None, *, price_db_path: Path | None = None) -> None:
        self.ledger = ledger or TradingLedger()
        self.price_db_path = Path(price_db_path or FACTOR_DB_PATH)

    def _prices(self, codes: list[str], start: str, end: str) -> tuple[list[dict], list[str]]:
        if not self.price_db_path.exists():
            return [], []
        import duckdb

        con = duckdb.connect(str(self.price_db_path), read_only=True)
        try:
            calendar = [_date(row[0]) for row in con.execute(
                "SELECT DISTINCT trade_date FROM index_daily_silver "
                "WHERE index_code='000001.SH' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                [start, end],
            ).fetchall()]
            if not codes:
                return [], calendar
            marks = ",".join("?" for _ in codes)
            rows = con.execute(
                "SELECT trade_date, code, close FROM stock_daily_silver "
                f"WHERE code IN ({marks}) AND trade_date BETWEEN ? AND ? "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY trade_date, code "
                "ORDER BY ingested_at DESC)=1",
                [*codes, start, end],
            ).fetchall()
            return [{"trade_date": _date(day), "code": str(code).split(".")[0].zfill(6),
                     "close": float(close or 0)} for day, code, close in rows], calendar
        except duckdb.Error:
            return [], []
        finally:
            con.close()

    def evaluate(self, *, account_key: str, start: str, end: str, experiment_id: str,
                 price_rows: Iterable[dict] | None = None,
                 calendar: Iterable[str] | None = None) -> dict[str, Any]:
        with self.ledger._connect() as conn:
            account = conn.execute(
                "SELECT initial_capital FROM portfolio_accounts WHERE account_key=?", (account_key,)
            ).fetchone()
            trades = [dict(row) for row in conn.execute(
                "SELECT id, position_id, trade_date, trade_time, code, action, price, shares, fees, source "
                "FROM portfolio_trades WHERE account_key=? AND trade_date<=? "
                "ORDER BY trade_date, trade_time, id", (account_key, end)
            ).fetchall()]
        initial = float(account[0] or 0) if account else 0.0
        if initial <= 0:
            return {"status": "no_funded_account", "rows": [], "summary": {}}
        codes = sorted({str(t["code"]).split(".")[0].zfill(6) for t in trades})
        if price_rows is None:
            fetched_prices, fetched_calendar = self._prices(codes, start, end)
            price_rows = fetched_prices
            if calendar is None:
                calendar = fetched_calendar
        prices = {(_date(row.get("trade_date")), str(row.get("code") or "").split(".")[0].zfill(6)):
                  float(row.get("close") or 0) for row in price_rows}
        dates = sorted({_date(day) for day in (calendar or ()) if start <= _date(day) <= end}
                       | {_date(t["trade_date"]) for t in trades if start <= _date(t["trade_date"]) <= end})
        if not dates:
            return {"status": "missing_calendar", "rows": [], "summary": {}}
        source_hash = hashlib.sha256(json.dumps(
            {"trades": trades, "prices": sorted((date, code, price) for (date, code), price in prices.items())},
            sort_keys=True, default=str,
        ).encode()).hexdigest()
        cash = initial
        inventory: dict[int, dict[str, Any]] = {}
        buys = sells = 0
        paid_fees = 0.0
        turnover = defaultdict(float)
        rows = []
        trade_index = 0
        previous_equity = None
        peak = initial
        max_drawdown = 0.0
        for day in dates:
            while trade_index < len(trades) and _date(trades[trade_index]["trade_date"]) <= day:
                trade = trades[trade_index]
                trade_index += 1
                shares = int(trade["shares"] or 0)
                amount = float(trade["price"] or 0) * shares
                fee = float(trade["fees"] or 0)
                position_id = int(trade["position_id"] or 0)
                code = str(trade["code"]).split(".")[0].zfill(6)
                position = inventory.setdefault(position_id, {"code": code, "shares": 0, "cost": 0.0})
                in_window = _date(trade["trade_date"]) >= start
                if trade["action"] == "buy":
                    cash -= amount + fee
                    position["shares"] += shares
                    position["cost"] += amount + fee
                    if in_window:
                        buys += 1
                elif trade["action"] == "sell":
                    if shares > position["shares"]:
                        raise ValueError(f"持仓流水无法对账：{day} {code} 卖出超量")
                    allocated = position["cost"] * shares / position["shares"]
                    position["cost"] -= allocated
                    position["shares"] -= shares
                    cash += amount - fee
                    if in_window:
                        sells += 1
                if in_window:
                    paid_fees += fee
                    turnover[day] += amount
            active = [position for position in inventory.values() if position["shares"] > 0]
            missing = sorted({position["code"] for position in active
                              if prices.get((day, position["code"]), 0) <= 0})
            market_value = (sum(position["shares"] * prices[(day, position["code"])]
                                for position in active) if not missing else None)
            equity = cash + market_value if market_value is not None else None
            daily_return = (equity / previous_equity - 1) * 100 if (
                equity is not None and previous_equity is not None and previous_equity > 0) else None
            if equity is not None:
                peak = max(peak, equity)
                max_drawdown = min(max_drawdown, (equity / peak - 1) * 100)
            row = {
                "trade_date": day, "cash": round(cash, 2),
                "market_value": round(market_value, 2) if market_value is not None else None,
                "equity": round(equity, 2) if equity is not None else None,
                "return_pct": round(daily_return, 4) if daily_return is not None else None,
                "cumulative_return_pct": round((equity / initial - 1) * 100, 4) if equity is not None else None,
                "drawdown_pct": round((equity / peak - 1) * 100, 4) if equity is not None else None,
                "position_count": len(active), "trade_notional": round(turnover[day], 2),
                "turnover_pct": round(turnover[day] / initial * 100, 4),
                "fees_cumulative": round(paid_fees, 2), "missing_prices": missing,
                "valuation_complete": not missing,
                "price_basis": "stock_daily_silver_close" if not missing else "missing_daily_close",
            }
            rows.append(row)
            previous_equity = equity
        complete = len(rows) > 0 and all(row["valuation_complete"] for row in rows)
        summary = {
            "initial_capital": initial, "days": len(rows),
            "valued_days": sum(row["valuation_complete"] for row in rows),
            "buys": buys, "sells": sells, "fees": round(paid_fees, 2),
            "total_return_pct": rows[-1]["cumulative_return_pct"] if complete else None,
            "max_drawdown_pct": round(max_drawdown, 4) if complete else None,
            "valuation_complete": complete,
        }
        self.ledger.save_equity_rows(experiment_id, account_key, rows, source_hash)
        return {"status": "complete" if complete else "incomplete_valuation",
                "rows": rows, "summary": summary, "source_hash": source_hash}
