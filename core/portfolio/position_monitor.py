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
            current_date = signal_date or datetime.now().strftime("%Y%m%d")
            if not positions:
                notified = self._retry_pending_exits(account_key, current_date, set())
                return {"ok": True, "count": 0, "decisions": [], "notified": notified, "message": "暂无持仓"}
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
                position["sellable_shares"] = self.repository.sellable_shares(int(position["id"]), current_date)
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
                from core.operations.ledger import TradingLedger, stable_id

                if decision["action"] in {"reduce", "sell", "blocked"} and signal.get("changed"):
                    exit_id = stable_id("exit", current_date, position.get("id"), decision["action"])
                    ledger = TradingLedger(self.repository.db_path)
                    evidence = {
                        "signal_id": exit_id, "candidate_date": str(position.get("entry_date") or ""),
                        "market_date": current_date, "code": position.get("code"),
                        "status": decision["action"], "config_version": (position.get("metadata") or {}).get("strategy_version") or "",
                        "quote_source_time": quote.get("time") or "",
                        "quote_received_at": quote.get("received_at") or "",
                        "sector_observed_at": sector.get("observed_at") or "",
                        "trigger_reason": decision.get("reason") or "",
                        "veto_reason": "" if decision.get("can_sell") else "T+1或行情条件限制",
                        "paper_execution_time": "",
                    }
                    ledger.evidence(exit_id, evidence)
                    ledger.event(
                        stable_id(exit_id, "signal"), "exit_signal",
                        {"position_id": position.get("id"), "decision": decision, **evidence},
                        signal_id=exit_id, account_key=account_key,
                    )
                row = {
                    "position": position,
                    "quote": quote,
                    "decision": decision,
                    "signal_id": signal.get("id"),
                    "changed": bool(signal.get("changed")),
                }
                if not signal.get("notified_at") and decision["action"] in {"reduce", "sell", "blocked"}:
                    sent = self._notify(position, decision, signal_id=int(signal.get("id") or 0), signal_date=current_date)
                    notified += sent
                if auto_execute and bool(decision.get("can_sell")):
                    sold = self._execute_paper_exit(position, decision, market_price, current_date, str(quote.get("time") or now[11:19]),
                                                    signal_id=int(signal.get("id") or 0), quote=quote)
                    row["executed_shares"] = sold
                    executed += int(sold > 0)
                results.append(row)
            notified += self._retry_pending_exits(account_key, current_date, {r["signal_id"] for r in results})
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
        *,
        signal_id: int = 0,
        quote: Dict[str, Any] | None = None,
    ) -> int:
        from core.portfolio.execution_quotes import quote_error

        if quote_error(quote or {}, trade_date):
            return 0
        action = str(decision.get("action") or "")
        total = int(position.get("shares") or 0)
        if action == "sell":
            shares = total
        elif action == "reduce":
            shares = int(total * 0.5 / 100) * 100
        else:
            return 0
        shares = min(shares, self.repository.sellable_shares(int(position["id"]), trade_date))
        if shares <= 0 or price <= 0:
            return 0
        from core.operations.ledger import TradingLedger, stable_id

        exit_id = stable_id("exit", trade_date, position.get("id"), action)
        TradingLedger(self.repository.db_path).event(
            stable_id(exit_id, "paper_order", "sell"), "paper_order",
            {"code": position.get("code"), "trade_date": trade_date, "trade_time": trade_time,
             "price": price, "shares": shares, "action": "sell"},
            signal_id=exit_id, account_key=str(position.get("account_key") or "default"),
        )
        result = self.repository.sell_position(
            int(position["id"]),
            {
                "trade_date": trade_date,
                "trade_time": trade_time,
                "price": price,
                "shares": shares,
                "reason": decision.get("reason") or decision.get("action_label") or "自动模拟退出",
                "source": "auto_realtime_exit",
                "execution_key": f"portfolio-exit:{position['id']}:{signal_id or trade_date}:{action}",
                "metadata": {"decision": decision, "signal_id": exit_id},
            },
        )
        if int(result.get("executed_shares") or 0):
            ledger = TradingLedger(self.repository.db_path)
            evidence = ledger.timeline(exit_id).get("evidence") or {}
            if evidence:
                evidence["paper_execution_time"] = trade_time
                ledger.evidence(exit_id, evidence)
        return int(result.get("executed_shares") or 0)

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

    def _retry_pending_exits(self, account_key: str, signal_date: str, attempted: set) -> int:
        sent = 0
        for signal in self.repository.pending_exit_signals(account_key, signal_date):
            if signal["id"] in attempted:
                continue
            position = self.repository.get_position(signal["position_id"])
            if not position:
                continue
            decision = {**signal, "reason": "补发历史退出提醒（非新的卖出指令）：" + str(signal.get("reason") or "")}
            sent += self._notify(position, decision, signal_id=signal["id"], signal_date=signal_date)
        return sent

    def _notify(self, position: Dict[str, Any], decision: Dict[str, Any], *, signal_id: int, signal_date: str) -> int:
        notifier = self._ensure_notifier()
        result = notifier.notify_exit_signal(position, decision, signal_date=signal_date)
        sent = int(result.get("sent") or 0)
        if sent:
            from core.operations.ledger import TradingLedger, stable_id

            exit_id = stable_id("exit", signal_date, position.get("id"), decision.get("action"))
            TradingLedger(self.repository.db_path).event(
                stable_id(exit_id, "notification", "sell"), "notification",
                {"sent": sent, "code": position.get("code"), "side": "sell",
                 "action": decision.get("action"), "channel_results": result.get("results") or []},
                signal_id=exit_id, account_key=str(position.get("account_key") or "default"),
            )
        if result.get("all_delivered", bool(sent)) and signal_id:
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
