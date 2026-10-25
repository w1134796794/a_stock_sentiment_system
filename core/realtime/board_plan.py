"""Preopen TDX/THS comparisons; verified proxies may supply optional trend scores."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from core.infrastructure.atomic_json import atomic_read_json, atomic_write_json
from core.infrastructure.file_mutex import file_mutex
from core.realtime.collection_plan import digest
from core.realtime.trading_session import shanghai_now


class BoardCollectionPlan:
    def __init__(self, root=None):
        from config.settings import WEB_DATA_DIR
        self.root = Path(root or WEB_DATA_DIR) / "collection_plans" / "tdx_boards"

    def path(self, date):
        datetime.strptime(date, "%Y%m%d")
        return self.root / f"{date}.json"

    def load(self, date, *, parent_id=None):
        path = self.path(date)
        if not path.exists():
            return None
        value = atomic_read_json(path)
        body = {key: item for key, item in value.items() if key != "plan_id"}
        if (body.get("market_date") != date or digest(body) != value.get("plan_id")
                or (parent_id is not None and body.get("collection_plan_id") != parent_id)):
            raise ValueError("Invalid or mismatched TDX board plan")
        return value

    def prepare(self, parent, provider, *, hints=None, now=None, clock=None):
        read_clock = clock or (lambda: shanghai_now(now))
        date = parent["market_date"]
        hints = dict(hints or {})
        if any(not isinstance(name, str) or not isinstance(code, str)
               or not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code) for name, code in hints.items()):
            raise ValueError("Mapping hints require sector names and explicit market-prefixed TDX codes")

        def preopen():
            stamp = shanghai_now(read_clock())
            if stamp.strftime("%Y%m%d") != date or stamp.strftime("%H:%M:%S") >= "09:25:00":
                raise ValueError("TDX board metadata must be prepared today before 09:25")
            return stamp

        preopen()
        path = self.path(date)
        with file_mutex(str(path) + ".lock"):
            existing = self.load(date, parent_id=parent["plan_id"])
            if existing:
                if existing.get("mapping_hints") != hints:
                    raise ValueError("Board mappings already frozen for this day")
                return existing
            names = {sector["name"] for sector in parent["sectors"]}
            if hints.keys() - names:
                raise ValueError("Mapping hints contain sectors outside the daily collection plan")
            catalogue = provider.prepare_board_catalogue()
            if catalogue["prepared_date"] != date:
                raise ValueError("SDK board preparation date does not match the collection day")
            definitions = {row["code"]: row for row in catalogue["rows"]}
            if any(not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code) for code in definitions):
                raise ValueError("Board catalogue has invalid identities")
            members, rows, selected = {}, [], set()
            for sector in parent["sectors"]:
                name = sector["name"]
                candidates = ([hints[name]] if name in hints else
                              [code for code, row in definitions.items() if row["name"] == name])
                comparisons = []
                for code in candidates:
                    preopen()
                    if code not in definitions:
                        raise ValueError(f"Unknown TDX mapping: {code}")
                    if code not in members:
                        members[code] = provider.prepare_board_members(code)
                    detail = members[code]
                    if detail["prepared_date"] != date:
                        raise ValueError("Board membership date mismatch")
                    ths, tdx = set(sector["members"]), set(detail["members"])
                    common = ths & tdx
                    comparisons.append({"code": code, "name": definitions[code]["name"],
                        "ths_count": len(ths), "tdx_count": len(tdx), "intersection_count": len(common),
                        "jaccard": len(common) / len(ths | tdx) if ths | tdx else 0,
                        "ths_only": sorted(ths - tdx), "tdx_only": sorted(tdx - ths),
                        "ths_members": sorted(ths), "tdx_members": sorted(tdx),
                        "membership_hash": digest({"ths": sorted(ths), "tdx": sorted(tdx)}),
                        "tdx_excluded_members": detail["excluded_members"]})
                    selected.add(code)
                ranked = sorted(comparisons, key=lambda item: item["jaccard"], reverse=True)
                verified = bool(ranked and ranked[0]["jaccard"] >= 0.8
                                and (len(ranked) == 1 or ranked[0]["jaccard"] > ranked[1]["jaccard"]))
                rows.append({"ths_name": name, "ths_code": sector["code"],
                             "score_eligible": verified,
                             "score_code": ranked[0]["code"] if verified else "",
                             "score_mapping_basis": "preopen_membership_overlap_ge_0.8" if verified else "unverified",
                             "status": "unmapped" if not comparisons else "requires_review",
                             "candidate_basis": "explicit_hint" if name in hints else "exact_name_candidate",
                             "candidates": comparisons, "formal_eligible": False})
            body = {"schema_version": 1, "market_date": date, "collection_plan_id": parent["plan_id"],
                    "captured_at": preopen().isoformat(), "mapping_hints": hints,
                    "scoring_version": "mapped_board_trend_score_v1" if parent.get("sector_confirmation_mode") == "trend_score" else "",
                    "catalogue_hash": digest(catalogue), "sdk_prepared_date": catalogue["prepared_date"],
                    "codes": sorted({r["score_code"] for r in rows if r.get("score_eligible")})
                             if parent.get("sector_confirmation_mode") == "trend_score" else sorted(selected),
                    "rows": rows, "formal_eligible": False,
                    "warnings": ["TDX and THS definitions are not interchangeable; name matches are candidates only",
                                 "SDK prepared_date does not prove all resource files are fresh"]}
            value = {**body, "plan_id": digest(body)}
            atomic_write_json(path, value, indent=2)
            return value
