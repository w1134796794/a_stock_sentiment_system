"""Context-aware, explainable exit decisions shared by live monitoring and tests."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List

POLICY_VERSION = "context-exit-v1"

ACTION_LABELS = {
    "hold": "继续持有",
    "watch": "风险观察",
    "reduce": "建议减仓",
    "sell": "建议卖出",
    "blocked": "当前无法卖出",
    "data_insufficient": "数据不足",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "--") else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ExitDecision:
    action: str
    action_label: str
    current_price: float
    protect_price: float
    pnl_pct: float
    market_state: str
    sector_state: str
    can_sell: bool
    reasons: List[str]
    evidence: Dict[str, Any]
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["reason"] = "；".join(self.reasons)
        return data


class ExitDecisionService:
    """Apply hard invalidation first, then stock/sector/market confirmation."""

    _STRATEGY_POLICY = {
        "weak_to_strong": {"activation_pct": 4.0, "trail_pct": 3.5, "weak_limit": 1},
        "first_board_launch": {"activation_pct": 5.0, "trail_pct": 4.5, "weak_limit": 2},
        "mainline_leader": {"activation_pct": 8.0, "trail_pct": 7.0, "weak_limit": 2},
        "default": {"activation_pct": 6.0, "trail_pct": 5.5, "weak_limit": 2},
    }

    def evaluate(
        self,
        position: Dict[str, Any],
        quote: Dict[str, Any],
        *,
        market_context: Dict[str, Any] | None = None,
        sector_context: Dict[str, Any] | None = None,
        signal_date: str = "",
    ) -> ExitDecision:
        market = dict(market_context or {})
        sector = dict(sector_context or {})
        current = _number(quote.get("last_price"))
        entry = _number(position.get("entry_price"))
        if current <= 0 or entry <= 0 or bool(quote.get("is_stale")):
            return self._decision(
                "data_insufficient", current, 0.0, 0.0, market, sector,
                can_sell=self._can_sell(position, signal_date),
                reasons=["实时行情缺失或已过期，暂不生成卖出判断"],
                evidence={"quote": quote, "market": market, "sector": sector},
            )

        can_sell = self._can_sell(position, signal_date)
        shares = int(position.get("shares") or 0)
        high = max(
            _number(position.get("high_watermark"), entry),
            _number(quote.get("high_price"), current),
            current,
        )
        pnl_pct = (current / entry - 1.0) * 100.0
        mfe_pct = (high / entry - 1.0) * 100.0
        drawdown_pct = (current / high - 1.0) * 100.0 if high > 0 else 0.0
        change_pct = _number(quote.get("change_pct"), pnl_pct)
        open_price = _number(quote.get("open_price"))
        structural_stop = _number(position.get("structural_stop"))
        emergency_loss = max(2.0, _number(position.get("emergency_loss_pct"), 6.0))
        emergency_stop = entry * (1.0 - emergency_loss / 100.0)

        policy = self._policy(position, market, sector)
        trailing_stop = high * (1.0 - policy["trail_pct"] / 100.0) if mfe_pct >= policy["activation_pct"] else 0.0
        protect_price = max(structural_stop, emergency_stop, trailing_stop)

        market_weak = self._market_weak(market)
        sector_weak = self._sector_weak(sector)
        market_state = "转弱" if market_weak else str(market.get("label") or market.get("regime_label") or "正常")
        sector_state = "转弱" if sector_weak else str(sector.get("label") or "正常")
        hard_reasons: List[str] = []
        stock_reasons: List[str] = []

        if shares <= 0:
            hard_reasons.append("持仓股数无效")
        if structural_stop > 0 and current <= structural_stop:
            hard_reasons.append(f"跌破结构保护价{structural_stop:.2f}")
        if current <= emergency_stop:
            hard_reasons.append(f"触及极端风险底线{emergency_stop:.2f}")
        if trailing_stop > 0 and current <= trailing_stop:
            stock_reasons.append(
                f"盈利后从高点回落{abs(drawdown_pct):.1f}%，跌破动态保护价{trailing_stop:.2f}"
            )
        if open_price > 0 and current < open_price and change_pct <= -2.0:
            stock_reasons.append("跌破开盘价且当日跌幅扩大")
        sector_change = _number(sector.get("index_change_pct"))
        if sector.get("data_completeness", 1) and change_pct - sector_change <= -2.0:
            stock_reasons.append("个股相对所属板块明显转弱")

        external_reasons: List[str] = []
        if sector_weak:
            external_reasons.append(str(sector.get("reason") or "所属板块同步转弱"))
        if market_weak:
            external_reasons.append(str(market.get("reason") or "市场与主要指数转弱"))

        evidence = {
            "entry_price": round(entry, 4),
            "current_price": round(current, 4),
            "high_watermark": round(high, 4),
            "pnl_pct": round(pnl_pct, 2),
            "mfe_pct": round(mfe_pct, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "structural_stop": round(structural_stop, 4),
            "emergency_stop": round(emergency_stop, 4),
            "trailing_stop": round(trailing_stop, 4),
            "strategy_policy": policy,
            "quote": quote,
            "market": market,
            "sector": sector,
        }

        if hard_reasons:
            action = "sell" if can_sell else "blocked"
            reasons = hard_reasons + ([] if can_sell else ["受T+1约束，当日新仓只能次日优先处理"])
        elif len(stock_reasons) >= int(policy["weak_limit"]) and (sector_weak or market_weak):
            action = "sell" if can_sell else "blocked"
            reasons = stock_reasons + external_reasons
            if not can_sell:
                reasons.append("受T+1约束，当日新仓只能次日优先处理")
        elif stock_reasons and (sector_weak or market_weak):
            action = "reduce" if can_sell else "blocked"
            reasons = stock_reasons + external_reasons
        elif sector_weak and market_weak and pnl_pct > 0:
            action = "reduce" if can_sell else "blocked"
            reasons = external_reasons + ["已有盈利，外部环境共振走弱，优先保护利润"]
        elif stock_reasons or sector_weak or market_weak:
            action = "watch"
            reasons = stock_reasons + external_reasons
            if not reasons:
                reasons = ["出现单层风险证据，暂未达到卖出条件"]
        else:
            action = "hold"
            reasons = ["个股结构、所属板块和市场环境暂未出现共振走弱"]

        return self._decision(
            action, current, protect_price, pnl_pct, market, sector,
            can_sell=can_sell, reasons=reasons, evidence=evidence,
            market_state=market_state, sector_state=sector_state,
        )

    def _policy(self, position: Dict[str, Any], market: Dict[str, Any], sector: Dict[str, Any]) -> Dict[str, float]:
        strategy = str(position.get("strategy_id") or "default")
        base = dict(self._STRATEGY_POLICY.get(strategy, self._STRATEGY_POLICY["default"]))
        if self._market_weak(market) or self._sector_weak(sector):
            base["trail_pct"] = max(2.5, base["trail_pct"] - 1.0)
        elif _number(market.get("market_score"), 50.0) >= 65 and not self._sector_weak(sector):
            base["trail_pct"] += 1.0
        return base

    @staticmethod
    def _market_weak(market: Dict[str, Any]) -> bool:
        score = _number(market.get("market_score"), 50.0)
        index_change = _number(market.get("index_change_pct"))
        regime = str(market.get("regime") or market.get("regime_label") or "")
        return bool(score < 35 or index_change <= -1.5 or any(word in regime for word in ("弱", "退潮", "冰点")))

    @staticmethod
    def _sector_weak(sector: Dict[str, Any]) -> bool:
        if sector.get("state") is False:
            return True
        breadth = sector.get("breadth")
        index_change = sector.get("index_change_pct")
        return bool(
            (breadth is not None and _number(breadth) < 0.40)
            or (index_change is not None and _number(index_change) <= -1.0)
        )

    @staticmethod
    def _can_sell(position: Dict[str, Any], signal_date: str) -> bool:
        entry_date = str(position.get("entry_date") or "").replace("-", "")
        current = str(signal_date or "").replace("-", "")
        return bool(not entry_date or not current or current > entry_date)

    @staticmethod
    def _decision(
        action: str,
        current_price: float,
        protect_price: float,
        pnl_pct: float,
        market: Dict[str, Any],
        sector: Dict[str, Any],
        *,
        can_sell: bool,
        reasons: List[str],
        evidence: Dict[str, Any],
        market_state: str = "",
        sector_state: str = "",
    ) -> ExitDecision:
        return ExitDecision(
            action=action,
            action_label=ACTION_LABELS.get(action, action),
            current_price=round(current_price, 4),
            protect_price=round(protect_price, 4),
            pnl_pct=round(pnl_pct, 2),
            market_state=market_state or str(market.get("label") or "待确认"),
            sector_state=sector_state or str(sector.get("label") or "待确认"),
            can_sell=can_sell,
            reasons=reasons,
            evidence=evidence,
        )


__all__ = ["ACTION_LABELS", "ExitDecision", "ExitDecisionService", "POLICY_VERSION"]
