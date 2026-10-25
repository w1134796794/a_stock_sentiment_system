"""Bounded reads of existing artifacts and shared state; no providers or SQL."""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import re
from datetime import datetime
from pathlib import Path

from core.realtime.trading_session import shanghai_now
from core.utils.stock_code_utils import StockCodeUtils

MAX_JSON = 16 * 1024 * 1024
MAX_MINUTES = 2 * 1024 * 1024
CANDIDATE_FIELDS = frozenset(
    {
        "code",
        "name",
        "score",
        "rank",
        "decision_label",
        "reasons",
        "排除理由",
        "exclusion_reasons",
        "行动分组",
        "命中策略",
        "策略共识数",
        "所属主线",
        "相关题材",
        "板块强度",
        "明日入场模式",
        "一句话结论",
        "次日确认条件",
        "失效条件",
        "执行仓位上限%",
        "规则等级",
        "resonance_sectors",
        "confidence",
        "trust_layers",
        "strategy_execution",
        "strategy_id",
        "strategy_name",
    }
)
SIGNAL_FIELDS = CANDIDATE_FIELDS | frozenset(
    {
        "status",
        "status_text",
        "confirm_status",
        "reason",
        "health_gate_reason",
        "received_at",
        "time",
        "date",
        "last_price",
        "change_pct",
        "entry_mode",
        "entry_price",
        "signal_at",
        "confirmed_at",
        "blocked_reasons",
        "confirmation_reasons",
        "condition_checks",
        "confirmation_reason",
        "confirmation_policy",
        "entry_parameters",
        "entry_veto_reason",
        "entry_mode_text",
        "entry_time",
        "confirm_time",
        "initial_confirm_time",
        "sector_detail",
        "sector_trend",
        "opening_evidence",
        "board_feedback",
        "strategy_version",
        "execution_strategy_id",
        "trigger_source",
        "quote_time",
        "quote_source_time",
        "quote_source",
        "quote_stale_seconds",
        "data_completeness",
        "confidence_grade",
        "signal_feature_snapshot",
        "evaluation_reused",
        "observation_source",
        "candidate_reasons",
        "structure",
        "structural_stop",
        "success_probability",
        "historical_samples",
        "historical_stats_status",
        "probability_calibration_status",
        "entry_evidence",
        "data_status",
        "is_stale",
    }
)
QUOTE_FIELDS = frozenset(
    {
        "code",
        "ts_code",
        "name",
        "open_price",
        "pre_close",
        "last_price",
        "high_price",
        "low_price",
        "bid1",
        "ask1",
        "vol_hand",
        "amount_yuan",
        "change_pct",
        "date",
        "time",
        "source",
        "received_at",
    }
)
MINUTE_FIELDS = frozenset(
    {
        "time",
        "datetime",
        "trade_date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "vol_hand",
        "amount_yuan",
    }
)
HEALTH_FIELDS = frozenset(
    {
        "ok",
        "updated_at",
        "heartbeat_at",
        "count",
        "cycles",
        "quote_count",
        "consecutive_failures",
        "lease_owned",
        "market_date",
        "trade_date",
        "storage",
    }
)


class ReadUnavailable(Exception):
    """An existing artifact is too large, malformed or currently inaccessible."""


def date_key(value):
    if not re.fullmatch(r"\d{8}", value):
        raise ValueError("Expected YYYYMMDD")
    datetime.strptime(value, "%Y%m%d")
    return value


def code_key(value):
    if not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", value):
        raise ValueError("Expected a six-digit stock code with optional exchange suffix")
    canonical = StockCodeUtils.standardize_code(value, add_suffix=True)
    if "." in value and canonical != StockCodeUtils.standardize_code(value[:6], add_suffix=True):
        raise ValueError("Stock exchange does not match code")
    return canonical


def codes_key(value):
    parts = value.split(",")
    if not 1 <= len(parts) <= 20:
        raise ValueError("Expected 1-20 stock codes")
    return list(dict.fromkeys(code_key(part.strip()).split(".")[0] for part in parts))


def select(row, fields):
    return {key: value for key, value in row.items() if key in fields}


