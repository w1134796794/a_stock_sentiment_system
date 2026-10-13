"""Chronological event-tape replay into three isolated research accounts."""
from __future__ import annotations

import copy
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from core.operations.ledger import TradingLedger, stable_id
from core.portfolio.signal_comparison import ComparisonConfig, SignalComparisonService, freeze_holding_state
from core.signals.case_features import clean, event_time
from core.signals.similar_case_service import number


@dataclass(frozen=True)
class AccountExperimentConfig:
    initial_capital: float = 1_000_000
    position_pct: float = .34
    max_positions: int = 3
    baseline_rotation_edge: float = 6
    quote_age_seconds: float = 90

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values())
                or self.initial_capital <= 0 or not 0 < self.position_pct <= 1
                or not 1 <= self.max_positions <= 3 or self.quote_age_seconds <= 0 or self.baseline_rotation_edge < 0):
            raise ValueError("Invalid account experiment configuration")


class AnalogAccountExperiment:
    """Input is frozen observations, not retroactively constructed daily buy prices.

    Confirmation/exit events schedule orders; execution waits for the next quote.
    No HoldingRepository, live account, remote provider, or notifier is used here.
    """
    def __init__(self, *, ledger: TradingLedger, config: AccountExperimentConfig | None = None,
                 comparison: ComparisonConfig | None = None):
        self.ledger = ledger
        self.config = config or AccountExperimentConfig()
        self.comparison = comparison or ComparisonConfig()
        self.service = SignalComparisonService(self.comparison)

    def freeze(self, *, label: str = "analog-accounts") -> dict:
        from pathlib import Path
        code_hash = stable_id(Path(__file__).read_text(encoding="utf-8"),
                              Path(__file__).with_name("signal_comparison.py").read_text(encoding="utf-8"))
        frozen = {"kind": "analog_account_contract", "label": label, "configuration": asdict(self.config),
                  "comparison": asdict(self.comparison), "code_hash": code_hash,
                  "frozen_at": datetime.now().isoformat(timespec="milliseconds"), "trading_gate_enabled": False}
        ident = stable_id("analog_account_contract_v1", frozen)
        self.ledger.save_experiment(ident, frozen["frozen_at"], stable_id(frozen["configuration"], frozen["comparison"]), code_hash, frozen)
        return {**frozen, "experiment_id": ident}

    @staticmethod
    def _quote(event: dict, code: str, *, max_age: float, side: str = "mark") -> dict:
        quote = (event.get("quotes") or {}).get(code) or {}
        at = event_time("", event.get("at"))
        observed = event_time(at[:10].replace("-", ""), quote.get("observed_at")) if at else ""
        if (not observed or observed > at or observed[:10] != at[:10]
                or (datetime.fromisoformat(at) - datetime.fromisoformat(observed)).total_seconds() > max_age
                or not number(quote.get("last_price")) or number(quote.get("last_price")) <= 0):
            return {}
        if side != "mark" and (not number(quote.get("volume")) or quote.get("suspended")
                              or quote.get("limit_up_locked" if side == "buy" else "limit_down_locked")):
            return {}
        if side != "mark":
            price = number(quote.get("ask_price" if side == "buy" else "bid_price", quote.get("last_price")))
            if price is None or price <= 0:
                return {}
        return quote

    def replay(self, contract_id: str, events: list[dict], *, trading_dates: list[str], research: bool = False,
               historical_states: list[dict] | None = None) -> dict:
        from pathlib import Path
        contract = self.ledger.experiment(contract_id).get("report") or {}
        current_hash = stable_id(Path(__file__).read_text(encoding="utf-8"),
                                 Path(__file__).with_name("signal_comparison.py").read_text(encoding="utf-8"))
        if (contract.get("kind") != "analog_account_contract" or contract.get("configuration") != asdict(self.config)
                or contract.get("comparison") != asdict(self.comparison) or contract.get("code_hash") != current_hash):
            raise ValueError("Frozen configuration/code mismatch")
        dates = sorted(set(trading_dates))
        if dates != trading_dates or not dates or any(len(d) != 8 or not d.isdigit() for d in dates):
            raise ValueError("Supply an ordered real trading calendar")
        # The caller supplies the real calendar; no weekday-only approximation.
        if not events or any(event_time("", e.get("at")) != e.get("at") for e in events):
            raise ValueError("Canonical observation timestamps required")
        if [e["at"] for e in events] != sorted(e["at"] for e in events):
            raise ValueError("Tape must be chronological")
        ident = stable_id("analog_account_replay_v1", contract_id, events, dates, research, historical_states or [])
        existing = self.ledger.experiment(ident)
        if existing:
            return existing["report"]
        accounts = {name: {"account_key": f"analog:{ident}:{name}", "cash": self.config.initial_capital,
                    "positions": {}, "fills": [], "nav": [], "pending": [], "comparisons": [], "seen": set(),
                    "rejected": [], "fees": 0., "turnover": 0.}
                    for name in ("original_rules", "analog_filter", "analog_rotation")}
        completed = []
        blocked = ""
        for day in dates:
            daily = [e for e in events if e["at"][:10].replace("-", "") == day]
            closes = [e for e in daily if e.get("session_close")]
            if len(closes) != 1 or closes[0] is not daily[-1]:
                blocked = f"missing_unique_session_close:{day}"
                break
            checkpoint = copy.deepcopy(accounts)
            for event in daily:
                for name, account in accounts.items():
                    self._execute_pending(account, event, name, day, research)
                    for exit_event in event.get("exits") or []:
                        exit_event = {"code": exit_event} if isinstance(exit_event, str) else exit_event
                        code = exit_event["code"]
                        if code in account["positions"]:
                            account["pending"].append({"side": "sell", "code": code, "created_at": event["at"],
                                "reason": exit_event.get("reason") or "original_exit", "shares": exit_event.get("shares"),
                                "id": stable_id(day, code, "exit", event["at"], exit_event.get("shares"))})
                    for signal in event.get("signals") or []:
                        signal_id = signal.get("signal_id")
                        if not signal_id or signal_id in account["seen"]:
                            continue
                        account["seen"].add(signal_id)
                        confirmed = event_time("", signal.get("confirm_at"))
                        if not confirmed or confirmed > event["at"] or confirmed[:10] != event["at"][:10]:
                            account["rejected"].append({"signal_id": signal_id, "reason": "future_or_missing_confirmation"})
                            continue
                        forecast = signal.get("forecast") or {}
                        if forecast.get("as_of") != confirmed:
                            forecast = {}
                        if not research and (not forecast.get("prediction_generated_at") or
                            forecast["prediction_generated_at"] > event["at"] or contract["frozen_at"] > event["at"]):
                            forecast = {}
                        holding_forecasts = {}
                        for code, position in account["positions"].items():
                            current = self._quote(event, code, max_age=self.config.quote_age_seconds)
                            if not current:
                                continue
                            age = len([d for d in dates if position["entry_date"] <= d <= day]) - 1
                            state = freeze_holding_state(position, as_of=event["at"], holding_days=age, quote=current,
                                market=event.get("market") or {}, sector=(event.get("sectors") or {}).get(code) or {}, config=self.comparison)
                            prediction = self.service.holding_horizon(state, historical_states or [])
                            prediction["prediction_generated_at"] = datetime.now().isoformat()
                            holding_forecasts[code] = prediction
                        account["pending"].append({"side": "buy", "signal": {**signal, "forecast": forecast},
                            "code": signal["code"], "created_at": event["at"], "id": signal_id,
                            "holding_forecasts": holding_forecasts})
            close = closes[0]
            if any(account.get("blocked_reason") for account in accounts.values()):
                blocked = next(account["blocked_reason"] for account in accounts.values() if account.get("blocked_reason"))
                accounts = checkpoint
                break
            if any(not self._quote(close, code, max_age=self.config.quote_age_seconds)
                   for account in accounts.values() for code in account["positions"]):
                accounts = checkpoint
                blocked = f"missing_holdings_close_quote:{day}"
                break
            for account in accounts.values():
                value = sum(self._quote(close, code, max_age=self.config.quote_age_seconds)["last_price"] * p["shares"]
                            for code, p in account["positions"].items())
                equity = account["cash"] + value
                account["nav"].append({"trade_date": day, "cash": account["cash"], "market_value": value,
                                       "equity": equity, "cash_utilization": value / equity if equity else 0})
                # An unfilled next-minute order expires at the session boundary.
                for order in account["pending"]:
                    account["rejected"].append({"signal_id": order["id"], "reason": "no_executable_next_observation"})
                account["pending"] = []
            completed.append(day)
        reports = {}
        for name, account in accounts.items():
            curve = [self.config.initial_capital] + [r["equity"] for r in account["nav"]]
            peak, drawdown = curve[0], 0.
            for value in curve:
                peak = max(peak, value)
                drawdown = max(drawdown, 1 - value / peak)
            reports[name] = {k: v for k, v in account.items() if k not in {"seen", "pending"}}
            reports[name]["summary"] = {"total_return_pct": (curve[-1] / curve[0] - 1) * 100,
                "max_drawdown_pct": drawdown * 100, "fees": account["fees"],
                "turnover": account["turnover"] / self.config.initial_capital,
                "mean_cash_utilization": sum(r["cash_utilization"] for r in account["nav"]) / len(account["nav"]) if account["nav"] else None,
                "fill_count": len(account["fills"]), "valued_days": len(account["nav"])}
            self.ledger.save_equity_rows(ident, account["account_key"], account["nav"], stable_id(events))
        report = clean({"kind": "analog_account_comparison", "experiment_id": ident, "contract_id": contract_id,
            "status": "blocked" if blocked else "complete", "blocked_reason": blocked, "completed_dates": completed,
            "classification": "research_event_time" if research else "forward_observed",
            "configuration": contract, "accounts": reports, "data_version": stable_id(events),
            "historical_state_version": stable_id(historical_states or []),
            "trading_gate_enabled": False, "production_release_allowed": False})
        self.ledger.save_experiment(ident, contract["frozen_at"], stable_id(asdict(self.config), asdict(self.comparison)), stable_id(events), report)
        return report

    def _sell(self, account, code, event, day, reason, shares=None):
        position = account["positions"].get(code)
        quote = self._quote(event, code, max_age=self.config.quote_age_seconds, side="sell")
        if position and not self._quote(event, code, max_age=self.config.quote_age_seconds):
            account["blocked_reason"] = f"missing_exit_quote:{day}:{code}"
        if not position or position["entry_date"] >= day or not quote:
            return False
        shares = min(int(shares), position["shares"]) if shares is not None else position["shares"]
        if shares <= 0:
            return False
        price = round(float(quote.get("bid_price") or quote["last_price"]) * (1 - self.comparison.slippage), 2)
        gross = price * shares
        allocated_cost = position["cost"] * shares / position["shares"]
        fees = gross * (self.comparison.commission_rate + self.comparison.stamp_duty_rate)
        account["cash"] += gross - fees
        account["fees"] += fees
        account["turnover"] += gross
        account["fills"].append({"fill_id": stable_id(account["account_key"], code, "sell", event["at"]),
            "code": code, "side": "sell", "at": event["at"], "price": price, "shares": shares,
            "fees": fees, "net_return": (gross - fees) / allocated_cost - 1,
            "forecast_id": position.get("forecast_id"), "entry_estimate": position.get("entry_estimate"), "reason": reason})
        position["shares"] -= shares
        position["cost"] -= allocated_cost
        if not position["shares"]:
            del account["positions"][code]
        return True

    def _execute_pending(self, account, event, variant, day, research):
        pending, account["pending"] = account["pending"], []
        for order in pending:
            if order["created_at"] >= event["at"]:
                account["pending"].append(order)
                continue
            code = order["code"]
            if order["side"] == "sell":
                if not self._sell(account, code, event, day, order["reason"], order.get("shares")):
                    account["pending"].append(order)
                continue
            signal = order["signal"]
            quote = self._quote(event, code, max_age=self.config.quote_age_seconds, side="buy")
            if not quote:
                if not self._quote(event, code, max_age=self.config.quote_age_seconds):
                    account["blocked_reason"] = f"missing_next_execution_quote:{day}:{code}"
                account["rejected"].append({"signal_id": order["id"], "reason": "next_quote_untradable"})
                continue
            if code in account["positions"] or any(f["code"] == code and f["side"] == "buy" and f["at"][:10] == event["at"][:10] for f in account["fills"]):
                continue
            prices = {c: self._quote(event, c, max_age=self.config.quote_age_seconds) for c in account["positions"]}
            if any(not p for p in prices.values()):
                account["blocked_reason"] = f"missing_holdings_intraday_quote:{day}"
                account["rejected"].append({"signal_id": order["id"], "reason": "missing_holdings_valuation"})
                continue
            equity = account["cash"] + sum(prices[c]["last_price"] * p["shares"] for c, p in account["positions"].items())
            price = round(float(quote.get("ask_price") or quote["last_price"]) * (1 + self.comparison.slippage), 2)
            lot = 200 if code.startswith("688") else 100
            required = price * lot * (1 + self.comparison.commission_rate)
            target = ""
            if variant == "original_rules":
                action = "cash_buy"
                if len(account["positions"]) >= self.config.max_positions or account["cash"] < required:
                    weak = []
                    for c, p in account["positions"].items():
                        sell = self._quote(event, c, max_age=self.config.quote_age_seconds, side="sell")
                        from core.portfolio.paper_trading_service import PaperTradingService
                        strength = PaperTradingService._holding_strength({**p,
                            "metadata": {"entry_strength_score": p["strength"]}, "last_price": prices[c]["last_price"],
                            "latest_action": (event.get("holding_actions") or {}).get(c, "hold")})
                        if p["entry_date"] < day and sell and float(signal.get("strength", 50)) >= strength + self.config.baseline_rotation_edge:
                            weak.append((strength, c))
                    target = min(weak, default=(0, ""))[1]
                    action = "rotate" if target else "no_action"
            else:
                forecast = copy.deepcopy(signal.get("forecast") or {})
                state_forecasts = order.get("holding_forecasts") or {}
                holdings = []
                for c, p in account["positions"].items():
                    sell = self._quote(event, c, max_age=self.config.quote_age_seconds, side="sell")
                    state_prediction = copy.deepcopy(state_forecasts.get(c) or {})
                    state = state_prediction.get("current_state") or {}
                    # Reject estimates for another account's cost/entry state or future publication.
                    if (state.get("entry_date") != p["entry_date"] or
                        number(state.get("features", {}).get("pnl")) is None or
                        state_prediction.get("as_of") != order["created_at"] or
                        (not research and (state_prediction.get("prediction_generated_at") or "9999") > order["created_at"])):
                        state_prediction = {}
                    gross = float(sell.get("bid_price") or sell.get("last_price") or 0) * p["shares"] * (1 - self.comparison.slippage)
                    proceeds = gross * (1 - self.comparison.commission_rate - self.comparison.stamp_duty_rate)
                    holdings.append({"code": c, "entry_date": p["entry_date"], "theme": p["theme"],
                        "forecast": state_prediction, "sellable": p["entry_date"] < day, "executable": bool(sell),
                        "proceeds": proceeds, "rotation_cost_return": self.comparison.commission_rate + self.comparison.stamp_duty_rate + self.comparison.slippage})
                comparison = self.service.compare(forecast, holdings, cash=account["cash"], required_cash=required,
                    theme=signal.get("theme") or "", market_date=day, allow_rotation=variant == "analog_rotation")
                comparison.update(signal_id=order["id"], code=code, as_of=order["created_at"],
                                  holding_forecasts=[h["forecast"] for h in holdings])
                action = comparison["recommended_research_action"]
                target = next((r["target_code"] for r in comparison["decisions"] if r["action"] == "rotate"), "")
                account["comparisons"].append(comparison)
                if len(account["positions"]) >= self.config.max_positions and action == "cash_buy":
                    action = "no_action"
            if action == "rotate":
                if not self._sell(account, target, event, day, "research_rotation"):
                    action = "no_action"
            if action not in {"cash_buy", "rotate"} or len(account["positions"]) >= self.config.max_positions:
                account["rejected"].append({"signal_id": order["id"], "reason": "comparison_gate"})
                continue
            budget = min(equity * self.config.position_pct, account["cash"])
            shares = int(budget / (price * (1 + self.comparison.commission_rate)) / 100) * 100
            if shares < lot:
                account["rejected"].append({"signal_id": order["id"], "reason": "insufficient_cash"})
                continue
            cost = shares * price
            fee = cost * self.comparison.commission_rate
            account["cash"] -= cost + fee
            account["fees"] += fee
            account["turnover"] += cost
            account["positions"][code] = {"code": code, "entry_date": day, "entry_price": price, "shares": shares,
                "cost": cost + fee, "theme": signal.get("theme") or "", "strength": signal.get("strength", 50),
                "strategy_id": signal.get("strategy_id"), "structural_stop": signal.get("structural_stop"),
                "metadata": {"strategy_version": signal.get("config_version")},
                "forecast_id": signal.get("forecast_id"), "entry_estimate": signal.get("forecast")}
            account["fills"].append({"fill_id": stable_id(account["account_key"], order["id"], "buy"),
                "code": code, "side": "buy", "at": event["at"], "price": price, "shares": shares,
                "fees": fee, "forecast_id": signal.get("forecast_id"), "reason": variant})
