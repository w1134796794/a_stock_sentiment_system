"""Shadow comparisons on a common forward horizon, never a production buy gate."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from core.operations.ledger import stable_id
from core.signals.case_features import clean, event_time
from core.signals.similar_case_service import SimilarCaseService, SimilarityConfig, block_weights, known_label, number


@dataclass(frozen=True)
class ComparisonConfig:
    horizon_days: int = 5
    min_cases: int = 20
    min_days: int = 8
    min_effective: float = 12
    min_probability_lower: float = .4
    min_return_lower: float = 0
    min_rotation_edge: float = .01
    max_tail_loss: float = .08
    max_theme_positions: int = 2
    commission_rate: float = .0003
    stamp_duty_rate: float = .0005
    slippage: float = .001

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values())
                or self.horizon_days not in (1, 3, 5) or self.min_cases < 2 or self.min_days < 2
                or self.min_effective < 1 or not 0 <= self.min_probability_lower <= 1
                or self.min_rotation_edge < 0 or self.max_tail_loss <= 0
                or not 1 <= self.max_theme_positions <= 3
                or any(not math.isfinite(v) or not 0 <= v < .1 for v in
                       (self.commission_rate, self.stamp_duty_rate, self.slippage))):
            raise ValueError("Invalid frozen comparison configuration")

    @property
    def version(self):
        return stable_id("remaining_horizon_comparison_v1", asdict(self))

    @property
    def cost_version(self):
        return stable_id("remaining_horizon_cost_v1", self.commission_rate, self.stamp_duty_rate, self.slippage)


def freeze_holding_state(position: dict, *, as_of: str, holding_days: int,
                         quote: dict, market: dict, sector: dict, config: ComparisonConfig) -> dict:
    at = event_time("", as_of)
    price, entry = number(quote.get("last_price")), number(position.get("entry_price"))
    stop = number(position.get("structural_stop") or position.get("protection_price"))
    features = {"holding_days": holding_days, "pnl": price / entry - 1 if price and entry else None,
                "distance_to_protection": price / stop - 1 if price and stop else None,
                "market_score": number(market.get("market_score")), "sector_breadth": number(sector.get("breadth"))}
    if features["sector_breadth"] is not None and features["sector_breadth"] > 1:
        features["sector_breadth"] /= 100
    meta = position.get("metadata") or {}
    result = {"as_of": at, "code": str(position["code"]), "entry_date": position.get("entry_date"),
              "position_id": position.get("id"), "features": features,
              "reference_price": price,
              "strategy_id": position.get("strategy_id"), "config_version": meta.get("strategy_version"),
              "limit_pct": number(quote.get("limit_pct")), "theme": position.get("theme") or position.get("sector_names"),
              "cost_version": config.cost_version, "horizon_days": config.horizon_days,
              "observation_times": {"quote": quote.get("observed_at") or quote.get("time"),
                                    "market": market.get("observed_at"), "sector": sector.get("observed_at")}}
    times = [event_time(at[:10].replace("-", ""), t) for t in result["observation_times"].values()]
    received = event_time(at[:10].replace("-", ""), quote.get("received_at"))
    from datetime import datetime
    result["pit_verified"] = bool(at and all(t and t <= at and t[:10] == at[:10] for t in times)
                                  and (not received or received <= at)
                                  and holding_days >= 0
                                  and all((datetime.fromisoformat(at) - datetime.fromisoformat(t)).total_seconds() <= 90 for t in times))
    result["state_id"] = stable_id("holding_state_v1", result)
    return clean(result)


def mature_holding_state(state: dict, *, horizon_end: str, exit_quote: dict,
                         reference_price: float, available_at: str, config: ComparisonConfig) -> dict:
    """Only attach an executable forward label to an already frozen state."""
    end, available = event_time("", horizon_end), event_time("", available_at)
    price = number(exit_quote.get("bid_price") or exit_quote.get("last_price"))
    if (not state.get("pit_verified") or state.get("cost_version") != config.cost_version
            or not end or not available or not state["as_of"] < end <= available
            or event_time(end[:10].replace("-", ""), exit_quote.get("observed_at")) != end
            or reference_price <= 0 or reference_price != state.get("reference_price")
            or not price or price <= 0 or exit_quote.get("limit_down_locked") or exit_quote.get("suspended")
            or not number(exit_quote.get("volume"))):
        return {"state": state, "label": None, "reason": "missing_executable_horizon_evidence"}
    value = price * (1 - config.slippage) * (1 - config.commission_rate - config.stamp_duty_rate) / reference_price - 1
    return {"state": state, "label": {"value": value, "label_end": end, "available_at": available,
                                       "horizon_days": config.horizon_days}}


class SignalComparisonService:
    def __init__(self, config: ComparisonConfig | None = None):
        self.config = config or ComparisonConfig()

    def _summary(self, records: list[dict], values: list[float], *, as_of: str, ident: str) -> dict:
        service = SimilarCaseService([], SimilarityConfig(min_samples=self.config.min_cases,
            min_days=self.config.min_days, min_effective=self.config.min_effective))
        weights = block_weights(records, [1.] * len(records))
        reliability = service._reliability(records, weights)
        result = {"forecast_id": ident, "as_of": as_of, "horizon_days": self.config.horizon_days,
                  "cost_version": self.config.cost_version, "reliability": reliability,
                  "trading_gate_enabled": False, "basis": "fixed_horizon_net_return"}
        if not reliability["sufficient"]:
            return {**result, "status": "insufficient", "reason": "insufficient_independent_horizon_samples"}
        data = [{**r, "labels": {"net_return": {"net_return": v, "state": "mature",
                 "label_end": as_of, "available_at": as_of}}} for r, v in zip(records, values)]
        stats = service._statistics(data, weights, {}, as_of, ident)
        return clean({**result, **stats, "status": "estimated", "sample_case_ids": [r["case"]["case_id"] for r in records]})

    def entry_horizon(self, analysis: dict, records: list[dict], *, as_of: str) -> dict:
        ids = set(analysis.get("sample_case_ids") or [])
        selected, values = [], []
        for record in records:
            if record["case"]["case_id"] not in ids or not record["case"].get("pit_verified"):
                continue
            label = known_label(record, f"return_d{self.config.horizon_days}", as_of)
            value = number(label.get("value")) if label else None
            from core.operations.ledger import stable_id
            original_cost = (record.get("versions") or {}).get("cost_version")
            expected_cost = stable_id("engine_cost_v1", self.config.commission_rate,
                                      self.config.stamp_duty_rate, self.config.slippage)
            if value is not None and original_cost == expected_cost:
                # return_dN is gross mark-to-entry (entry already includes slippage).
                value = (1 + value) * (1 - self.config.slippage) * (1 - self.config.commission_rate - self.config.stamp_duty_rate) / (1 + self.config.commission_rate) - 1
                selected.append(record)
                values.append(value)
        ident = stable_id("entry_remaining_horizon_v1", analysis.get("forecast_id"), self.config.version, as_of)
        return self._summary(selected, values, as_of=as_of, ident=ident)

    def holding_horizon(self, state: dict, historical_states: list[dict]) -> dict:
        at = state["as_of"]
        candidates = []
        for record in historical_states:
            past, label = record.get("state") or {}, record.get("label") or {}
            if (not state.get("pit_verified") or not past.get("pit_verified") or not label
                    or not label.get("available_at", "") < at or not label.get("label_end", "") < at
                    or past.get("as_of", "")[:10] >= at[:10]
                    or any(not state.get(k) or past.get(k) != state.get(k) for k in
                           ("strategy_id", "config_version", "limit_pct", "cost_version", "horizon_days"))
                    or abs(past.get("features", {}).get("holding_days", -99) - state["features"]["holding_days"]) > 1
                    or past["as_of"][11:13] != at[11:13]):
                continue
            distance = 0.
            for key, scale in (("pnl", .05), ("distance_to_protection", .05), ("market_score", 20), ("sector_breadth", .2)):
                a, b = number(state["features"].get(key)), number(past["features"].get(key))
                if a is None or b is None:
                    distance = math.inf
                    break
                distance += ((a - b) / scale) ** 2
            if distance <= 4:
                candidates.append((distance, record))
        candidates.sort(key=lambda x: (x[0], x[1]["state"]["state_id"]))
        selected, seen = [], set()
        for _, record in candidates:
            past = record["state"]
            key = (past["code"], past["as_of"][:10])
            if key not in seen:
                selected.append(record)
                seen.add(key)
            if len(selected) == 40:
                break
        rows = [{"case": {"case_id": r["state"]["state_id"], "market_date": r["state"]["as_of"][:10].replace("-", ""),
                         "features": {"resonance_sectors": r["state"].get("theme")}}} for r in selected]
        result = self._summary(rows, [r["label"]["value"] for r in selected], as_of=at,
                               ident=stable_id("holding_remaining_horizon_v1", state["state_id"], self.config.version))
        return {**result, "state_id": state["state_id"], "current_state": state}

    def reliable(self, forecast: dict) -> bool:
        p = number((forecast.get("probability_interval") or {}).get("low"))
        ret = number((forecast.get("expected_return_interval") or {}).get("low"))
        tail = number((forecast.get("return_distributions") or {}).get("net_return", {}).get("p05"))
        return bool(forecast.get("status") == "estimated" and (forecast.get("reliability") or {}).get("sufficient")
                    and forecast.get("cost_version") == self.config.cost_version
                    and forecast.get("horizon_days") == self.config.horizon_days
                    and p is not None and p >= self.config.min_probability_lower
                    and ret is not None and ret > self.config.min_return_lower
                    and tail is not None and tail >= -self.config.max_tail_loss)

    def compare(self, candidate: dict, holdings: list[dict], *, cash: float, required_cash: float,
                theme: str, market_date: str, allow_rotation: bool = True) -> dict:
        decisions = [{"action": "hold", "eligible": True, "reason": "preserve_existing_positions"},
                     {"action": "no_action", "eligible": True, "reason": "no_production_rule_change"}]
        common = self.reliable(candidate)
        exposure = sum(h.get("theme") == theme for h in holdings) if theme else len(holdings)
        buy = common and bool(theme) and exposure < self.config.max_theme_positions and len(holdings) < 3 and cash >= required_cash
        decisions.append({"action": "cash_buy", "eligible": buy,
                          "reason": "ready" if buy else "prediction_cash_slots_or_theme_gate"})
        eligible = []
        for holding in holdings:
            forecast = holding.get("forecast") or {}
            costs = number(holding.get("rotation_cost_return"))
            lower = number((candidate.get("expected_return_interval") or {}).get("low"))
            upper = number((forecast.get("expected_return_interval") or {}).get("high"))
            other_exposure = exposure - int(holding.get("theme") == theme)
            edge = lower - upper - costs if lower is not None and upper is not None and costs is not None else None
            if (allow_rotation and common and forecast.get("status") == "estimated"
                    and (forecast.get("reliability") or {}).get("sufficient")
                    and forecast.get("horizon_days") == candidate.get("horizon_days")
                    and forecast.get("cost_version") == candidate.get("cost_version")
                    and forecast.get("as_of") == candidate.get("as_of") and forecast.get("state_id")
                    and str(holding.get("entry_date") or "") < market_date
                    and holding.get("sellable") is True and holding.get("executable") is True
                    and holding.get("proceeds", 0) + cash >= required_cash
                    and theme and other_exposure < self.config.max_theme_positions
                    and edge is not None and edge >= self.config.min_rotation_edge):
                eligible.append((edge, str(holding["code"])))
        target = max(eligible, default=(None, ""))
        decisions.append({"action": "rotate", "eligible": bool(target[1]), "target_code": target[1],
                          "net_edge_lower": target[0], "reason": "ready" if target[1] else "no_reliable_sellable_same_horizon_holding"})
        chosen = "cash_buy" if buy else "rotate" if target[1] else "no_action"
        return {"comparison_id": stable_id(self.config.version, candidate, holdings, cash, required_cash),
                "configuration_version": self.config.version, "candidate_forecast": candidate,
                "decisions": decisions, "recommended_research_action": chosen, "trading_gate_enabled": False}
