"""Immutable inputs known before a market session, separate from research reruns."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.infrastructure.file_mutex import file_mutex

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=SHANGHAI) if result.tzinfo is None else result.astimezone(SHANGHAI)


def cutoff(market_date: str):
    return datetime.strptime(market_date + "092500", "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PreopenEvidenceStore:
    def __init__(self, root: Path | str | None = None):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or WEB_DATA_DIR)

    def path(self, candidate_date: str, market_date: str):
        if not (len(candidate_date) == len(market_date) == 8 and candidate_date.isdigit() and market_date.isdigit()):
            raise ValueError("Expected YYYYMMDD evidence dates")
        return self.root / "preopen_evidence" / market_date / f"{candidate_date}.json"

    def load(self, candidate_date: str, market_date: str):
        path = self.path(candidate_date, market_date)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        bundle_id = payload.pop("bundle_id", "")
        if (not bundle_id or _digest(payload) != bundle_id
                or payload.get("candidate_date") != candidate_date
                or payload.get("market_date") != market_date
                or _stamp(payload["frozen_at"]) >= cutoff(market_date)
                or not (payload.get("health") or {}).get("ok")):
            raise ValueError("Invalid preopen evidence bundle")
        payload["bundle_id"] = bundle_id
        return payload

    def freeze(self, candidate_date: str, market_date: str, *, inputs: dict, health: dict,
               now: datetime | None = None):
        current = _stamp(now or datetime.now(SHANGHAI))
        path = self.path(candidate_date, market_date)
        with file_mutex(str(path) + ".lock"):
            existing = self.load(candidate_date, market_date)
            if existing:
                return {"ok": True, "existing": True, "bundle_id": existing["bundle_id"]}
            if current >= cutoff(market_date):
                return {"ok": False, "reason": "past_preopen_cutoff"}
            if not health.get("ok"):
                return {"ok": False, "reason": "postclose_health_failed", "reasons": health.get("reasons") or []}
            payload = {
                "schema_version": 1, "candidate_date": candidate_date, "market_date": market_date,
                "frozen_at": current.isoformat(timespec="microseconds"),
                "health": deepcopy(health), "inputs": deepcopy(inputs),
            }
            payload["bundle_id"] = _digest(payload)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"ok": True, "existing": False, "bundle_id": payload["bundle_id"]}


class FrozenLeaderPool:
    def __init__(self, bundle):
        self.bundle = bundle

    def build_pool(self, trade_date=None, *, lookback=10, limit=30):
        pools = self.bundle["inputs"]["leader_pools"]
        pool = deepcopy(pools.get(str(lookback)) or pools["10"])
        pool["rows"] = (pool.get("rows") or [])[:max(int(limit), 1)]
        return pool


def freeze_preopen_inputs(candidate_date: str, market_date: str, *, web_data_dir=None,
                          db_path=None, snapshot_dir=None, now=None):
    from config.settings import FACTOR_DB_PATH, SNAPSHOT_DIR, WEB_DATA_DIR
    from core.infrastructure.duckdb_access import connect
    from core.operations.health import postclose_health
    from core.realtime.leader_pool_service import LeaderPoolService
    from core.realtime.overlay_service import RealtimeOverlayService
    from core.realtime.sector_service import get_realtime_sector_service
    from core.signals.sector_confirmation import sector_names_from_row

    root = Path(web_data_dir or WEB_DATA_DIR)
    db = Path(db_path or FACTOR_DB_PATH)
    snapshots = Path(snapshot_dir or SNAPSHOT_DIR)
    store = PreopenEvidenceStore(root)
    current = _stamp(now or datetime.now(SHANGHAI))
    existing = store.load(candidate_date, market_date)
    if existing:
        return {"ok": True, "existing": True, "bundle_id": existing["bundle_id"]}
    if current >= cutoff(market_date):
        return {"ok": False, "reason": "past_preopen_cutoff"}
    # Hold the same mutex as every database reader/writer across the capture.
    with file_mutex(str(db.resolve()) + ".access.lock"):
        from core.screening.strategy_profiles import StrategyProfileRepository
        from risk.risk_config import RiskConfig
        from dataclasses import asdict
        from config.settings import BASE_DIR

        profiles = StrategyProfileRepository()
        artifact_paths = {
            "fetch": root / "fetch_status" / f"fetch_{candidate_date}.json",
            "factors": root / "factor_status" / f"factors_{candidate_date}.json",
            "decision_pool": root / "screening" / "decision_pool" / f"decision_pool_{candidate_date}.json",
            "snapshot": snapshots / f"{candidate_date}.json",
        }
        def source_fingerprint():
            paths = set((root / "screening").rglob("*.json")) | set(artifact_paths.values())
            paths.update((profiles.path, profiles.base_profile_path, BASE_DIR / "config" / "risk_control.yaml",
                          root / "config_overrides.json"))
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in paths if path.is_file()}
        before = source_fingerprint()
        health = postclose_health(candidate_date, db_path=db, web_data_dir=root,
                                  snapshot_dir=snapshots, as_of_market_date=market_date, use_frozen=False)
        if not health["ok"]:
            return {"ok": False, "reason": "postclose_health_failed", "reasons": health["reasons"]}
        captured_profiles = profiles.list_profiles()
        captured_risk = asdict(RiskConfig.load())
        overlay = RealtimeOverlayService(screening_dir=root / "screening")
        candidates = overlay.candidate_rows(candidate_date, limit=10000)
        candidates_by_profile = {
            item["id"]: overlay.candidate_rows(candidate_date, profile=item["id"], limit=10000)
            for item in captured_profiles if item.get("id")
        }
        leader = LeaderPoolService(screening_dir=root / "screening", duckdb_path=db)
        pools = {str(days): leader.build_pool(candidate_date, lookback=days, limit=10000)
                 for days in (5, 10, 20)}
        from core.portfolio.holding_repository import HoldingRepository
        holdings = HoldingRepository().list_positions(status="open")
        rows = [*candidates, *(row for values in candidates_by_profile.values() for row in values),
                *(pools["20"].get("rows") or []), *holdings]
        sectors = sorted(set().union(*(sector_names_from_row(row) for row in rows))) if rows else []
        sector_service = get_realtime_sector_service()
        mapping = sector_service.resolve_codes_by_names(sectors, source="ths")
        members = {name: sector_service.member_codes(code) for name, code in mapping.items()}
        codes = sorted({str(row.get("code") or "").split(".")[0] for row in rows})
        with connect(db, read_only=True) as con:
            daily = con.execute("SELECT * FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR)=?", [candidate_date]).fetchdf()
            factors = con.execute("SELECT * FROM factor_stock_wide WHERE CAST(trade_date AS VARCHAR)=?", [candidate_date]).fetchdf()
        daily = daily[daily["code"].astype(str).str.split(".").str[0].isin(codes)]
        factors = factors[factors["code"].astype(str).str.split(".").str[0].isin(codes)]
        def records(frame):
            return json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        artifacts = {}
        for label, path in artifact_paths.items():
            artifacts[label] = json.loads(path.read_text(encoding="utf-8"))
        if (before != source_fingerprint() or captured_profiles != profiles.list_profiles()
                or captured_risk != asdict(RiskConfig.load())):
            return {"ok": False, "reason": "inputs_changed_during_capture"}
        inputs = {"candidates": candidates, "candidates_by_profile": candidates_by_profile,
                  "leader_pools": pools, "holdings": holdings, "daily_rows": records(daily),
                  "factor_rows": records(factors), "sector_members": members, "sector_codes": mapping,
                  "artifacts": artifacts, "profiles": captured_profiles,
                  "risk_config": captured_risk, "source_fingerprint": before}
        # Use the actual end-of-capture time, not the earlier validation time.
        return store.freeze(candidate_date, market_date, inputs=inputs, health=health,
                            now=(now if now is not None else datetime.now(SHANGHAI)))
