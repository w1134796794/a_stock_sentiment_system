"""Trading-session monitor for open holdings and contextual exit alerts."""
from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any, Callable, Dict, List

from loguru import logger

from core.infrastructure.shared_state import get_shared_state_backend
from core.portfolio.exit_decision_service import ExitDecisionService
from core.portfolio.holding_repository import HoldingRepository
from core.realtime.models import normalize_stock_code


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "--") else default
    except (TypeError, ValueError):
        return default


class PositionMonitor:
    def __init__(
        self,
        repository: HoldingRepository | None = None,
        decision_service: ExitDecisionService | None = None,
        *,
        quote_service: Any = None,
        sector_breadth_provider: Any = None,
        market_context_provider: Callable[[], Dict[str, Any]] | None = None,
        notifier: Any = None,
        backend: Any = None,
    ) -> None:
        self.repository = repository or HoldingRepository()
        self.decisions = decision_service or ExitDecisionService()
        self.quote_service = quote_service
        self.sector_breadth_provider = sector_breadth_provider
        self.market_context_provider = market_context_provider
        self.notifier = notifier
        self.backend = backend or get_shared_state_backend()
        self._lock = Lock()

    def run_once(
        self,
        *,
        account_key: str = "default",
        signal_date: str = "",
        auto_execute: bool = False,
    ) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": True, "skipped": True, "reason": "持仓监控正在运行"}
        shared_token = None
        try:
            lock_key = f"portfolio:position-monitor:{account_key}"
            shared_token = self.backend.acquire_lock(lock_key, 20)
            if not shared_token:
                return {"ok": True, "skipped": True, "reason": "其他进程正在监控持仓"}
            positions = self.repository.list_positions(account_key)
            if not positions:
                return {"ok": True, "count": 0, "decisions": [], "message": "暂无持仓"}
            current_date = signal_date or datetime.now().strftime("%Y%m%d")
            quote_payload = self._quotes([row["code"] for row in positions])
            quote_map = {
                normalize_stock_code(row.get("code"), add_suffix=False): dict(row)
                for row in quote_payload.get("quotes") or []
            }
            market = self._market_context()
            results: List[Dict[str, Any]] = []
            notified = 0
            executed = 0
            now = datetime.now().isoformat(timespec="seconds")
            for position in positions:
                quote = quote_map.get(str(position["code"]), {})
                sector = self._sector_context(position, current_date)
                decision = self.decisions.evaluate(
                    position,
                    quote,
                    market_context=market,
                    sector_context=sector,
                    signal_date=current_date,
                ).to_dict()
                current = _number(decision.get("current_price"))
                market_price = current or _number(position.get("last_price")) or _number(position.get("entry_price"))
                high = max(
                    _number(position.get("high_watermark")),
                    _number(quote.get("high_price")),
                    market_price,
                )
                self.repository.update_position_market(
                    int(position["id"]),
                    last_price=market_price,
                    high_watermark=high,
                    action=str(decision["action"]),
                    reason=str(decision["reason"]),
                )
                shares = int(position.get("shares") or 0)
                cost = _number(position.get("cost_amount"))
                value = market_price * shares
                self.repository.save_snapshot(
                    int(position["id"]),
                    {
                        "snapshot_at": now[:16] + ":00",
                        "last_price": market_price,
                        "market_value": value,
                        "unrealized_pnl": value - cost,
                        "unrealized_pnl_pct": decision["pnl_pct"],
                        "high_watermark": high,
                        "evidence": decision.get("evidence"),
                    },
                )
                signal = self.repository.save_exit_signal(
                    int(position["id"]),
                    {
                        **decision,
                        "signal_date": current_date,
                        "signal_time": now[11:19],
                    },
                )
                row = {
                    "position": position,
                    "quote": quote,
                    "decision": decision,
                    "signal_id": signal.get("id"),
                    "changed": bool(signal.get("changed")),
                }
                if row["changed"] and decision["action"] in {"reduce", "sell", "blocked"}:
                    sent = self._notify(position, decision, signal_id=int(signal.get("id") or 0), signal_date=current_date)
                    notified += sent
                if auto_execute and bool(decision.get("can_sell")):
                    sold = self._execute_paper_exit(position, decision, market_price, current_date, now[11:19])
                    row["executed_shares"] = sold
                    executed += int(sold > 0)
                results.append(row)
            return {
                "ok": True,
                "count": len(results),
                "notified": notified,
                "executed": executed,
                "market": market,
                "decisions": results,
                "generated_at": now,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PositionMonitor] 持仓监控失败: {exc}")
            return {"ok": False, "error": str(exc), "decisions": []}
        finally:
            if shared_token:
                try:
                    self.backend.release_lock(
                        f"portfolio:position-monitor:{account_key}", shared_token,
                    )
                except Exception:  # pragma: no cover
                    pass
            self._lock.release()

    def _execute_paper_exit(
        self,
        position: Dict[str, Any],
        decision: Dict[str, Any],
        price: float,
        trade_date: str,
        trade_time: str,
    ) -> int:
        action = str(decision.get("action") or "")
        total = int(position.get("shares") or 0)
        if action == "sell":
            shares = total
        elif action == "reduce":
            shares = int(total * 0.5 / 100) * 100
        else:
            return 0
        if shares < 100 or price <= 0:
            return 0
        self.repository.sell_position(
            int(position["id"]),
            {
                "trade_date": trade_date,
                "trade_time": trade_time,
                "price": price,
                "shares": shares,
                "reason": decision.get("reason") or decision.get("action_label") or "自动模拟退出",
                "source": "auto_realtime_exit",
                "metadata": {"decision": decision},
            },
        )
        return shares

    def _quotes(self, codes: List[str]) -> Dict[str, Any]:
        service = self._ensure_quote_service()
        return service.get_quotes(codes) if service is not None else {"quotes": []}

    def _sector_context(self, position: Dict[str, Any], market_date: str) -> Dict[str, Any]:
        names = [
            item.strip()
            for item in str(position.get("sector_names") or "").replace("，", ",").split(",")
            if item.strip()
        ]
        provider = self._ensure_sector_breadth_provider()
        if provider is None or not names:
            return {"state": None, "data_completeness": 0.0, "reason": "持仓未配置所属板块"}
        state, detail = provider.evaluate(names, market_date)
        return {**dict(detail or {}), "state": state}

    def _market_context(self) -> Dict[str, Any]:
        if self.market_context_provider is not None:
            return dict(self.market_context_provider() or {})
        try:
            from core.realtime.sector_service import RealtimeSectorService

            payload = RealtimeSectorService().get_market_quotes(limit=20)
            rows = payload.get("quotes") or []
            changes = [
                _number(row.get("change_pct")) for row in rows
                if row.get("change_pct") not in (None, "")
            ]
            average = sum(changes) / len(changes) if changes else 0.0
            falling_ratio = sum(value < 0 for value in changes) / len(changes) if changes else 0.0
            score = max(0.0, min(100.0, 50.0 + average * 10.0 - max(falling_ratio - 0.5, 0) * 30.0)) if changes else 50.0
            weak = bool(changes and (average <= -1.0 or falling_ratio >= 0.7))
            return {
                "market_score": round(score, 1),
                "index_change_pct": round(average, 2),
                "regime": "转弱" if weak else "正常",
                "label": "主要指数转弱" if weak else "主要指数正常",
                "reason": "主要指数多数下跌" if weak else "主要指数未形成共振走弱",
                "index_count": len(changes),
                "falling_ratio": round(falling_ratio, 4),
                "source": str(payload.get("source") or "realtime_market"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "market_score": 50.0,
                "regime": "数据不足",
                "label": "市场数据不足",
                "reason": f"主要指数数据不足: {exc}",
                "index_count": 0,
            }

    def _notify(self, position: Dict[str, Any], decision: Dict[str, Any], *, signal_id: int, signal_date: str) -> int:
        notifier = self._ensure_notifier()
        result = notifier.notify_exit_signal(position, decision, signal_date=signal_date)
        sent = int(result.get("sent") or 0)
        if sent and signal_id:
            self.repository.mark_signal_notified(signal_id)
        return sent

    def _ensure_quote_service(self):
        if self.quote_service is None:
            from core.realtime.quote_service import RealtimeQuoteService

            self.quote_service = RealtimeQuoteService()
        return self.quote_service

    def _ensure_sector_breadth_provider(self):
        if self.sector_breadth_provider is None:
            from core.realtime.sector_breadth import RealtimeSectorBreadthProvider

            self.sector_breadth_provider = RealtimeSectorBreadthProvider(
                quote_service=self._ensure_quote_service()
            )
        return self.sector_breadth_provider

    def _ensure_notifier(self):
        if self.notifier is None:
            from core.notifications.notifier import NotificationService

            self.notifier = NotificationService()
        return self.notifier


__all__ = ["PositionMonitor"]
