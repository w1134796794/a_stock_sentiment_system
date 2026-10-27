"""Single-owner, storage-only strategy process driven by market revisions."""
from __future__ import annotations

from copy import deepcopy
import math
from threading import Event
from time import monotonic
from uuid import uuid4

from core.infrastructure.shared_state import TaskLease, get_shared_state_backend
from core.realtime.collection_plan import DailyCollectionPlan
from core.realtime.market_events import CHANNEL, REVISION_KEY
from core.realtime.trading_session import realtime_session_status, shanghai_now


RESULT_KEY = "realtime:strategy:result:"
HEALTH_KEY = "realtime:strategy:health"


def stored_strategy_view(date, *, profile="production", limit=10000, backend=None):
    backend = backend or get_shared_state_backend()
    value = backend.get_json(RESULT_KEY + date)
    plan = DailyCollectionPlan(backend=backend).load(date)
    if not value:
        return {"ok": False, "market_date": date, "candidate_date": (plan or {}).get("candidate_date"),
                "profile": profile, "rows": [], "counts": {}, "reason": "等待独立策略进程发布结果",
                "collection_plan_id": (plan or {}).get("plan_id")}
    value = deepcopy(value)
    source = "leader_pool" if profile == "leader_pool" else "decision_pool"
    if profile != "production":
        allowed = {r["code"] for r in (plan or {}).get("rows") or [] if source in r["sources"]}
        value["rows"] = [r for r in value["rows"] if r["code"] in allowed and
                         (profile in {"leader_pool", "decision_pool", ""} or r.get("strategy_id") == profile)]
    value["rows"] = value["rows"][:max(1, limit)]
    now = shanghai_now()
    from datetime import datetime
    try:
        age = (now - shanghai_now(datetime.fromisoformat(value["generated_at"]))).total_seconds()
    except (ValueError, KeyError):
        age = float("inf")
    value["result_age_seconds"] = age if math.isfinite(age) else None
    from config.settings import REALTIME_QUOTE_STALE_SECONDS
    # A stopped worker must never leave a stale green executable confirmation.
    for row in value["rows"]:
        try:
            quote_age = (now - shanghai_now(datetime.fromisoformat(row.get("received_at") or ""))).total_seconds()
        except (ValueError, TypeError):
            quote_age = float("inf")
        if age > REALTIME_QUOTE_STALE_SECONDS or age < -1 or not -1 <= quote_age <= REALTIME_QUOTE_STALE_SECONDS:
            if row.get("confirm_status") == "confirmed":
                row.update(status="observe", confirm_status="observe", status_text="结果已过期",
                           reason="独立策略结果已过期，等待更新", health_gate_reason="策略结果已过期")
            value["ok"] = False
    value["counts"] = {key: sum(r.get("confirm_status") == key for r in value["rows"])
                       for key in ("confirmed", "confirmed_history", "observe", "cancelled", "unfilled")}
    value["counts"]["confirmed"] += value["counts"]["confirmed_history"]
    value["profile"] = profile
    return value


