"""Application service for holdings, transactions and portfolio summaries."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from config.settings import (
    PAPER_INITIAL_CAPITAL,
    PAPER_MAX_POSITIONS,
    PAPER_POSITION_PCT,
    PAPER_ROTATION_MIN_EDGE,
)
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.protection_price import resolve_protection_price


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "--") else default
    except (TypeError, ValueError):
        return default


class HoldingService:
    def __init__(self, repository: HoldingRepository | None = None) -> None:
        self.repository = repository or HoldingRepository()
        self.repository.ensure_account(
            "default",
            name="模拟交易账户",
            initial_capital=PAPER_INITIAL_CAPITAL,
        )

    def dashboard(self, account_key: str = "default") -> Dict[str, Any]:
        exit_signals = self.repository.list_exit_signals(account_key, limit=100)
        latest_protection: Dict[int, float] = {}
        for signal in exit_signals:
            position_id = int(signal.get("position_id") or 0)
            if position_id and position_id not in latest_protection:
                latest_protection[position_id] = _number(signal.get("protect_price"))
        positions = [
            self._decorate(row, latest_protection.get(int(row.get("id") or 0), 0.0))
            for row in self.repository.list_positions(account_key)
        ]
        market_value = sum(_number(row.get("market_value")) for row in positions)
        cost_amount = sum(_number(row.get("cost_amount")) for row in positions)
        unrealized = market_value - cost_amount
        actions: Dict[str, int] = {}
        for row in positions:
            action = str(row.get("latest_action") or "hold")
            actions[action] = actions.get(action, 0) + 1
        account = self.repository.account(account_key)
        cash = _number(account.get("cash"))
        initial_capital = _number(account.get("initial_capital"))
        total_assets = cash + market_value
        total_pnl = total_assets - initial_capital
        return {
            "account": account,
            "summary": {
                "position_count": len(positions),
                "market_value": round(market_value, 2),
                "cost_amount": round(cost_amount, 2),
                "unrealized_pnl": round(unrealized, 2),
                "unrealized_pnl_pct": round(unrealized / cost_amount * 100.0, 2) if cost_amount else 0.0,
                "cash": round(cash, 2),
                "total_assets": round(total_assets, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl / initial_capital * 100.0, 2) if initial_capital else 0.0,
                "invested_pct": round(market_value / total_assets * 100.0, 2) if total_assets else 0.0,
                "actions": actions,
            },
            "positions": positions,
            "exit_signals": exit_signals,
            "trades": self.repository.list_trades(account_key, limit=500),
            "policy": {
                "initial_capital": PAPER_INITIAL_CAPITAL,
                "max_positions": PAPER_MAX_POSITIONS,
                "position_pct": PAPER_POSITION_PCT,
                "rotation_min_edge": PAPER_ROTATION_MIN_EDGE,
                "rotation_enabled": True,
            },
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def add_buy(self, payload: Dict[str, Any], account_key: str = "default") -> Dict[str, Any]:
        code = str(payload.get("code") or "")
        if (
            not self.repository.get_open_position(code, account_key)
            and len(self.repository.list_positions(account_key)) >= PAPER_MAX_POSITIONS
        ):
            raise ValueError(f"模拟交易最多同时持有{PAPER_MAX_POSITIONS}只股票")
        return self._decorate(self.repository.open_position(payload, account_key))

    def sell(self, position_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._decorate(self.repository.sell_position(position_id, payload))

    def update(self, position_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._decorate(self.repository.update_position(position_id, payload))

    def import_backtest_state(
        self,
        state: Dict[str, Any],
        account_key: str = "default",
    ) -> Dict[str, Any]:
        imported = 0
        skipped = 0
        for code, source in (state.get("current_positions") or {}).items():
            row = dict(source or {})
            try:
                self.add_buy(
                    {
                        "code": code,
                        "name": row.get("stock_name"),
                        "entry_date": row.get("entry_date"),
                        "entry_time": row.get("entry_time"),
                        "entry_price": row.get("entry_price"),
                        "shares": row.get("shares"),
                        "strategy_id": row.get("strategy_id"),
                        "strategy_name": row.get("strategy_name"),
                        "sector_names": row.get("resonance_sectors"),
                        "structural_stop": row.get("stop_loss_price"),
                        "source": "backtest_import",
                        "metadata": {"backtest_position": row},
                    },
                    account_key,
                )
                imported += 1
            except (TypeError, ValueError):
                skipped += 1
        return {"imported": imported, "skipped": skipped}

    @staticmethod
    def _decorate(row: Dict[str, Any], latest_protect_price: float = 0.0) -> Dict[str, Any]:
        if not row:
            return {}
        data = dict(row)
        price = _number(data.get("last_price"), _number(data.get("entry_price")))
        shares = int(data.get("shares") or 0)
        cost = _number(data.get("cost_amount"))
        market_value = price * shares
        pnl = market_value - cost
        metadata = dict(data.get("metadata") or {})
        base_protect, protection_source = resolve_protection_price(data)
        protection_price = max(base_protect, _number(latest_protect_price))
        if latest_protect_price > base_protect:
            protection_source = "实时动态保护价"
        entry_strength = _number(metadata.get("entry_strength_score"), 50.0)
        action_adjustment = {
            "hold": 0.0,
            "watch": -5.0,
            "reduce": -12.0,
            "sell": -25.0,
            "blocked": -20.0,
            "data_insufficient": -3.0,
        }.get(str(data.get("latest_action") or "hold"), 0.0)
        pnl_pct = pnl / cost * 100.0 if cost else 0.0
        current_strength = max(
            0.0,
            min(100.0, entry_strength + max(min(pnl_pct * 0.8, 12.0), -12.0) + action_adjustment),
        )
        data.update(
            {
                "last_price": round(price, 2),
                "entry_price": round(_number(data.get("entry_price")), 2),
                "market_value": round(market_value, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "entry_strength_score": round(entry_strength, 2),
                "current_strength_score": round(current_strength, 2),
                "structural_stop": round(protection_price, 2),
                "protection_price": round(protection_price, 2),
                "protection_price_source": protection_source,
            }
        )
        return data


__all__ = ["HoldingService"]
