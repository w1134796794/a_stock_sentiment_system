"""Receipt-time replay of persisted quotes. No providers, Redis or live effects."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from backtest.minute_entry import normalize_minute_bars
from core.infrastructure.atomic_json import atomic_read_json
from core.realtime.collection_plan import digest as plan_digest
from core.realtime.day_replay import clean, digest
from core.realtime.entry_signal_service import RealtimeEntrySignalService
from core.realtime.models import QuoteSnapshot, normalize_stock_code
from core.realtime.snapshot_signal_service import SnapshotSignalService
from core.realtime.trading_session import shanghai_now

VERSION = "archived_snapshot_receipt_v1"
KINDS = {"snapshot", "minutes", "auction", "sector_minutes"}


def aware(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError("Missing timestamp timezone")
    return shanghai_now(stamp)


def cutoff(request):
    date, until = request["market_date"], request["until"]
    stamp = shanghai_now(datetime.strptime(date + until, "%Y%m%d%H:%M:%S"))
    if stamp.strftime("%Y%m%d") != date or not "09:30:00" <= until <= "15:01:00":
        raise ValueError("Invalid snapshot replay window")
    return stamp


def load_snapshot_inputs(root, request, *, progress=None):
    """Open existing files only, including the unarchived ingestion spool."""
    end = cutoff(request)
    root = Path(root)
    date = request["market_date"]
    result = {
        "request": {**request, "fetch_missing": False},
        "plan": None,
        "bundle": None,
        "events": [],
        "load_failures": [],
        "replay_mode": "archived_snapshot",
    }
    failures = result["load_failures"]
    try:
        folder = root / "collection_plans"
        plan_path, bundle_path = folder / f"{date}.json", folder / f"{date}.inputs.json"
        if not plan_path.exists() or not bundle_path.exists():
            failures.append({"evidence": "missing_collection_inputs", "reason": "缺少当日盘前冻结名单或输入包"})
            return result
        result["plan"] = atomic_read_json(plan_path)
        result["bundle"] = atomic_read_json(bundle_path)
        validate_plan(result["plan"], result["bundle"], date)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        failures.append({"evidence": "invalid_collection_inputs", "reason": str(exc)})
        return result
    seen, conflicts = {}, set()
    # The archiver commits the daily DB before deleting the spool row. Reading
    # the spool first avoids losing events that move during this read-only scan.
    for path in (root / "market_archive" / "ingestion.sqlite3", root / "market_archive" / f"market_{date}.sqlite3"):
        if not path.exists():
            continue
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
                con.row_factory = sqlite3.Row
                pending = path.name == "ingestion.sqlite3"
                sql = "SELECT event_id,kind,code,received_at,payload FROM market_events"
                parameters = ()
                if pending:
                    sql += " WHERE market_date=? AND archived=0"
                    parameters = (date,)
                for index, source in enumerate(con.execute(sql, parameters)):
                    if progress and index % 1000 == 0:
                        progress({"stage": "读取归档快照", "completed": index, "total": 0})
                    if source["kind"] not in KINDS:
                        continue
                    try:
                        stamp = aware(source["received_at"])
                        if stamp.strftime("%Y%m%d") != date or stamp > end:
                            continue
                        payload = json.loads(source["payload"])
                        if not isinstance(payload, dict):
                            raise ValueError("Invalid event payload")
                        event = {key: source[key] for key in ("event_id", "kind", "code", "received_at")}
                        event["payload"] = payload
                        identity = event["event_id"]
                        if identity in seen and seen[identity] != event:
                            conflicts.add(identity)
                        else:
                            seen[identity] = event
                    except (ValueError, TypeError) as exc:
                        failures.append(
                            {"evidence": "invalid_archive_event", "code": source["code"], "reason": str(exc)}
                        )
        except (sqlite3.Error, OSError) as exc:
            failures.append({"evidence": "unreadable_archive", "reason": f"{path.name}: {exc}"})
    if conflicts:
        failures.append({"evidence": "conflicting_event_ids", "reason": str(len(conflicts))})
    result["events"] = sorted(
        (row for key, row in seen.items() if key not in conflicts),
        key=lambda row: (aware(row["received_at"]), row["event_id"]),
    )
    return result


def validate_plan(plan, bundle, date):
    if not isinstance(plan, dict) or not isinstance(bundle, dict):
        raise ValueError("缺少当日盘前冻结名单或输入包")
    frozen = aware(plan["frozen_at"])
    if (
        plan.get("market_date") != date
        or bundle.get("market_date") != date
        or not plan.get("candidate_date")
        or plan["candidate_date"] >= date
        or bundle.get("candidate_date") != plan["candidate_date"]
        or frozen.strftime("%Y%m%d") != date
        or frozen.strftime("%H:%M:%S") >= "09:25:00"
        or plan_digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan.get("plan_id")
        or plan_digest(bundle) != plan.get("inputs_hash")
        or plan.get("bundle_id") != bundle.get("bundle_id")
        or not isinstance(bundle.get("inputs", {}).get("risk_config"), dict)
    ):
        raise ValueError("盘前名单、冻结输入及配置的身份或哈希不一致")


class ArchiveEntryService(RealtimeEntrySignalService):
    def __init__(self, bundle, clock, sector_points):
        from core.signals.minute_amount_profile import MinuteAmountProfileRepository
        from core.signals.production_rules import minute_evaluator
        from risk.risk_config import RiskConfig

        profiles = MinuteAmountProfileRepository()
        profiles._payload = bundle["inputs"].get("minute_amount_profile") or {}
        super().__init__(
            evaluator=minute_evaluator(RiskConfig.from_dict(bundle["inputs"]["risk_config"])),
            amount_profile_repository=profiles,
            calendar=SimpleNamespace(),
            signal_stats_repository=SimpleNamespace(get=lambda *a, **kw: {}),
            sector_breadth_provider=SimpleNamespace(),
            snapshot_signal_service=SnapshotSignalService(clock=clock),
            frozen_bundle=bundle,
        )
        self.clock = clock
        self.ticks, self.auctions, self.frames = {}, {}, {}
        self.sector_points = sector_points
        self._feedback_inputs = bundle["inputs"].get("board_feedback_inputs") or {}
        from core.realtime.sector_service import RealtimeSectorService

        self.members = RealtimeSectorService.confirmation_members(bundle["inputs"])
        self.previous = {
            normalize_stock_code(r.get("code") or r.get("ts_code"), add_suffix=False): r
            for r in bundle["inputs"].get("daily_rows") or []
        }

    def _snapshot_frame(self, code, market_date):
        now = self.clock()
        values = [
            row for row in self.ticks.get(code, []) if 0 <= (now - aware(row["received_at"])).total_seconds() < 180
        ]
        self.ticks[code] = values
        return pd.DataFrame(values)

    def _auction(self, code, market_date):
        return self.auctions.get(code, {})

    def _sector_checker(self, row, code, all_rows, frames, quotes, market_date):
        from core.realtime.sector_breadth import closed_quote_checker

        policy = self.evaluator.confirmation_policy
        window = 6 if policy.mode == "legacy" else max(policy.pullback_window, policy.sustain_minutes) + 5
        boundary = self.clock().replace(second=0, microsecond=0)
        selected = {(boundary - timedelta(minutes=i)).strftime("%H:%M:%S") for i in range(window)}
        names = self._sectors(row)
        members = {member for name in names for member in self.members.get(name, [])}
        points = {
            c: {minute: value for minute, value in values.items() if minute in selected}
            for c, values in self.sector_points.items()
            if c in members
        }
        return closed_quote_checker(
            names=names,
            market_date=market_date,
            code=code,
            members=self.members,
            previous=self.previous,
            points=points,
            clock=self.clock(),
        )


def replay_snapshot_inputs(inputs, progress=None):
    """Re-evaluate the common snapshot-first rules at recorded receipt boundaries."""
    from config.settings import REALTIME_QUOTE_STALE_SECONDS

    inputs = deepcopy(inputs)
    policy = inputs.setdefault(
        "replay_policy",
        {
            "quote_stale_seconds": REALTIME_QUOTE_STALE_SECONDS,
            "snapshot_stale_seconds": 8.0,
            "consecutive_ticks": 2,
            "hot_window_seconds": 180,
            "basis": "captured_replay_policy_not_historical_proof",
        },
    )
    if (
        not math.isfinite(float(policy["quote_stale_seconds"]))
        or float(policy["quote_stale_seconds"]) < 3
        or not math.isfinite(float(policy["snapshot_stale_seconds"]))
        or float(policy["snapshot_stale_seconds"]) < 3
        or policy["consecutive_ticks"] != 2
        or policy["hot_window_seconds"] != 180
    ):
        raise ValueError("Invalid replay freshness policy")
    request = inputs["request"]
    date, end = request["market_date"], cutoff(request)
    versions = {}
    for name in (
        "snapshot_replay.py",
        "snapshot_signal_service.py",
        "entry_signal_service.py",
        "sector_breadth.py",
        "collection_plan.py",
        "models.py",
        "day_replay.py",
        "sector_service.py",
        "listing_evidence.py",
        "runtime_diagnostics.py",
    ):
        path = Path(__file__).with_name(name)
        versions[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in (
        "backtest/minute_entry.py",
        "backtest/confirmation_quality.py",
        "backtest/reversal_entry.py",
        "core/signals/production_rules.py",
        "core/signals/sector_sync.py",
        "core/signals/opening.py",
        "core/signals/sector_confirmation.py",
        "core/signals/minute_amount_profile.py",
        "core/signals/board_feedback.py",
        "core/utils/price_limit.py",
        "core/utils/stock_eligibility.py",
        "core/data/minute_quality.py",
        "risk/risk_config.py",
    ):
        versions[name] = hashlib.sha256((Path(__file__).resolve().parents[2] / name).read_bytes()).hexdigest()
    report = {
        "classification": VERSION,
        "replay_mode": "archived_snapshot",
        "market_date": date,
        "synthetic": bool(inputs.get("synthetic")),
        "until": request["until"],
        "profile": request["profile"],
        "candidate_date": "",
        "input_basis": "archived_receipt_time",
        "input_hash": digest(inputs),
        "code_hash": digest(versions),
        "code_versions": versions,
        "replay_inputs": inputs,
        "generated_at": shanghai_now().isoformat(),
        "notification_sent": False,
        "account_modified": False,
        "live_runtime_verified": False,
        "remote_fetch_performed": False,
        "formal_eligible": False,
        "complete": False,
        "failures": deepcopy(inputs.get("load_failures") or []),
        "stocks": [],
        "counts": {},
        "confirmed_stocks": 0,
        "limitations": [
            "仅使用盘前冻结输入和实际归档；不查询接口、不读取实时缓存、不发送通知。",
            "按实际接收时间推进规则回放，不伪造3秒采样点，不向前填充缺失行情。",
            "接收时间不是Redis提交时间；缺少原始租约、健康门禁或循环证据时不证明当时能推送。",
            "分钟数据只在归档的接收时点后可见，板块仅使用有发布时间留档的闭合分钟。",
            "使用当前代码与冻结配置，不证明历史代码版本相同；下一快照成交仅为模拟。",
            "原始汇总状态可能被后续覆盖；确认对账只使用截止前逐次运行观测，缺记录不代表当时未确认。",
        ],
    }
    try:
        plan, bundle = inputs.get("plan"), inputs.get("bundle")
        validate_plan(plan, bundle, date)
    except (ValueError, KeyError, TypeError) as exc:
        report["failures"].append({"evidence": "invalid_collection_inputs", "reason": str(exc)})
        return report
    from core.signals.production_rules import production_candidates

    report.update(
        candidate_date=plan["candidate_date"], bundle_id=bundle["bundle_id"], collection_plan_id=plan["plan_id"]
    )
    plan_codes = {row["code"] for row in plan["rows"]}
    rows = [row for row in production_candidates(bundle["inputs"]) if row["code"] in plan_codes]
    wanted = set(request.get("codes") or [])
    source = "leader_pool" if request["profile"] == "leader_pool" else "decision_pool"
    selected = {row["code"] for row in plan["rows"] if source in row["sources"]}
    if wanted:
        selected &= wanted
    report["requested_codes_not_in_pool"] = sorted(wanted - selected)
    stocks = {
        row["code"]: {
            "code": row["code"],
            "name": row.get("name", ""),
            "first_confirm_time": "",
            "first_evaluated_at": "",
            "transitions": [],
            "snapshot_count": 0,
            "evaluation_count": 0,
            "source_counts": {},
            "gaps": [],
        }
        for row in rows
        if row["code"] in selected
    }
    report["stocks"] = list(stocks.values())
    if not stocks:
        report["failures"].append({"evidence": "empty_frozen_scope", "reason": "冻结采集名单中没有所选候选"})
        return report
    now = [shanghai_now(datetime.strptime(date + "09:15:00", "%Y%m%d%H:%M:%S"))]
    sector_points = {}
    service = ArchiveEntryService(bundle, lambda: now[0], sector_points)
    service.snapshot_signals.max_snapshot_age_seconds = float(policy["snapshot_stale_seconds"])
    report["replay_policy"] = policy
    quotes, source_counts, last_keys, last_receipts = {}, defaultdict(Counter), {}, {}
    receipt_history = defaultdict(list)
    events, invalid, seen = [], Counter(), set()
    member_codes = {c for values in service.members.values() for c in values}
    for event in inputs.get("events") or []:
        try:
            stamp = aware(event["received_at"])
            raw, kind, code = event["payload"], event["kind"], event["code"]
            if stamp > end:
                continue
            if (
                stamp.strftime("%Y%m%d") != date
                or kind not in KINDS
                or not isinstance(raw, dict)
                or code != raw.get("code")
                or code not in plan_codes | member_codes
                or aware(raw["received_at"]) != stamp
                or raw.get("synthetic")
            ):
                raise ValueError("Invalid archive identity")
            identity = event["event_id"]
            if identity in seen:
                continue
            seen.add(identity)
            if kind == "snapshot":
                if (
                    code not in plan_codes
                    or raw.get("date") != date
                    or raw.get("source") != "eltdx_batch"
                    or not math.isfinite(float(raw["last_price"]))
                    or float(raw["last_price"]) <= 0
                ):
                    raise ValueError("Invalid snapshot")
                datetime.strptime(raw["time"], "%H:%M:%S")
            events.append((stamp, event))
        except (ValueError, KeyError, TypeError, OverflowError):
            invalid["invalid_archive_event"] += 1
    events.sort(key=lambda value: (value[0], value[1]["event_id"]))
    report["archive_event_counts"] = dict(Counter(event["kind"] for _, event in events))
    # Process all data received at the same instant before evaluating the prefix.
    from itertools import groupby

    def evaluate_at(stamp, selected_codes, boundary):
        now[0] = stamp
        quote_map = {
            code: QuoteSnapshot.from_raw(raw)
            .with_freshness(stale_after_seconds=float(policy["quote_stale_seconds"]), now=stamp)
            .to_dict()
            for code, raw in quotes.items()
        }
        for row in rows:
            code = row["code"]
            if code not in stocks or code not in selected_codes:
                continue
            stock = stocks[code]
            quote = quote_map.get(code, {"code": code, "date": date, "is_stale": True})
            signal = service._evaluate_entry(
                row,
                quote,
                service.frames.get(code, pd.DataFrame()),
                service.frames,
                quote_map,
                service.previous.get(code, {}),
                date,
                rows,
                {},
            )
            signal.pop("evaluation_timing", None)
            if not (signal.get("condition_checks") or {}).get("source_policy"):
                signal["trigger_source"] = "not_evaluated"
            source_counts[code][signal.get("trigger_source", "unknown")] += 1
            stock["evaluation_count"] += 1
            stock.update(
                status=signal["signal_status"],
                closing_status=signal["signal_status"],
                reason=signal["reason"],
                last_signal=clean(signal),
                opening_evidence=signal.get("opening_evidence", {}),
            )
            stock["last_evaluated_at"] = stamp.isoformat()
            if signal["signal_status"] == "confirmed" and not stock["first_confirm_time"]:
                stock.update(
                    first_confirm_time=signal.get("confirm_time") or quote.get("time", ""),
                    first_evaluated_at=stamp.strftime("%H:%M:%S"),
                    first_evaluated_at_iso=stamp.isoformat(),
                )
            checks = signal.get("condition_checks") or {}
            latest = (checks.get("latest") or {}).get("checks") or {}
            key = digest(
                {
                    "status": signal["signal_status"],
                    "reason": signal["reason"],
                    "confirm_time": signal.get("confirm_time"),
                    "entry_time": signal.get("entry_time"),
                    "source": signal.get("trigger_source"),
                    "fallback": checks.get("fallback_reason"),
                    "gates": {k: v for k, v in latest.items() if v is None or isinstance(v, bool)},
                }
            )
            if key != last_keys.get(code):
                stock["transitions"].append(
                    {"evaluated_at": stamp.isoformat(), "evaluation_boundary": boundary, **clean(signal)}
                )
                last_keys[code] = key

    names_by_code = {row["code"]: service._sectors(row) for row in rows}
    for index, (stamp, grouped) in enumerate(groupby(events, key=lambda value: value[0])):
        now[0] = stamp
        changed, sector_changed = set(), set()
        for _, event in grouped:
            raw, code, kind = event["payload"], event["code"], event["kind"]
            changed.add(code)
            if kind == "snapshot":
                if code in quotes and str(raw.get("time", "")) < str(quotes[code].get("time", "")):
                    invalid["regressed_snapshot_source_time"] += 1
                    continue
                if code in last_receipts:
                    gap = (stamp - last_receipts[code]).total_seconds()
                    prior = last_receipts[code]
                    same_session = (
                        "09:30:00" <= prior.strftime("%H:%M:%S") <= stamp.strftime("%H:%M:%S") <= "11:30:00"
                        or "13:00:00" <= prior.strftime("%H:%M:%S") <= stamp.strftime("%H:%M:%S") <= "15:00:00"
                    )
                    if gap > service.snapshot_signals.max_snapshot_age_seconds and same_session and code in stocks:
                        stocks[code]["gaps"].append(
                            {"from": prior.isoformat(), "to": stamp.isoformat(), "seconds": gap}
                        )
                last_receipts[code] = stamp
                quotes[code] = raw
                service.ticks.setdefault(code, []).append(raw)
                if code in stocks:
                    stocks[code]["snapshot_count"] += 1
                    stocks[code].setdefault("first_quote_received_at", stamp.isoformat())
                    receipt_history[code].append(stamp)
            elif kind == "minutes":
                try:
                    from core.data.minute_quality import inspect_minute_bars

                    original = pd.DataFrame(raw.get("bars") or [])
                    quality = inspect_minute_bars(original, date, expected_code=code, require_full_day=False, now=stamp)
                    # Missing minutes remain missing; malformed identities/prices are rejected.
                    if any(not issue.startswith(("缺少 ", "有效交易分钟不足")) for issue in quality["issues"]):
                        raise ValueError("Invalid received minute evidence")
                    frame = normalize_minute_bars(original)
                    if not frame.empty:
                        service.frames[code] = frame[frame["time"] < stamp.strftime("%H:%M") + ":00"].copy()
                except (ValueError, TypeError, KeyError):
                    invalid["invalid_minute_payload"] += 1
            elif kind == "auction":
                service.auctions[code] = raw
            else:
                if raw.get("source") != "collector_closed_quote_minutes" or raw.get("date") != date:
                    invalid["invalid_sector_publication"] += 1
                    continue
                sector_changed.add(code)
                for point in raw.get("points") or []:
                    try:
                        minute = point["time"]
                        minute_end = shanghai_now(datetime.strptime(date + minute, "%Y%m%d%H:%M:%S"))
                        received = aware(point["received_at"])
                        if (
                            point["code"] != code
                            or point.get("source") != "eltdx_batch"
                            or not 0 <= (minute_end - received).total_seconds() < 60
                            or minute_end > stamp
                            or not math.isfinite(float(point["price"]))
                            or float(point["price"]) <= 0
                        ):
                            raise ValueError("Invalid closed sector point")
                        prior = sector_points.get(code, {}).get(minute)
                        if prior and received <= aware(prior["received_at"]):
                            continue
                        sector_points.setdefault(code, {})[minute] = point
                    except (ValueError, KeyError, TypeError):
                        invalid["invalid_sector_point"] += 1
        if not (
            "09:30:00" <= stamp.strftime("%H:%M:%S") <= "11:30:00"
            or "13:00:00" <= stamp.strftime("%H:%M:%S") <= "15:00:00"
        ):
            continue
        affected = changed | {
            code
            for code, names in names_by_code.items()
            if any(sector_changed.intersection(service.members.get(name, [])) for name in names)
        }
        evaluate_at(stamp, affected, "archive_receipt")
        if progress and index % 20 == 0:
            progress(
                {
                    "stage": "归档快照驱动回放",
                    "completed": index + 1,
                    "total": len(events),
                    "minute": stamp.strftime("%H:%M:%S"),
                }
            )
    evaluate_at(
        end,
        {code for code, stock in stocks.items() if stock.get("last_evaluated_at") != end.isoformat()},
        "cutoff_freshness_check",
    )
    for evidence, count in invalid.items():
        report["failures"].append({"evidence": evidence, "reason": str(count)})
    for code, stock in stocks.items():
        stock.setdefault("status", "missing_evidence")
        stock.setdefault("reason", "缺少归档快照及可评估事件")
        stock["source_counts"] = dict(source_counts[code])
        stock["ever_confirmed"] = bool(stock["first_confirm_time"])
        stock["closing_status"] = stock["status"]
        stock["board_feedback"] = clean(service._board_feedback_context(code, date))
        stock["last_quote_received_at"] = last_receipts[code].isoformat() if code in last_receipts else ""
        coverage = []
        for start_time, end_time in (("09:30:00", "11:30:00"), ("13:00:00", "15:00:00")):
            start = shanghai_now(datetime.strptime(date + start_time, "%Y%m%d%H:%M:%S"))
            finish = min(end, shanghai_now(datetime.strptime(date + end_time, "%Y%m%d%H:%M:%S")))
            if finish <= start:
                continue
            observed = [at for at in receipt_history[code] if start <= at <= finish]
            tolerance = service.snapshot_signals.max_snapshot_age_seconds
            coverage.append(
                {
                    "from": start.isoformat(),
                    "to": finish.isoformat(),
                    "samples": len(observed),
                    "head_missing_seconds": (observed[0] - start).total_seconds()
                    if observed
                    else (finish - start).total_seconds(),
                    "tail_missing_seconds": (finish - observed[-1]).total_seconds()
                    if observed
                    else (finish - start).total_seconds(),
                }
            )
            if (
                not observed
                or (observed[0] - start).total_seconds() > tolerance
                or (finish - observed[-1]).total_seconds() > tolerance
            ):
                report["failures"].append(
                    {
                        "code": code,
                        "evidence": "incomplete_snapshot_session",
                        "reason": f"{start_time}至{finish.strftime('%H:%M:%S')}首尾采样不足",
                    }
                )
        stock["archive_coverage"] = coverage
        if not stock["snapshot_count"]:
            stock.update(
                status="missing_evidence",
                closing_status="missing_evidence",
                reason="未归档个股快照，不能用收盘报价替代",
            )
            report["failures"].append(
                {"code": code, "evidence": "missing_archived_snapshots", "reason": "未归档个股快照，不能用收盘报价替代"}
            )
        elif stock["snapshot_count"] < service.snapshot_signals.consecutive_ticks:
            report["failures"].append(
                {"code": code, "evidence": "insufficient_snapshot_sequence", "reason": "归档快照不足以构成连续确认序列"}
            )
        if code not in service.frames:
            report["failures"].append(
                {"code": code, "evidence": "missing_received_minutes", "reason": "缺少带接收时间的分钟归档"}
            )
        if stock["gaps"]:
            report["failures"].append(
                {"code": code, "evidence": "snapshot_gaps", "reason": f"采样空窗 {len(stock['gaps'])} 段"}
            )
    if not sector_points:
        report["failures"].append(
            {"evidence": "missing_published_sector_minutes", "reason": "缺少板块闭合分钟发布时间留档，不回填历史可见性"}
        )
    report["confirmed_stocks"] = sum(s["ever_confirmed"] for s in stocks.values())
    report["counts"] = dict(Counter(s["status"] for s in stocks.values()))
    report["evaluation_count"] = sum(s["evaluation_count"] for s in stocks.values())
    for stock in stocks.values():
        last = stock.get("last_signal") or {}
        if last.get("data_status") in {
            "snapshot_missing_sector",
            "missing_sector",
            "snapshot_structure_pending",
            "snapshot_missing_amount_profile",
            "missing_session_open",
            "missing_auction",
        }:
            report["failures"].append(
                {"code": stock["code"], "evidence": last["data_status"], "reason": last["reason"]}
            )
    report["complete"] = bool(events) and not report["failures"]
    return clean(report)


def run_snapshot_replay(root, request, progress=None):
    report = replay_snapshot_inputs(load_snapshot_inputs(root, request, progress=progress), progress)
    from config.settings import APP_DB_PATH, HOLDING_DB_PATH
    from scripts.diagnose_intraday_signal import runtime_evidence

    codes = [stock["code"] for stock in report["stocks"]]
    report["recorded_runtime"] = {
        "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", request["market_date"], codes),
        "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", request["market_date"], codes),
    }
    from core.realtime.runtime_diagnostics import attach_runtime_diagnostics

    return attach_runtime_diagnostics(report)
