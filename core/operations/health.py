"""Fail-closed gates for post-close artifacts and live execution evidence."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config.settings import FACTOR_DB_PATH, MARKET_DATA_NODE_ROLE, SNAPSHOT_DIR, WEB_DATA_DIR
from core.etl.stage_status import factor_status, fetch_status
from core.factors.jobs.runner import FactorJobRunner


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def postclose_health(trade_date: str, *, db_path: Path | None = None,
                     web_data_dir: Path | None = None, snapshot_dir: Path | None = None,
                     as_of_market_date: str = "") -> dict[str, Any]:
    date = str(trade_date).replace("-", "")[:8]
    root = Path(web_data_dir or WEB_DATA_DIR)
    db = Path(db_path or FACTOR_DB_PATH)
    silver = fetch_status(date, db_path=db, web_data_dir=root)
    factors = factor_status(date, db_path=db)
    job_path = root / "factor_status" / f"factors_{date}.json"
    jobs = _read_json(job_path)
    results = jobs.get("jobs") or []
    expected = set(FactorJobRunner.JOBS)
    completed = FactorJobRunner.successful_job_keys(results)
    decision_path = root / "screening" / "decision_pool" / f"decision_pool_{date}.json"
    decision = _read_json(decision_path)
    snapshot_path = Path(snapshot_dir or SNAPSHOT_DIR) / f"{date}.json"
    snapshot = _read_json(snapshot_path)
    decision_ok = decision.get("trade_date") == date
    snapshot_ok = (snapshot.get("meta") or {}).get("date") == date
    reasons = []
    if not silver.get("ready") or silver.get("premature_fetch"):
        reasons.append("Silver 分区/取数质量未完整通过")
    if not factors.get("ready"):
        reasons.append("因子分区缺失")
    if completed != expected or jobs.get("trade_date") != date:
        reasons.append("因子任务逐项成功记录缺失或失败")
    if not decision_ok:
        reasons.append("决策池缺失或交易日不一致")
    if not snapshot_ok:
        reasons.append("页面快照缺失或交易日不一致")
    if as_of_market_date:
        market_date = str(as_of_market_date).replace("-", "")[:8]
        cutoff = f"{market_date[:4]}-{market_date[4:6]}-{market_date[6:]}T09:25:00"
        manifest = _read_json(root / "fetch_status" / f"fetch_{date}.json")
        timestamps = {
            "盘后取数": manifest.get("fetched_at"),
            "因子计算": jobs.get("generated_at"),
            "决策池": decision.get("generated_at"),
            "页面快照": (snapshot.get("meta") or {}).get("generated_at"),
        }
        for label, stamp in timestamps.items():
            value = str(stamp or "")
            if not value or value.replace(" ", "T") > cutoff:
                reasons.append(f"{label}不具备开盘前时点证据")
    return {
        "ok": not reasons, "trade_date": date, "reasons": reasons,
        "silver": silver, "factors": factors,
        "factor_jobs": {"path": str(job_path), "successful": sorted(completed), "missing": sorted(expected - completed)},
        "decision_pool": {"path": str(decision_path), "ready": decision_ok},
        "snapshot": {"path": str(snapshot_path), "ready": snapshot_ok},
        "as_of_market_date": as_of_market_date,
    }


def _age_seconds(value: Any, now: datetime) -> float | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return (now - stamp).total_seconds()
    except ValueError:
        return None


def intraday_health(row: dict[str, Any], market_date: str, *,
                    collector: dict[str, Any] | None = None,
                    now: datetime | None = None,
                    require_shared: bool | None = None,
                    quote_max_age: float = 12.0,
                    sector_max_age: float = 30.0,
                    min_sector_coverage: float = 0.6) -> dict[str, Any]:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    collector = collector or {}
    require_shared = MARKET_DATA_NODE_ROLE == "server" if require_shared is None else require_shared
    reasons = []
    if require_shared and collector.get("storage") != "redis":
        reasons.append("Redis 共享行情不可用")
    state = collector.get("collector") or {}
    collector_age = _age_seconds(state.get("updated_at"), now)
    if (collector_age is None or not 0 <= collector_age <= quote_max_age * 2
            or not state.get("ok") or not state.get("lease_owned")):
        reasons.append("采集器租约/心跳无有效证明")
    if str(collector.get("trade_date") or "").replace("-", "")[:8] != market_date:
        reasons.append("批量行情日期不符")
    quote_stamp = row.get("received_at") or row.get("quote_time")
    quote_age = _age_seconds(quote_stamp, now)
    if quote_age is None or not 0 <= quote_age <= quote_max_age or row.get("is_stale"):
        reasons.append("个股行情过期或缺少接收时间")
    sector = row.get("sector_detail") or {}
    sector_age = _age_seconds(sector.get("observed_at"), now)
    completeness = float(sector.get("data_completeness") or 0)
    member_count = int(sector.get("member_count") or 0)
    observed_members = int(sector.get("observed_members") or 0)
    coverage = observed_members / min(member_count, 80) if member_count > 0 else 0.0
    if (sector_age is None or not 0 <= sector_age <= sector_max_age
            or completeness < 1.0 or coverage < min_sector_coverage):
        reasons.append("板块观察过期或覆盖不足")
    return {
        "ok": not reasons, "reasons": reasons,
        "collector_age_seconds": collector_age, "quote_age_seconds": quote_age,
        "sector_age_seconds": sector_age, "sector_coverage": round(coverage, 4),
        "sector_data_completeness": completeness,
    }


def gate_realtime_payload(payload: dict[str, Any], *, collector: dict[str, Any],
                          now: datetime | None = None, require_shared: bool | None = None) -> dict[str, Any]:
    market_date = str(payload.get("market_date") or "").replace("-", "")[:8]
    for row in payload.get("rows") or []:
        if str(row.get("confirm_status") or row.get("status") or "") != "confirmed":
            continue
        health = intraday_health(row, market_date, collector=collector, now=now, require_shared=require_shared)
        row["health_gate"] = health
        if not health["ok"]:
            row["confirm_status"] = "observe"
            row["status"] = "observe"
            row["status_text"] = "数据待恢复"
            row["signal_status_text"] = "数据待恢复"
            row["health_gate_reason"] = "；".join(health["reasons"])
            row["reason"] = row["health_gate_reason"]
    if isinstance(payload.get("counts"), dict):
        for key in ("confirmed", "observe", "cancelled", "unfilled"):
            payload["counts"][key] = sum(
                str(row.get("confirm_status") or row.get("status") or "") == key
                for row in payload.get("rows") or []
            )
    return payload