class ReadonlyService:
    def __init__(self, *, web_data_dir=None, cache_dir=None, base_dir=None, backend=None, clock=shanghai_now):
        from config.settings import BASE_DIR, CACHE_DIR, REALTIME_QUOTE_STALE_SECONDS, WEB_DATA_DIR
        from core.infrastructure.shared_state import get_shared_state_backend

        self.root = Path(web_data_dir or WEB_DATA_DIR)
        self.cache = Path(cache_dir or CACHE_DIR)
        self.base = Path(base_dir or BASE_DIR)
        self.backend = backend if backend is not None else get_shared_state_backend()
        self.clock = clock
        self.stale_seconds = REALTIME_QUOTE_STALE_SECONDS

    def envelope(self, data):
        return {"ok": True, "read_only": True, "as_of": self.clock().isoformat(), "data": data}

    @staticmethod
    def _safe_file(path, root):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ReadUnavailable("Artifact is outside the allowed data directory")
        return path

    def _json(self, path):
        self._safe_file(path, self.root)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise ReadUnavailable("Artifact exceeds the bounded read limit")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ReadUnavailable("Invalid artifact")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ReadUnavailable("Artifact is temporarily unreadable") from exc

    def freshness(self, stamp, threshold=60):
        try:
            age = (self.clock() - shanghai_now(datetime.fromisoformat(stamp))).total_seconds()
        except (TypeError, ValueError):
            age = None
        return {
            "data_age_seconds": round(age, 3) if age is not None else None,
            "data_status": "missing_timestamp" if age is None else "fresh" if 0 <= age <= threshold else "stale",
        }

    def health(self):
        components = {}
        for name in ("collector", "archive", "strategy", "minute-sync"):
            value = self.backend.get_json(f"realtime:{name}:health") or {}
            components[name] = {
                **select(value, HEALTH_FIELDS),
                **self.freshness(value.get("updated_at") or value.get("heartbeat_at")),
            }
        return self.envelope(
            {
                "storage": "redis" if self.backend.is_shared else "memory",
                "shared_market_data": bool(self.backend.is_shared),
                "components": components,
            }
        )

    def dates(self):
        folder = self.root / "screening" / "decision_pool"
        self._safe_file(folder, self.root)
        dates = sorted(
            {
                match[1]
                for path in folder.glob("decision_pool_*.json")
                if (match := re.fullmatch(r"decision_pool_(\d{8})\.json", path.name))
            },
            reverse=True,
        )
        return self.envelope({"dates": dates[:30], "latest": dates[0] if dates else None})

    def candidates(self, date, *, code="", offset=0, limit=50):
        date_key(date)
        code6 = code_key(code).split(".")[0] if code else ""
        payload = self._json(self.root / "screening" / "decision_pool" / f"decision_pool_{date}.json")
        source = "decision_pool"
        if payload is None:
            payload = self._json(self.root / "screening" / f"screening_{date}.json")
            source = "screening"
        if payload is None:
            return self.envelope({"trade_date": date, "data_status": "missing", "rows": [], "total": 0})
        rows = (
            payload.get("rows") if source == "decision_pool" else payload.get("candidate_pool") or payload.get("final")
        )
        if not isinstance(rows, list):
            raise ReadUnavailable("Invalid candidate rows")
        rows = [
            row
            for row in rows
            if isinstance(row, dict) and (not code6 or str(row.get("code", "")).split(".")[0] == code6)
        ]
        return self.envelope(
            {
                "trade_date": date,
                "source": source,
                "generated_at": payload.get("generated_at"),
                "data_status": "stored",
                "total": len(rows),
                "offset": offset,
                "rows": [select(row, CANDIDATE_FIELDS) for row in rows[offset : offset + limit]],
            }
        )

    def quotes(self, codes):
        requested = codes_key(codes)
        keys = [f"realtime:quotes:latest:{code}" for code in requested]
        values = self.backend.get_many_json(keys)
        rows = []
        for code, key in zip(requested, keys, strict=True):
            value = values.get(key)
            if not isinstance(value, dict):
                continue
            from core.realtime.models import QuoteSnapshot

            item = QuoteSnapshot.from_raw(value).with_freshness(
                stale_after_seconds=self.stale_seconds, now=self.clock()
            )
            row = {**select(value, QUOTE_FIELDS), **self.freshness(value.get("received_at"), self.stale_seconds)}
            if not math.isfinite(item.last_price) or item.last_price <= 0:
                row["data_status"] = "invalid"
            elif item.is_stale:
                row["data_status"] = "stale"
            rows.append({**row, "code": code})
        return self.envelope(
            {
                "rows": rows,
                "missing_codes": [code for code in requested if code not in {row["code"] for row in rows}],
                "source": "shared_cache" if self.backend.is_shared else "process_cache",
            }
        )

    def signals(self, date, *, code="", offset=0, limit=50):
        date_key(date)
        code6 = code_key(code).split(".")[0] if code else ""
        payload = self.backend.get_json(f"realtime:strategy:result:{date}")
        if not payload:
            return self.envelope({"market_date": date, "data_status": "missing", "rows": [], "total": 0})
        rows = [
            row
            for row in payload.get("rows", [])
            if isinstance(row, dict) and (not code6 or str(row.get("code", "")).split(".")[0] == code6)
        ]
        fresh = self.freshness(payload.get("generated_at"), self.stale_seconds)
        selected = []
        for row in rows[offset : offset + limit]:
            item = select(row, SIGNAL_FIELDS)
            quote_fresh = self.freshness(row.get("received_at"), self.stale_seconds)
            if (
                fresh["data_status"] != "fresh" or quote_fresh["data_status"] != "fresh" or bool(row.get("is_stale"))
            ) and item.get("confirm_status") == "confirmed":
                item.update(
                    stored_confirm_status=item.get("confirm_status"),
                    stored_reason=item.get("reason"),
                    reason="策略结果或行情已过期，仅作为历史数据查看",
                    confirm_status="observe",
                    status="observe",
                    status_text="结果已过期",
                    health_gate_reason="策略结果或行情已过期",
                )
            selected.append({**item, "quote_freshness": quote_fresh})
        return self.envelope(
            {
                "market_date": date,
                "generated_at": payload.get("generated_at"),
                **fresh,
                "total": len(rows),
                "offset": offset,
                "rows": selected,
                "source": "stored_strategy_result",
                "candidate_date": payload.get("candidate_date"),
                "collection_plan_id": payload.get("collection_plan_id"),
                "execution_health": select(payload.get("execution_health") or {}, {"ready", "reasons"}),
            }
        )

    def minutes(self, code, date, *, offset=0, limit=500):
        date_key(date)
        canonical = code_key(code)
        code6 = canonical.split(".")[0]
        rows = self.backend.get_json(f"realtime:minutes:{date}:{code6}")
        source = "shared_cache" if self.backend.is_shared else "process_cache"
        modified = None
        if not isinstance(rows, list) or not rows:
            rows = None
            for name in (f"{canonical}_{date}.csv", f"{code6}_{date}.csv"):
                plain = self.cache / "stock" / "tick" / name
                for path in (plain, plain.with_suffix(".csv.gz")):
                    self._safe_file(path, self.cache)
                    if not path.exists():
                        continue
                    opener = gzip.open if path.suffix == ".gz" else open
                    with opener(path, "rb") as stream:
                        raw = stream.read(MAX_MINUTES + 1)
                    if len(raw) > MAX_MINUTES:
                        raise ReadUnavailable("Minute cache exceeds the bounded read limit")
                    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
                    rows = []
                    for row in reader:
                        rows.append(row)
                        if len(rows) > 2000:
                            raise ReadUnavailable("Too many minute rows")
                    modified = datetime.fromtimestamp(path.stat().st_mtime, self.clock().tzinfo).isoformat()
                    source = "minute_cache"
                    break
                if rows is not None:
                    break
        rows = rows or []
        return self.envelope(
            {
                "code": canonical,
                "trade_date": date,
                "source": source,
                "data_status": "stored" if rows else "missing",
                "file_modified_at": modified,
                "last_bar_time": (rows[-1].get("time") or rows[-1].get("datetime")) if rows else None,
                "total": len(rows),
                "offset": offset,
                "rows": [select(row, MINUTE_FIELDS) for row in rows[offset : offset + limit] if isinstance(row, dict)],
            }
        )

    def log_summary(self, date, kind):
        date_key(date)
        if kind == "preopen":
            root = self.root
            path = root / "logs" / f"preopen_collection_{date}.log"
        elif kind == "postclose":
            root = self.base
            path = root / "logs" / f"automation_postclose_{date}.log"
        else:
            raise ValueError("Unsupported log kind")
        self._safe_file(path, root)
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                text = stream.read(65536).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return self.envelope({"date": date, "kind": kind, "data_status": "missing"})
        return self.envelope(
            {
                "date": date,
                "kind": kind,
                "data_status": "stored",
                "tail_bytes": len(text.encode()),
                "error_lines": sum(
                    bool(re.search(r"\b(ERROR|CRITICAL|Traceback)\b", line)) for line in text.splitlines()
                ),
                "warning_lines": sum(bool(re.search(r"\b(WARNING|WARN)\b", line)) for line in text.splitlines()),
                "raw_log_included": False,
            }
        )
