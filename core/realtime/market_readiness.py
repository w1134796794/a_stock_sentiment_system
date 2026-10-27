"""Read-only operational evidence audit, separate from trading decisions."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.realtime.snapshot_replay import aware, validate_plan
from core.realtime.trading_session import shanghai_now


@dataclass(frozen=True)
class AuditLimits:
    snapshot_gap: float = 8.0
    quote_age: float = 12.0
    archive_lag: float = 30.0
    minute_lag: float = 130.0
    strategy_age: float = 30.0
    observation_gap: float = 90.0
    window_seconds: int = 180
    sector_coverage: float = 0.6
    sector_publish_lag: float = 30.0


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def read_json(path):
    # Publishers use atomic replace; read without the shared helper's writable lock file.
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def at(date, time):
    return shanghai_now(datetime.strptime(date + time, "%Y%m%d%H:%M:%S"))


def calendar_day(path, date):
    """Do not silently treat unknown dates or missing calendars as weekdays."""
    states = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if row.get("exchange") not in {None, "", "SSE"}:
                continue
            key = row["cal_date"].replace("-", "")
            datetime.strptime(key, "%Y%m%d")
            value = str(row["is_open"])
            if value not in {"0", "1"} or key in states and states[key] != value:
                raise ValueError("Invalid/conflicting real trading calendar")
            states[key] = value
    if date not in states:
        raise ValueError("Requested date is outside the real calendar; refresh the calendar separately")
    previous = max((day for day, flag in states.items() if flag == "1" and day < date), default="")
    return states[date] == "1", previous


def phase_for(date, now):
    if date < now.strftime("%Y%m%d") or now.strftime("%H:%M:%S") > "15:00:00":
        return "postclose"
    clock = now.strftime("%H:%M:%S")
    return "preopen" if clock < "09:30:00" else "break" if "11:30:00" < clock < "13:00:00" else "live"


def sessions(date, start, end):
    return [
        (max(start, at(date, left)), min(end, at(date, right)))
        for left, right in (("09:30:00", "11:30:00"), ("13:00:00", "15:00:00"))
        if max(start, at(date, left)) < min(end, at(date, right))
    ]


def continuity(stamps, date, start, end, tolerance):
    ordered = sorted(set(stamps))
    missing, gaps, maximum = [], 0, 0.0
    for left, right in sessions(date, start, end):
        values = [value for value in ordered if left <= value <= right]
        edges = [left, *values, right]
        for before, after in zip(edges, edges[1:], strict=False):
            gap = (after - before).total_seconds()
            maximum = max(maximum, gap)
            if gap > tolerance:
                gaps += 1
                if len(missing) < 8:
                    missing.append({"from": before.isoformat(), "to": after.isoformat(), "seconds": round(gap, 3)})
    return {
        "ok": bool(ordered) and gaps == 0,
        "samples": len(ordered),
        "gap_count": gaps,
        "max_gap_seconds": round(maximum, 3),
        "gap_examples": missing,
        "first": ordered[0].isoformat() if ordered else "",
        "last": ordered[-1].isoformat() if ordered else "",
    }


@contextmanager
def archive_reader(root, date):
    """UNION also deduplicates an event visible in both sides of archive handoff."""
    con = sqlite3.connect(":memory:", uri=True)
    con.row_factory = sqlite3.Row
    sources = []
    try:
        for filename in ("ingestion.sqlite3", f"market_{date}.sqlite3"):
            path = Path(root) / "market_archive" / filename
            if not path.exists():
                continue
            alias = f"source{len(sources)}"
            con.execute(f"ATTACH DATABASE ? AS {alias}", (path.resolve().as_uri() + "?mode=ro",))
            sources.append((alias, filename == "ingestion.sqlite3"))

        def read(kinds, start, end, codes=None, *, latest=False):
            if latest:
                for code in codes or []:
                    candidates = []
                    for alias, pending in sources:
                        clause = "kind IN (" + ",".join("?" for _ in kinds) + ") AND code=?"
                        parameters = [*kinds, code]
                        if pending:
                            clause += " AND market_date=? AND archived=0"
                            parameters.append(date)
                        clause += " AND julianday(received_at) BETWEEN julianday(?) AND julianday(?)"
                        parameters.extend((start.isoformat(), end.isoformat()))
                        row = con.execute(
                            f"SELECT event_id,kind,code,received_at,payload FROM {alias}.market_events "
                            f"WHERE {clause} ORDER BY received_at DESC LIMIT 1",
                            parameters,
                        ).fetchone()
                        if row is not None:
                            candidates.append(row)
                    if candidates:
                        yield max(candidates, key=lambda row: aware(row["received_at"]))
                return
            pieces, parameters = [], []
            for alias, pending in sources:
                clause = "kind IN (" + ",".join("?" for _ in kinds) + ")"
                parameters.extend(kinds)
                if pending:
                    clause += " AND market_date=? AND archived=0"
                    parameters.append(date)
                if codes is not None:
                    if not codes:
                        return
                    clause += " AND code IN (" + ",".join("?" for _ in codes) + ")"
                    parameters.extend(codes)
                clause += " AND julianday(received_at) BETWEEN julianday(?) AND julianday(?)"
                parameters.extend((start.isoformat(), end.isoformat()))
                pieces.append(
                    f"SELECT event_id,kind,code,received_at,payload FROM {alias}.market_events WHERE {clause}"
                )
            if pieces:
                for row in con.execute(" UNION ".join(pieces) + " ORDER BY received_at", parameters):
                    stamp = aware(row["received_at"])
                    if start <= stamp <= end:
                        yield row

        yield read, len(sources)
    finally:
        con.close()


class RedisAuditReader:
    """Only GET/MGET/ZRANGEBYSCORE/HMGET/PING. No shared-backend fallback."""

    def __init__(self, url, prefix):
        import redis

        self.client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3, retry_on_timeout=False
        )
        self.prefix = prefix
        self.client.ping()

    def many(self, keys):
        values = self.client.mget([f"{self.prefix}:{key}" for key in keys])
        return {key: json.loads(raw) if raw else None for key, raw in zip(keys, values, strict=True)}

    def ticks(self, date, codes, start, end):
        pipe = self.client.pipeline(transaction=False)
        for code in codes:
            pipe.zrangebyscore(f"{self.prefix}:realtime:snapshots:v2:{date}:{code}", start.timestamp(), end.timestamp())
        return {
            code: [json.loads(value) for value in values] for code, values in zip(codes, pipe.execute(), strict=True)
        }

    def sector_points(self, date, codes, minutes):
        pipe = self.client.pipeline(transaction=False)
        for code in codes:
            pipe.hmget(f"{self.prefix}:realtime:sector-quote-minutes:{date}:{code}", minutes)
        return {
            (code, minute): json.loads(raw)
            for code, values in zip(codes, pipe.execute(), strict=True)
            for minute, raw in zip(minutes, values, strict=True)
            if raw
        }

    def close(self):
        self.client.close()


class PersistentAuditReader(RedisAuditReader):
    """Match production rule inputs; Redis is used only for process health."""
    storage = "durable_spool_and_archive"

    def __init__(self, url, prefix, *, root, date, now):
        super().__init__(url, prefix)
        from core.realtime.persistent_market import PersistentMarketReader
        self.view = PersistentMarketReader(Path(root) / "market_archive").view(date, asof=now)

    def many(self, keys):
        quote_prefix, minute_prefix = "realtime:quotes:latest:", f"realtime:minutes:{self.view.date}:"
        health = [k for k in keys if not k.startswith((quote_prefix, minute_prefix))]
        values = super().many(health)
        codes = [k.removeprefix(quote_prefix) for k in keys if k.startswith(quote_prefix)]
        quotes = {r["code"]: r for r in self.view.snapshots(codes, latest=True)}
        for key in keys:
            if key.startswith(quote_prefix):
                values[key] = quotes.get(key.removeprefix(quote_prefix))
            elif key.startswith(minute_prefix):
                values[key] = self.view.minutes(key.removeprefix(minute_prefix)).to_dict("records")
        return values

    def ticks(self, date, codes, start, end):
        result = {code: [] for code in codes}
        for row in self.view.snapshots(codes, start=start, end=end):
            result[row["code"]].append(row)
        return result

    def sector_points(self, date, codes, minutes):
        points = self.view.sector_points(codes, minutes=minutes)
        return {(code, minute): row for code, values in points.items() for minute, row in values.items()}


def audit_market(
    *,
    root,
    calendar_path,
    ledger_path,
    date,
    now=None,
    phase="auto",
    live=None,
    limits=None,
    service_states=None,
    board_shadow=False,
):
    now = shanghai_now(now)
    limits = limits or AuditLimits()
    if not all(math.isfinite(value) and value > 0 for value in limits.__dict__.values()) or limits.sector_coverage > 1:
        raise ValueError("Invalid audit thresholds")
    datetime.strptime(date, "%Y%m%d")
    if date > now.strftime("%Y%m%d"):
        raise ValueError("Cannot audit a future trading day")
    phase = phase_for(date, now) if phase == "auto" else phase
    if phase not in {"preopen", "live", "break", "postclose"}:
        raise ValueError("Invalid audit phase")
    if phase in {"preopen", "live"} and date != now.strftime("%Y%m%d"):
        raise ValueError("Live/preopen checks require today's date")
    if phase == "postclose" and date == now.strftime("%Y%m%d") and now < at(date, "15:00:00"):
        raise ValueError("Today's full-day check is not due before 15:00")
    if phase == "live" and (date != now.strftime("%Y%m%d") or not sessions(date, now - timedelta(seconds=1), now)):
        raise ValueError("Live audit requires today's active trading session")
    report = {
        "classification": "market_data_readiness_v1",
        "market_date": date,
        "calendar_path": str(Path(calendar_path).resolve()),
        "phase": phase,
        "generated_at": now.isoformat(),
        "read_only": True,
        "remote_market_fetch": False,
        "notification_sent": False,
        "account_modified": False,
        "checks": [],
        "stocks": [],
        "live_ready": None,
        "replay_ready": None,
        "whole_day_replay_ready": None,
        "board_shadow_ready": None,
        "limits": limits.__dict__,
        "status": "not_ready",
        "input_storage": getattr(live, "storage", "legacy_audit_reader"),
        "notes": [
            "Readiness checks evidence availability, not whether a stock should be bought.",
            "Intraday archive checks cover a recent window; only postclose checks cover the full session.",
            "No history is backfilled. Mapped TDX trend scores do not prove THS constituent breadth.",
        ],
    }

    def check(key, ok, reason, *, scope="both", details=None, warning=False):
        report["checks"].append(
            {
                "key": key,
                "status": "pass" if ok else "warn" if warning else "fail",
                "scope": scope,
                "reason": reason,
                "details": details or {},
            }
        )

    def finish():
        common = not any(c["status"] == "fail" and c["scope"] in {"both", "preopen"} for c in report["checks"])
        if phase in {"preopen", "live"}:
            report["live_ready"] = common and not any(
                c["status"] == "fail" and c["scope"] == "live" for c in report["checks"]
            )
        if phase != "preopen":
            report["replay_ready"] = common and not any(
                c["status"] == "fail" and c["scope"] == "replay" for c in report["checks"]
            )
        if phase == "postclose":
            report["whole_day_replay_ready"] = report["replay_ready"]
        failed = any(value is False for value in (report["live_ready"], report["replay_ready"]))
        report["status"] = (
            "not_ready"
            if failed
            else "warning"
            if report["board_shadow_ready"] is False or any(c["status"] == "warn" for c in report["checks"])
            else "ready"
        )
        return report

    try:
        trading, previous = calendar_day(calendar_path, date)
        check("real_calendar", True, "真实交易日历覆盖请求日期")
        if not trading:
            report["status"] = "skipped_non_trading_day"
            return report
    except (OSError, KeyError, TypeError, ValueError) as exc:
        check("real_calendar", False, str(exc))
        return finish()
    try:
        root = Path(root)
        plan = read_json(root / "collection_plans" / f"{date}.json")
        bundle = read_json(root / "collection_plans" / f"{date}.inputs.json")
        validate_plan(plan, bundle, date)
        if plan["candidate_date"] != previous:
            raise ValueError("Frozen candidate date differs from previous real trading day")
        check(
            "frozen_inputs",
            True,
            "冻结名单、配置及输入哈希一致",
            details={"plan_id": plan["plan_id"], "candidate_date": previous},
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        check("frozen_inputs", False, str(exc))
        return finish()
    inputs = bundle["inputs"]
    check("preopen_health", (bundle.get("health") or {}).get("ok") is True, "盘前冻结包完整性状态")
    codes = sorted({row["code"] for row in plan["rows"]})
    check("collection_universe", bool(codes), "当日优先采集名单", details={"codes": codes, "count": len(codes)})
    check(
        "amount_profile",
        bool((inputs.get("minute_amount_profile") or {}).get("profiles")),
        "冻结历史同分钟成交进度模型",
    )
    from core.realtime.entry_signal_service import RealtimeEntrySignalService
    from core.realtime.sector_service import RealtimeSectorService
    from core.signals.production_rules import production_candidates

    members = RealtimeSectorService.confirmation_members(inputs)
    breadth_required = (inputs.get("risk_config") or {}).get("entry_sector_mode", "breadth_required") == "breadth_required"
    report["sector_confirmation_mode"] = "breadth_required" if breadth_required else "trend_score"
    rows = {row["code"]: row for row in production_candidates(inputs) if row["code"] in codes}
    report["active_strategy_codes"] = sorted(rows)
    daily = {
        str(row.get("code") or row.get("ts_code") or "").split(".")[0]: row for row in inputs.get("daily_rows") or []
    }
    missing_previous = [
        code
        for code in codes
        if not positive((daily.get(code) or {}).get("close"))
        or not positive(RealtimeEntrySignalService._previous_amount_yuan(daily.get(code) or {}))
    ]
    check("prior_daily", not missing_previous, "候选昨收及昨成交额", details={"missing_codes": missing_previous})
    invalid_groups = []
    expected_members = {}
    for code in codes:
        names = RealtimeEntrySignalService._sectors(rows.get(code) or {})
        if code not in rows:
            names = set(next((row.get("sectors", []) for row in plan["rows"] if row["code"] == code), []))
        expected_members[code] = {member for name in names for member in members.get(name, []) if member != code}
        invalid_groups.extend(f"{code}:{name}" for name in names if not members.get(name))
        if not names:
            invalid_groups.append(f"{code}:missing_sector_names")
    check(
        "frozen_sector_members",
        not invalid_groups,
        "同花顺绑定及清理退市后的完整成分分母",
        details={"missing": invalid_groups},
        warning=not breadth_required,
    )
    if not breadth_required:
        expected_members = {}
        report["notes"].append("Board trend is optional scoring; missing breadth or board scores do not veto stock confirmation.")
    if service_states is not None and phase in {"preopen", "live"}:
        check(
            "systemd_workers",
            all(value == "active" for value in service_states.values()) and len(service_states) == 3,
            "采集、归档、策略服务状态",
            scope="live",
            details=service_states,
        )
    if phase in {"preopen", "live"}:
        check("redis_reader", live is not None, "Redis只读连接可用，无内存降级", scope="live")
    if board_shadow:
        shadow = audit_shadow(root, plan, now, phase, limits.window_seconds)
        report["board_shadow_ready"] = shadow["ready"]
        report["board_shadow"] = shadow
    if phase == "preopen":
        report["notes"].append(
            "Preopen readiness excludes auction/opening snapshots and intraday evidence not yet due."
        )
        return finish()
    end = min(now, at(date, "15:00:00"))
    if phase == "break":
        end = at(date, "11:30:00")
    start = (
        at(date, "09:30:00")
        if phase == "postclose"
        else max(at(date, "09:30:00"), end - timedelta(seconds=limits.window_seconds))
    )
    if end >= at(date, "13:00:00") and phase != "postclose":
        start = max(start, at(date, "13:00:00"))
    report["archive_window"] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "scope": "full_day" if phase == "postclose" else "recent_window_only",
    }
    evidence_until = min(now, at(date, "15:05:00")) if phase == "postclose" else end
    report["archive_window"]["evidence_received_until"] = evidence_until.isoformat()
    snapshots, latest, minute_payloads, sector_points, auctions, invalid = (
        defaultdict(list),
        {},
        {},
        {},
        {},
        defaultdict(int),
    )
    try:
        with archive_reader(root, date) as (read, source_count):
            check("archive_sources", source_count > 0, "增量归档与持久接收日志可读", scope="replay")
            for event in read(("snapshot",), start, end, codes):
                try:
                    raw = json.loads(event["payload"])
                    code, received = event["code"], aware(event["received_at"])
                    if (
                        raw.get("code") != code
                        or raw.get("date") != date
                        or raw.get("source") != "eltdx_batch"
                        or aware(raw["received_at"]) != received
                        or raw.get("synthetic")
                        or not all(
                            positive(raw.get(field))
                            for field in ("last_price", "open_price", "pre_close", "amount_yuan", "vol_hand")
                        )
                        or raw.get("quality_ok") is not True
                    ):
                        raise ValueError("Invalid quote evidence")
                    source_time = at(date, raw["time"])
                    if not 0 <= (received - source_time).total_seconds() <= limits.quote_age:
                        raise ValueError("Snapshot source time stale or future")
                    prior = latest.get(code)
                    if prior and (
                        raw["time"] < prior["time"]
                        or float(raw["vol_hand"]) < float(prior["vol_hand"])
                        or float(raw["amount_yuan"]) < float(prior["amount_yuan"])
                    ):
                        raise ValueError("Snapshot source/cumulative counters regressed")
                    snapshots[code].append(received)
                    if code not in latest or received > aware(latest[code]["received_at"]):
                        latest[code] = raw
                except (ValueError, KeyError, TypeError):
                    invalid["snapshot"] += 1
            for event in read(("minutes",), at(date, "09:15:00"), evidence_until, codes):
                minute_payloads.setdefault(event["code"], []).append(event)
            for event in read(("auction",), at(date, "09:15:00"), evidence_until, codes, latest=True):
                raw = json.loads(event["payload"])
                if raw.get("code") == event["code"] and aware(raw["received_at"]) == aware(event["received_at"]):
                    auctions[event["code"]] = raw
            # Only visible publication records qualify. Legacy minute files have no visibility proof.
            for event in read(
                ("sector_minutes",), max(at(date, "09:30:00"), start - timedelta(minutes=4)), evidence_until
            ):
                try:
                    raw = json.loads(event["payload"])
                    published = aware(event["received_at"])
                    if (
                        raw.get("code") != event["code"]
                        or raw.get("date") != date
                        or raw.get("source") != "collector_closed_quote_minutes"
                        or aware(raw["received_at"]) != published
                    ):
                        raise ValueError("Invalid sector publication")
                    for point in raw.get("points") or []:
                        minute = point["time"]
                        minute_end, received = at(date, minute), aware(point["received_at"])
                        if (
                            point.get("code") != event["code"]
                            or point.get("source") != "eltdx_batch"
                            or not 0 <= (minute_end - received).total_seconds() < 60
                            or minute_end > published
                            or not positive(point.get("price"))
                        ):
                            raise ValueError("Invalid sector minute")
                        if (published - minute_end).total_seconds() > limits.sector_publish_lag:
                            invalid["late_sector_publication"] += 1
                        key = point["code"], minute
                        if key not in sector_points or received > aware(sector_points[key]["received_at"]):
                            sector_points[key] = point
                except (ValueError, TypeError, KeyError):
                    invalid["sector_minutes"] += 1
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        check("archive_read", False, f"归档读取失败: {type(exc).__name__}", scope="replay")
    check(
        "archive_invalid_rows", not invalid, "归档记录身份、日期、质量与时间校验", scope="replay", details=dict(invalid)
    )
    import pandas as pd

    from core.data.minute_quality import inspect_minute_bars

    for code in codes:
        sequence = continuity(snapshots[code], date, start, end, limits.snapshot_gap)
        check(f"snapshots:{code}", sequence["ok"], "归档快照存在且在检查窗口内连续", scope="replay", details=sequence)
        item = {"code": code, "snapshots": sequence}
        report["stocks"].append(item)
        raw = latest.get(code) or {}
        check(
            f"session_reference:{code}",
            bool(positive(raw.get("open_price")) and positive(raw.get("pre_close"))),
            "真实开盘价与昨收，不用首分钟替代",
            scope="replay",
        )
        try:
            versions = {}
            events = sorted(minute_payloads[code], key=lambda e: aware(e["received_at"]))
            for event in events:
                payload = json.loads(event["payload"])
                if payload.get("code") != code or aware(payload["received_at"]) != aware(event["received_at"]):
                    raise ValueError("Minute archive identity mismatch")
                for bar in payload["bars"]:
                    versions[str(bar.get("time") or bar.get("datetime"))] = bar
            event = events[-1]
            frame = pd.DataFrame(list(versions.values()))
            quality = inspect_minute_bars(
                frame, date, expected_code=code, require_full_day=phase == "postclose", now=end
            )
            item["minutes"] = {key: quality[key] for key in ("valid", "missing_count", "missing_times", "reason")}
            receipt_age = max(0.0, (end - aware(event["received_at"])).total_seconds())
            check(
                f"minutes:{code}",
                quality["valid"] and receipt_age <= limits.minute_lag,
                "分钟序列连续且原始接收时间未落后",
                scope="replay",
                details={**item["minutes"], "receipt_age_seconds": receipt_age},
            )
        except (ValueError, TypeError, KeyError):
            check(f"minutes:{code}", False, "缺少有效且带接收时间的分钟归档", scope="replay")
        from backtest.evidence_policy import requires_auction

        if requires_auction(RealtimeEntrySignalService._row_execution(rows.get(code) or {})):
            auction = auctions.get(code) or {}
            check(
                f"auction:{code}",
                bool(positive(auction.get("竞价成交额")) and positive(auction.get("竞价成交量"))),
                "策略必需的真实集合竞价证据",
                scope="replay",
            )
    # Assess availability, not bullishness: red/green market values are not readiness failures.
    minute = at(date, "09:31:00") if phase == "postclose" else end.replace(second=0, microsecond=0)
    missing_sector = defaultdict(list)
    while minute <= end:
        clock = minute.strftime("%H:%M:%S")
        if "11:30:00" < clock <= "13:00:00":
            minute += timedelta(minutes=1)
            continue
        earlier = (minute - timedelta(minutes=3)).strftime("%H:%M:%S")
        for code, expected in expected_members.items():
            current = {
                c
                for c in expected
                if (c, clock) in sector_points and positive(sector_points[c, clock].get("pre_close"))
            }
            paired = {
                c
                for c in current
                if (c, earlier) in sector_points
                and positive(sector_points[c, clock].get("open_price"))
                and sector_points[c, clock].get("open_price") == sector_points[c, earlier].get("open_price")
                and sector_points[c, clock].get("pre_close") == sector_points[c, earlier].get("pre_close")
            }
            denominator = len(expected)
            coverage = len(current) / denominator if denominator else 0
            need_trend = clock >= "09:34:00" and not "13:00:00" <= clock < "13:04:00"
            trend = len(paired) / denominator if denominator else 0
            if coverage < limits.sector_coverage or len(current) < 2 or need_trend and trend < limits.sector_coverage:
                missing_sector[code].append(
                    {
                        "minute": clock,
                        "coverage": round(coverage, 4),
                        "paired_coverage": round(trend, 4),
                        "denominator": denominator,
                    }
                )
        minute += timedelta(minutes=1)
    for code in (codes if breadth_required else []):
        gaps = missing_sector[code]
        check(
            f"sector:{code}",
            not gaps,
            "板块同组成分当前/三分钟前覆盖与连续性",
            scope="replay",
            details={"missing_minutes": len(gaps), "examples": gaps[:8]},
        )
    try:
        path = Path(ledger_path)
        if not path.exists():
            raise FileNotFoundError("Runtime ledger is absent")
        observations = defaultdict(list)
        completion_ages, durations = [], []
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
            for code, observed, payload in con.execute(
                "SELECT code,observed_at,payload_json FROM runtime_observations WHERE market_date=? AND profile='production' "
                "AND julianday(observed_at) BETWEEN julianday(?) AND julianday(?)",
                (date, start.isoformat(), end.isoformat()),
            ):
                stamp = aware(observed)
                if code not in codes or not start <= stamp <= end:
                    continue
                observations[code].append(stamp)
                timing = json.loads(payload).get("evaluation_timing") or {}
                if timing.get("quote_age_at_completion_seconds") is not None:
                    age = float(timing["quote_age_at_completion_seconds"])
                    if not math.isfinite(age) or age < 0:
                        raise ValueError("Invalid recorded quote age")
                    completion_ages.append(age)
                if timing.get("duration_ms") is not None:
                    duration = float(timing["duration_ms"])
                    if not math.isfinite(duration) or duration < 0:
                        raise ValueError("Invalid recorded evaluation duration")
                    durations.append(duration)
        for code in rows:
            sequence = continuity(observations[code], date, start, end, limits.observation_gap)
            check(
                f"runtime:{code}",
                sequence["ok"],
                "逐股真实运行观测留档（不要求出现买点）",
                scope="both" if phase == "live" else "replay",
                details=sequence,
            )
        check(
            "completion_quote_age",
            bool(completion_ages) and max(completion_ages) <= limits.quote_age,
            "逐股判定完成时报价年龄",
            scope="live" if phase == "live" else "replay",
            details={
                "max_seconds": max(completion_ages) if completion_ages else None,
                "max_evaluation_ms": max(durations) if durations else None,
            },
        )
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        check(
            "runtime_ledger",
            False,
            f"运行观测记录不可用: {type(exc).__name__}",
            scope="both" if phase == "live" else "replay",
        )
    if phase == "live" and live is not None:
        try:
            health_keys = ["realtime:collector:health", "realtime:archive:health", "realtime:strategy:health"]
            keys = (
                health_keys
                + [f"realtime:quotes:latest:{code}" for code in codes]
                + [f"realtime:minutes:{date}:{code}" for code in codes]
            )
            values = live.many(keys)
            for key, maximum in zip(
                health_keys, (limits.quote_age * 2, limits.archive_lag, limits.strategy_age), strict=True
            ):
                value = values.get(key) or {}
                age = (now - aware(value.get("updated_at"))).total_seconds() if value.get("updated_at") else None
                valid = value.get("ok") is True and age is not None and 0 <= age <= maximum
                if key.endswith("collector:health"):
                    valid = (
                        valid
                        and value.get("lease_owned") is True
                        and value.get("collection_plan_id") == plan["plan_id"]
                    )
                if key.endswith("strategy:health"):
                    valid = valid and value.get("plan_id") == plan["plan_id"] and value.get("market_date") == date
                check(key, valid, "工作进程心跳及冻结名单对齐", scope="live", details={"age_seconds": age})
            ticks = live.ticks(date, codes, start, now)
            from core.realtime.models import QuoteSnapshot

            for code in codes:
                raw = values.get(f"realtime:quotes:latest:{code}") or {}
                quote = QuoteSnapshot.from_raw(raw).with_freshness(stale_after_seconds=limits.quote_age, now=now)
                check(
                    f"hot_quote:{code}",
                    quote.code == code and quote.date == date and quote.last_price > 0 and not quote.is_stale,
                    "策略报价新鲜度与身份",
                    scope="live",
                    details={"age_seconds": quote.stale_seconds},
                )
                hot = ticks.get(code) or []
                valid_times = [
                    aware(row["received_at"])
                    for row in hot
                    if row.get("code") == code
                    and row.get("date") == date
                    and row.get("source") == "eltdx_batch"
                    and row.get("quality_ok") is True
                ]
                hot_sequence = continuity(valid_times, date, start, end, limits.snapshot_gap)
                check(
                    f"hot_ticks:{code}",
                    hot_sequence["ok"] and len(set(valid_times)) >= 2,
                    "策略输入的连续新快照",
                    scope="live",
                    details=hot_sequence,
                )
                hot_minutes = pd.DataFrame(values.get(f"realtime:minutes:{date}:{code}") or [])
                quality = inspect_minute_bars(hot_minutes, date, expected_code=code, require_full_day=False, now=now)
                check(
                    f"hot_minutes:{code}",
                    quality["valid"],
                    "策略实际读取的分钟数据完整性",
                    scope="live",
                    details={"reason": quality["reason"]},
                )
            minute = now.replace(second=0, microsecond=0)
            labels = [minute.strftime("%H:%M:%S"), (minute - timedelta(minutes=3)).strftime("%H:%M:%S")]
            all_members = sorted(set().union(*expected_members.values())) if expected_members else []
            points = live.sector_points(date, all_members, labels)
            for code, expected in expected_members.items():
                current, paired = set(), set()
                for member in expected:
                    current_point = points.get((member, labels[0])) or {}
                    prior_point = points.get((member, labels[1])) or {}
                    try:
                        valid = (
                            current_point.get("code") == member
                            and current_point.get("time") == labels[0]
                            and current_point.get("source") == "eltdx_batch"
                            and positive(current_point.get("price"))
                            and positive(current_point.get("pre_close"))
                            and 0 <= (minute - aware(current_point["received_at"])).total_seconds() < 60
                        )
                    except (ValueError, TypeError, KeyError):
                        valid = False
                    if valid:
                        current.add(member)
                        try:
                            paired_valid = (
                                prior_point.get("code") == member
                                and prior_point.get("time") == labels[1]
                                and prior_point.get("source") == "eltdx_batch"
                                and positive(prior_point.get("price"))
                                and positive(current_point.get("open_price"))
                                and current_point.get("open_price") == prior_point.get("open_price")
                                and current_point.get("pre_close") == prior_point.get("pre_close")
                                and 0
                                <= (minute - timedelta(minutes=3) - aware(prior_point["received_at"])).total_seconds()
                                < 60
                            )
                        except (ValueError, TypeError, KeyError):
                            paired_valid = False
                        if paired_valid:
                            paired.add(member)
                coverage = len(current) / len(expected) if expected else 0
                trend = len(paired) / len(expected) if expected else 0
                need_trend = labels[0] >= "09:34:00" and not "13:00:00" <= labels[0] < "13:04:00"
                check(
                    f"hot_sector:{code}",
                    len(current) >= 2
                    and coverage >= limits.sector_coverage
                    and (not need_trend or trend >= limits.sector_coverage),
                    "策略实际板块缓存的完整分母与三分钟前配对",
                    scope="live",
                    details={
                        "coverage": round(coverage, 4),
                        "paired_coverage": round(trend, 4),
                        "denominator": len(expected),
                    },
                )
            archive = values.get("realtime:archive:health") or {}
            oldest = archive.get("oldest_pending_at")
            lag = (now - aware(oldest)).total_seconds() if oldest else 0
            pending = archive.get("pending")
            valid = (
                pending == 0
                or isinstance(pending, int)
                and pending > 0
                and bool(oldest)
                and 0 <= lag <= limits.archive_lag
            )
            check(
                "archive_backlog",
                valid,
                "可靠增量归档积压年龄",
                scope="live",
                details={"pending": archive.get("pending"), "oldest_age_seconds": lag},
            )
            duration = (values.get("realtime:strategy:health") or {}).get("duration_ms")
            check(
                "strategy_cycle_cost",
                duration is not None and float(duration) <= limits.quote_age * 1000,
                "整轮耗时与报价新鲜度预算（不等同逐股耗时）",
                scope="live",
                warning=True,
                details={"duration_ms": duration},
            )
        except Exception as exc:
            check("redis_read", False, f"Redis只读检查失败: {type(exc).__name__}", scope="live")
    return finish()


def audit_shadow(root, parent, now, phase, window_seconds):
    """Optional diagnostic evidence must never become a formal THS gate."""
    from core.realtime.collection_plan import digest

    date = parent["market_date"]
    result = {"ready": False, "formal_gate": False, "boards": [], "reason": ""}
    try:
        plan = read_json(Path(root) / "collection_plans" / "tdx_boards" / f"{date}.json")
        body = {key: value for key, value in plan.items() if key != "plan_id"}
        if (
            body.get("market_date") != date
            or digest(body) != plan.get("plan_id")
            or body.get("collection_plan_id") != parent["plan_id"]
        ):
            raise ValueError("Invalid or mismatched TDX board plan")
        if not plan or not plan.get("codes"):
            raise ValueError("Missing frozen TDX board mapping")
        captured = aware(plan["captured_at"])
        if captured.strftime("%Y%m%d") != date or captured >= at(date, "09:25:00"):
            raise ValueError("Invalid TDX board preopen capture")
        if phase == "preopen":
            result.update(ready=True, reason="Frozen mapping only; snapshots not yet due")
            return result
        end = at(date, "11:30:00") if phase == "break" else min(now, at(date, "15:00:00"))
        start = (
            at(date, "09:30:00")
            if phase == "postclose"
            else max(at(date, "09:30:00"), end - timedelta(seconds=window_seconds))
        )
        observed, invalid = defaultdict(list), 0
        with archive_reader(root, date) as (read, _):
            for event in read(("board_snapshot",), start, end, plan["codes"]):
                try:
                    raw = json.loads(event["payload"])
                    received = aware(raw["received_at"])
                    if (
                        raw.get("code") != event["code"]
                        or raw.get("date") != date
                        or raw.get("board_plan_id") != plan["plan_id"]
                        or raw.get("synthetic")
                        or raw.get("source") != "eltdx_board_snapshot"
                        or not positive(raw.get("last_price"))
                        or received != aware(event["received_at"])
                    ):
                        raise ValueError("Invalid shadow snapshot")
                    observed[event["code"]].append(received)
                except (ValueError, TypeError, KeyError):
                    invalid += 1
        result["boards"] = [
            {"code": code, **continuity(observed[code], date, start, end, 20)} for code in plan["codes"]
        ]
        result.update(ready=invalid == 0 and all(row["ok"] for row in result["boards"]), invalid_rows=invalid)
        result["reason"] = "Optional TDX shadow snapshots; not formal THS confirmation"
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result["reason"] = str(exc)
    return result
