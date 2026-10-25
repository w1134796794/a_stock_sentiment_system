"""Immutable, dated collection universe prepared before the opening auction."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from core.infrastructure.atomic_json import atomic_read_json, atomic_write_json
from core.infrastructure.file_mutex import file_mutex
from core.infrastructure.shared_state import get_shared_state_backend
from core.realtime.models import normalize_stock_code
from core.realtime.trading_session import shanghai_now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def collection_sector_names(row):
    from core.signals.sector_confirmation import sector_names_from_row
    names = sector_names_from_row(row)
    for field in ("industry_tags", "concept_tags"):
        for tag in row.get(field) or []:
            name = tag.get("name") if isinstance(tag, dict) else tag
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


class DailyCollectionPlan:
    def __init__(self, root=None, backend=None):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or WEB_DATA_DIR) / "collection_plans"
        self.backend = backend or get_shared_state_backend()

    @staticmethod
    def key(date):
        if len(str(date)) != 8 or not str(date).isdigit():
            raise ValueError("Expected YYYYMMDD")
        return f"realtime:collection-plan:{date}"

    def load(self, date):
        key = self.key(date)
        value = self.backend.get_json(key)
        path = self.root / f"{date}.json"
        if value is None and path.exists():
            value = atomic_read_json(path)
        if value:
            body = {k: v for k, v in value.items() if k != "plan_id"}
            if body.get("market_date") != date or digest(body) != value.get("plan_id"):
                raise ValueError("Invalid collection plan")
        return value

    def bundle(self, date):
        plan = self.load(date)
        if not plan:
            return None
        bundle = self.backend.get_json(f"realtime:collection-inputs:{date}")
        if bundle is None:
            path = self.root / f"{date}.inputs.json"
            bundle = atomic_read_json(path) if path.exists() else None
        if bundle is not None and digest(bundle) != plan["inputs_hash"]:
            raise ValueError("Collection input hash mismatch")
        return bundle

    def freeze(self, bundle, *, now=None):
        clock = shanghai_now(now)
        date, candidate = bundle["market_date"], bundle["candidate_date"]
        self.key(date)
        path = self.root / f"{date}.json"
        with file_mutex(str(path) + ".prepare.lock"):
            existing = self.load(date)
            if existing:
                if existing["inputs_hash"] != digest(bundle):
                    raise ValueError("Daily collection inputs already frozen; no intraday replacement")
                self._publish(existing, bundle)
                return existing
            cutoff = shanghai_now(datetime.strptime(date + "09:25:00", "%Y%m%d%H:%M:%S"))
            if clock >= cutoff or not (bundle.get("health") or {}).get("ok"):
                raise ValueError("Collection plan requires healthy preopen evidence before 09:25")
            inputs = bundle["inputs"]
            from risk.risk_config import RiskConfig
            sector_mode = RiskConfig.from_dict(inputs.get("risk_config") or {}).entry_sector_mode
            if inputs.get("listing_evidence") and inputs["listing_evidence"].get("market_date") != date:
                raise ValueError("Listing evidence must match collection market date")
            from core.realtime.listing_evidence import listing_exclusions
            excluded = listing_exclusions(inputs)
            listing = inputs.get("listing_evidence") or {}
            leaders = ((inputs.get("leader_pools") or {}).get("10", {}).get("rows") or [])[:30]
            decisions = inputs.get("candidates") or []
            stocks = {}
            for source, rows in (("leader_pool", leaders), ("decision_pool", decisions)):
                for row in rows:
                    code = normalize_stock_code(row.get("code") or row.get("stock_code"), add_suffix=False)
                    if len(code) != 6 or not code.isdigit():
                        continue
                    if code in excluded:
                        continue
                    item = stocks.setdefault(code, {"code": code, "name": row.get("name") or "",
                                                     "sources": [], "sectors": []})
                    if source not in item["sources"]:
                        item["sources"].append(source)
                    item["sectors"] = sorted(set(item["sectors"]) | collection_sector_names(row))
            if sector_mode == "breadth_required" and any(not row["sectors"] for row in stocks.values()):
                raise ValueError("Stock sector/concept metadata missing before open")
            names = sorted({name for row in stocks.values() for name in row["sectors"]})
            requested = {normalize_stock_code(r.get("code") or r.get("stock_code"), add_suffix=False)
                         for r in [*leaders, *decisions]}
            requested.update(normalize_stock_code(c, add_suffix=False) for name in names
                             for c in (inputs.get("sector_members") or {}).get(name, []))
            from core.realtime.sector_service import RealtimeSectorService
            eligible_members = RealtimeSectorService.confirmation_members(inputs)
            sectors = [{"name": name, "code": (inputs.get("sector_codes") or {}).get(name) or "",
                        "members": sorted(eligible_members.get(name) or [])} for name in names]
            unresolved = [r["name"] for r in sectors if not r["code"] or not r["members"]]
            if unresolved and sector_mode == "breadth_required":
                raise ValueError("Preopen sector identities/members missing: " + ",".join(unresolved))
            holding_codes = {normalize_stock_code(r.get("code"), add_suffix=False) for r in inputs.get("holdings") or []}
            body = {"schema_version": 1, "market_date": date, "candidate_date": candidate,
                    "sector_confirmation_mode": sector_mode,
                    "frozen_at": clock.isoformat(), "bundle_id": bundle["bundle_id"],
                    "inputs_hash": digest(bundle), "rows": list(stocks.values()), "sectors": sectors,
                    "listing_exclusions": [value for code, value in excluded.items() if code in requested],
                    "listing_evidence_complete": bool(listing.get("complete")),
                    "stock_count": len(stocks),
                    "leader_count": sum("leader_pool" in r["sources"] for r in stocks.values()),
                    "decision_count": sum("decision_pool" in r["sources"] for r in stocks.values()),
                    "sector_member_count": len({c for s in sectors for c in s["members"]}),
                    "uncollected_holdings": sorted(holding_codes - stocks.keys()),
                    "warnings": [] if (inputs.get("minute_amount_profile") or {}).get("profiles") else ["历史分钟量能模型缺失，相应买点将保持观察"],
                    "hot_window_seconds": 180}
            if not listing.get("complete"):
                body["warnings"].append("上市状态证据不完整，未知状态成员保留；禁止按行情缺失推断退市")
            plan = {**body, "plan_id": digest(body)}
            atomic_write_json(self.root / f"{date}.inputs.json", bundle)
            atomic_write_json(path, plan, indent=2)
            self._publish(plan, bundle)
            return plan

    def _publish(self, plan, bundle):
        date = plan["market_date"]
        # Publish inputs before the manifest that points to them.
        self.backend.set_json(f"realtime:collection-inputs:{date}", bundle, 3 * 86400)
        self.backend.set_json(self.key(date), plan, 3 * 86400)