class StrategyRuntime:
    def __init__(self, *, backend=None, plans=None, ledger=None, notifications=False, clock=shanghai_now):
        from core.operations.ledger import TradingLedger
        self.backend = backend or get_shared_state_backend()
        self.plans = plans or DailyCollectionPlan(backend=self.backend)
        self.ledger = ledger or TradingLedger()
        self.notifications = notifications
        self.clock = clock
        self.stop_event = Event()
        self._date = ""
        self._version = ""
        self._completed = 0
        self._loaded_plan_id = ""
        self.outbox = None
        self._lease = None
        self._last_rows = {}
        self._seen_versions = {}
        self._minute_boundary = ""
        from core.realtime.debug_journal import IntradayDebugJournal
        self.debug_journal = IntradayDebugJournal(clock=clock)

    def _load(self, date):
        from core.realtime.entry_signal_service import RealtimeEntrySignalService
        from core.realtime.read_only_market import ReadOnlyMarketData
        from core.realtime.quote_service import RealtimeQuoteService
        from core.signals.minute_amount_profile import MinuteAmountProfileRepository
        from core.signals.production_rules import minute_evaluator, production_candidates
        from risk.risk_config import RiskConfig
        if self._date == date and self._loaded_plan_id:
            return
        plan = self.plans.load(date)
        bundle = self.plans.bundle(date) if plan else None
        if not plan or not bundle:
            raise ValueError("缺少当日盘前采集名单或冻结策略输入，禁止盘中重建")
        if self._loaded_plan_id == plan["plan_id"]:
            return
        self.plan, self.bundle = plan, bundle
        from core.realtime.persistent_market import PersistentQuoteProvider
        reader = ReadOnlyMarketData(frozen=bundle, provider=PersistentQuoteProvider(backend=self.backend))
        self.market_reader = reader
        self.quotes = RealtimeQuoteService(data_manager=reader, ttl_seconds=0)
        profiles = MinuteAmountProfileRepository()
        profiles._payload = bundle["inputs"].get("minute_amount_profile") or {}
        evaluator = minute_evaluator(RiskConfig.from_dict(bundle["inputs"]["risk_config"]))
        self.signals = RealtimeEntrySignalService(data_manager=reader, quote_service=self.quotes,
            evaluator=evaluator, frozen_bundle=bundle, amount_profile_repository=profiles)
        self.signals.snapshot_signals.state_backend = self.backend
        self.signals.debug_journal = self.debug_journal
        self.signals.persistent_evaluation_enabled = True
        self.signals.evaluation_clock = self.clock
        self.signals.minute_ttl_seconds = 0
        self.rows = production_candidates(bundle["inputs"])
        allowed = {r["code"] for r in plan["rows"]}
        self.rows = [r for r in self.rows if r["code"] in allowed]
        active = {row["code"] for row in self.rows}
        self.execution_audit = {
            "selection_policy": "decision_candidate_precedence_one_execution_per_stock",
            "planned_count": len(allowed), "active_count": len(active),
            "not_executed_codes": sorted(allowed - active),
            "rows": [{"code": row["code"], "sources": row.get("execution_sources") or [],
                      "execution_pool": row.get("execution_pool"), "strategy_id": row.get("strategy_id"),
                      "allowed_entry_modes": (row.get("strategy_execution") or {}).get("allowed_entry_modes") or [],
                      "acceleration_eligibility": row.get("acceleration_eligibility") or {}}
                     for row in self.rows],
        }
        self._last_rows, self._seen_versions = {}, {}
        self._loaded_plan_id = plan["plan_id"]

    def _prepare(self, payload):
        from core.operations.evidence import attach_evidence
        from core.operations.health import block_realtime_confirmation, gate_realtime_payload, realtime_readiness
        from core.realtime.quote_cache import RealtimeQuoteCache
        health = self.quotes.dm.provider.cache.health()
        payload["execution_health"] = realtime_readiness(self.bundle["health"], health, payload["market_date"])
        gate_realtime_payload(payload, collector=health)
        if (health.get("collector") or {}).get("collection_plan_id") != self.plan["plan_id"]:
            payload["execution_health"]["ready"] = False
            payload["execution_health"]["reasons"].append("采集器与策略的当日冻结名单版本未对齐")
        if not payload["execution_health"]["ready"]:
            reason = "；".join(payload["execution_health"]["reasons"])
            for row in payload.get("rows") or []:
                if row.get("confirm_status") == "confirmed":
                    block_realtime_confirmation(row, reason)
        payload = attach_evidence(payload, frozen_bundle=self.bundle)
        for row in payload.get("rows") or []:
            if not row.get("evaluation_reused"):
                self.debug_journal.record("health_gate", payload["market_date"], row["code"],
                    payload={**row, "execution_health": payload.get("execution_health"),
                             "collection_plan_id": self.plan["plan_id"],
                             "evidence_bundle_id": self.bundle["bundle_id"]})
        return payload

    def _record(self, payload):
        from core.operations.ledger import stable_id
        from core.realtime.signal_event_repository import RealtimeSignalEventRepository
        if self._lease is not None and not self._lease.is_owner():
            raise RuntimeError("策略任务租约已丢失，停止发布和入队")
        history = RealtimeSignalEventRepository()
        history.record_payload(payload)
        for row in payload["rows"]:
            status = row.get("confirm_status")
            if status in {"confirmed", "cancelled", "unfilled"} or row.get("health_gate_reason"):
                evidence = row["signal_evidence"]
                ident = row["signal_id"]
                if self.ledger.event(stable_id(ident, "signal", status, row.get("reason")), "signal", evidence, signal_id=ident):
                    self.ledger.evidence(ident, evidence)
        if self.notifications and self.outbox:
            self.outbox.enqueue(payload)

    def run_once(self, *, force=False):
        from core.realtime.overlay_service import RealtimeOverlayService
        from core.operations.evidence import record_runtime_observations
        now = shanghai_now(self.clock())
        date = now.strftime("%Y%m%d")
        session = realtime_session_status(now)
        if not force and not session["is_open"]:
            self.backend.set_json(HEALTH_KEY, {"ok": True, "session_break": session["session_break"],
                "reason": session["reason"], "updated_at": now.isoformat(), "market_date": date}, 60)
            return None
        revision = self.backend.get_json(REVISION_KEY) or {}
        version = revision.get("id") if revision.get("market_date") == date else ""
        if (not force and version == self._version and date == self._date
                and (not self._minute_boundary or self._minute_boundary == now.strftime("%Y%m%d%H%M"))):
            return None
        started = monotonic()
        self._load(date)
        from core.realtime.market_events import code_versions
        versions = code_versions(self.backend, date)
        changed = {code for code in set(versions) | set(self._seen_versions)
                   if versions.get(code) != self._seen_versions.get(code)}
        boundary = now.strftime("%Y%m%d%H%M")
        from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
        peer_dependencies = type(self.signals.sector_breadth) is not RealtimeSectorBreadthProvider
        selected = affected_codes(self.rows, self.plan, changed,
            full=force or not self._last_rows or not revision.get("scoped") or boundary != self._minute_boundary,
            peer_dependencies=peer_dependencies)
        if not selected:
            self._version, self._seen_versions = version, versions
            return None
        view = self.market_reader.provider.begin(date, asof=now)
        self.signals.sector_breadth.market_view = view
        quotes = self.quotes.refresh_quotes([r["code"] for r in self.rows if peer_dependencies or r["code"] in selected])
        quote_map = {r["code"]: r for r in quotes.get("quotes") or []}
        projector = RealtimeOverlayService.__new__(RealtimeOverlayService)
        meta = {"candidate_date": self.plan["candidate_date"], "market_date": date,
                "profile": "production", "evidence_bundle_id": self.bundle["bundle_id"]}

        def project(row, quote, signal):
            item = {**row, **projector._build_row(self.plan["candidate_date"], date, row, quote, signal)}
            item.update({"status": item["confirm_status"], "status_text": signal.get("signal_status_text") or "观察",
                         "sample_interval_seconds": quote.get("sample_interval_seconds"),
                         "is_leader_observation": row.get("is_leader_observation", False),
                         "strategy_name": row.get("strategy_name") or "", "leader_score": row.get("leader_score"),
                         "last_leader_date": row.get("last_leader_date") or ""})
            return item

        def confirmed(row, quote, signal):
            if signal.get("signal_status") == "confirmed":
                self._record(self._prepare({**meta, "rows": [project(row, quote, signal)]}))

        signals = self.signals.evaluate(self.rows, quote_map, market_date=date, on_evaluated=confirmed,
                                       evaluate_codes=selected)
        projected = {row["code"]: project(row, quote_map.get(row["code"], {}), signals.get(row["code"], {}))
                     for row in self.rows if row["code"] in selected}
        raw_rows = {**self._last_rows, **projected}
        payload = {**meta, "ok": True, "generated_at": shanghai_now(self.clock()).isoformat(),
                   "execution_audit": self.execution_audit,
                   "collection_plan_id": self.plan["plan_id"], "market_revision": version,
                   "rows": [{**deepcopy(raw_rows[row["code"]]), "evaluation_reused": row["code"] not in selected}
                            for row in self.rows]}
        payload = self._prepare(payload)
        evaluated = {**payload, "rows": [r for r in payload["rows"] if r["code"] in selected]}
        self._record(evaluated)
        self._completed += 1
        cycle = {"id": uuid4().hex, "market_date": date, "started_at": now.isoformat(),
                 "execution_scope_counts": {scope: sum(scope in (r.get("execution_sources") or []) for r in self.rows)
                                            for scope in ("leader_pool", "decision_pool")},
                 "execution_strategy_counts": {strategy: sum(r.get("strategy_id") == strategy for r in self.rows)
                                               for strategy in {r.get("strategy_id") for r in self.rows}},
                 "finished_at": shanghai_now(self.clock()).isoformat(), "duration_ms": round((monotonic() - started) * 1000, 3),
                 "changed_codes": sorted(changed), "reused_stocks": len(payload["rows"]) - len(selected),
                 "jobs": [{"profile": "production", "evaluated_stocks": len(selected),
                           "stock_timings": [{"code": r["code"], **r["evaluation_timing"]} for r in evaluated["rows"]]}]}
        payload["runtime_cycle"] = {k: v for k, v in cycle.items() if k != "jobs"}
        evaluated["runtime_cycle"] = payload["runtime_cycle"]
        record_runtime_observations(self.ledger, evaluated)
        self.ledger.event(cycle["id"], "runtime_cycle", cycle)
        self.debug_journal.record("cycle", date, payload=cycle)
        from core.realtime.signal_event_repository import RealtimeSignalEventRepository
        view = RealtimeSignalEventRepository().merge_history(deepcopy(payload))
        self.backend.set_json(RESULT_KEY + date, view, 172800)
        self.backend.set_json(HEALTH_KEY, {"ok": True, "updated_at": payload["generated_at"],
            "market_date": date, "completed_cycles": self._completed, "duration_ms": cycle["duration_ms"],
            "market_revision": version, "plan_id": self.plan["plan_id"]}, 60)
        self._date, self._version = date, version
        self._seen_versions, self._minute_boundary, self._last_rows = versions, boundary, raw_rows
        return payload

    def run(self, *, once=False):
        from loguru import logger
        if not self.backend.is_shared:
            raise RuntimeError("独立策略进程要求可用的Redis，禁止降级为进程内缓存")
        lease = TaskLease.acquire("realtime_strategy", 30)
        if lease is None:
            raise RuntimeError("已有独立策略进程持有任务锁")
        self._lease = lease
        subscription = None
        try:
            if self.notifications:
                from core.notifications.outbox import RealtimeNotificationOutbox
                self.outbox = RealtimeNotificationOutbox(self.ledger)
                self.outbox.start()
            if hasattr(self.backend, "client"):
                subscription = self.backend.client.pubsub(ignore_subscribe_messages=True)
                subscription.subscribe(self.backend._key(CHANNEL))
            while not self.stop_event.is_set() and lease.is_owner():
                try:
                    self.run_once(force=once)
                except Exception as exc:
                    self.debug_journal.record("error", shanghai_now(self.clock()).strftime("%Y%m%d"),
                        payload={"error_type": type(exc).__name__, "error": str(exc),
                                 "reason": "strategy_cycle_failed"})
                    self.backend.set_json(HEALTH_KEY, {"ok": False, "reason": str(exc),
                        "updated_at": shanghai_now(self.clock()).isoformat()}, 60)
                    logger.exception("Read-only strategy cycle failed")
                    if once:
                        raise
                if once:
                    break
                # Notification loss is harmless: every wake checks the stored revision.
                if subscription:
                    subscription.get_message(timeout=1)
                    # Short, bounded debounce; this coalesces board chunks, not confirmations.
                    self.stop_event.wait(0.1)
                    for _ in range(256):
                        if not subscription.get_message(timeout=0):
                            break
                else:
                    self.stop_event.wait(1)
        finally:
            if subscription:
                subscription.close()
            if self.outbox:
                self.outbox.stop()
            lease.release()
            self._lease = None


def affected_codes(rows, plan, changed, *, full=False, peer_dependencies=True):
    from core.realtime.collection_plan import collection_sector_names
    priority = {row["code"] for row in rows}
    # Preserve cross-candidate relative-strength dependencies on priority quotes.
    if full or (peer_dependencies and priority.intersection(changed)):
        return priority
    members = {sector["name"]: set(sector["members"]) | {str(sector.get("code") or "").split(".")[0]}
               for sector in plan["sectors"]}
    return priority.intersection(changed) | {row["code"] for row in rows if any(
        changed.intersection(members.get(name, set())) for name in collection_sector_names(row))}
