"""Read-only application projections: bounded viewer summaries and admin details."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from core.data.providers.eltdx_research import RESEARCH_KINDS
from core.realtime.market_evidence import timestamp
from core.realtime.models import normalize_stock_code
from core.realtime.research_store import ResearchStore
from core.realtime.trading_session import shanghai_now

LIVE_KINDS = ("auction_series", "opening_match", "trade_batch", "depth")


def valid_date(value):
    datetime.strptime(value, "%Y%m%d")
    if len(value) != 8:
        raise ValueError("Expected YYYYMMDD")
    return value


def valid_code(value):
    if not re.fullmatch(r"(?:\d{6}(?:\.(?:SH|SZ|BJ))?|(?:sh|sz|bj)\d{6})", value):
        raise ValueError("Expected security code")
    return normalize_stock_code(value)


class EvidenceViews:
    def __init__(self, root, backend):
        self.root, self.backend = Path(root), backend
        self.research = ResearchStore(root)

    def plan(self, date):
        from core.realtime.collection_plan import DailyCollectionPlan

        return DailyCollectionPlan(self.root, self.backend).load(date)

    def stock(self, date, code):
        valid_date(date)
        code = valid_code(code)
        from core.realtime.evidence_store import EvidenceStore

        now = shanghai_now()
        live = []
        depth_book = None
        plan = self.plan(date)
        in_scope = bool(plan and code in {row["code"] for row in plan["rows"]})
        for kind in LIVE_KINDS:
            raw = self.backend.get_json(EvidenceStore.key(date, code, kind))
            if (
                not raw
                or not in_scope
                or raw.get("collection_plan_id") != plan["plan_id"]
                or raw.get("capture_mode") != "live"
                or raw.get("code") != code
                or raw.get("market_date") != date
            ):
                live.append(dict(kind=kind, status="missing", summary=None, received_at=None, is_stale=None))
                continue
            age = (now - timestamp(raw["received_at"])).total_seconds()
            # No raw rows, cursors, error text or transport details in viewer response.
            summary = raw.get("summary")
            if kind == "opening_match" and raw.get("rows"):
                summary = {key: raw["rows"][0].get(key) for key in ("price", "amount_yuan", "volume_hand")}
            if kind == "depth" and raw.get("rows"):
                depth_book = raw["rows"][0]
                summary = {
                    key: depth_book.get(key)
                    for key in ("spread_yuan", "bid_depth_hand", "ask_depth_hand", "imbalance", "book_state")
                }
            live.append(
                dict(
                    kind=kind,
                    status=raw["quality_status"],
                    summary=summary,
                    coverage={
                        key: raw.get("coverage", {}).get(key)
                        for key in ("complete", "sequence_verified", "rows_observed")
                    },
                    received_at=raw["received_at"],
                    is_stale=not 0 <= age <= 15,
                    row_count=len(raw.get("rows", [])),
                )
            )
        quote = self.backend.get_json(f"realtime:quotes:latest:{code}") or {}
        existing_depth = next((item for item in live if item["kind"] == "depth" and item.get("received_at")), None)
        if (
            in_scope
            and quote.get("date") == date
            and quote.get("depth")
            and quote.get("received_at")
            and (existing_depth is None or timestamp(quote["received_at"]) >= timestamp(existing_depth["received_at"]))
        ):
            age = (now - timestamp(quote["received_at"])).total_seconds()
            book = depth_book = quote["depth"]
            live = [row for row in live if row["kind"] != "depth"] + [
                dict(
                    kind="depth",
                    status=book["quality_status"],
                    summary={
                        key: book.get(key)
                        for key in ("spread_yuan", "bid_depth_hand", "ask_depth_hand", "imbalance", "book_state")
                    },
                    received_at=quote["received_at"],
                    is_stale=not 0 <= age <= 15,
                    source="snapshot_embedded",
                )
            ]
        auction_points = []
        curve = self.backend.get_json(EvidenceStore.key(date, code, "auction_series")) or {}
        if (
            in_scope
            and curve.get("collection_plan_id") == plan["plan_id"]
            and curve.get("quality_status") == "ok"
            and curve.get("capture_mode") == "live"
            and curve.get("market_date") == date
        ):
            points = curve.get("rows", [])
            step = max(1, (len(points) + 119) // 120)
            auction_points = [
                {key: row.get(key) for key in ("event_time", "price", "matched_volume_hand")} for row in points[::step]
            ][:120]
        latest = []
        for kind in sorted(RESEARCH_KINDS):
            rows = self.research.read(code, kind, limit=1)
            if not rows:
                continue
            row = rows[0]
            latest.append(
                dict(
                    kind=kind,
                    version_id=row["version_id"],
                    captured_at=row["captured_at"],
                    status=row["quality_status"],
                    row_count=len(row["rows"]),
                    start=row["start"],
                    end=row["end"],
                    adjust=row["adjust"],
                    period=row["period"],
                    availability_basis=row["availability_basis"],
                    historical_eligible=False,
                    preview=[
                        {str(key): str(value)[:160] for key, value in list(item.get("values", {}).items())[:6]}
                        for item in row["rows"][:2]
                    ]
                    if kind in {"company_profile", "announcements", "finance_report", "topics"}
                    else None,
                )
            )
        return dict(
            ok=True,
            market_date=date,
            code=code,
            in_collection_scope=in_scope,
            live=live,
            research=latest,
            auction_points=auction_points,
            depth_levels={
                side: [
                    {key: level.get(key) for key in ("level", "price", "volume_hand")}
                    for level in (depth_book or {}).get(side, [])[:5]
                ]
                for side in ("buy_levels", "sell_levels")
            },
            formal_eligible=False,
            strategy_affected=False,
            remote_fetch_performed=False,
        )

    def research_rows(self, code, kind, *, offset=0, limit=50, as_of=None):
        code = valid_code(code)
        if kind not in RESEARCH_KINDS or offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid research page")
        values = self.research.read(code, kind, as_of=as_of)
        if not values:
            return dict(ok=True, total=0, rows=[], status="missing")
        value = values[0]
        return dict(
            ok=True,
            version_id=value["version_id"],
            captured_at=value["captured_at"],
            status=value["quality_status"],
            coverage=value["coverage"],
            total=len(value["rows"]),
            rows=value["rows"][offset : offset + limit],
            offset=offset,
            limit=limit,
            historical_eligible=False,
        )

    def raw(self, date, code, kind, *, offset=0, limit=50):
        from core.realtime.evidence_store import EvidenceStore

        valid_date(date)
        code = valid_code(code)
        if kind not in LIVE_KINDS or offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid raw evidence page")
        value = self.backend.get_json(EvidenceStore.key(date, code, kind)) or {}
        rows = value.get("rows", [])
        return dict(
            ok=True,
            total=len(rows),
            offset=offset,
            limit=limit,
            rows=rows[offset : offset + limit],
            metadata={
                key: value.get(key)
                for key in ("quality_status", "quality_reasons", "coverage", "received_at", "collection_plan_id")
            },
        )

    def board(self, date):
        from core.realtime.board_diagnostics import board_shadow_report
        from core.realtime.board_plan import BoardCollectionPlan
        from core.realtime.board_quote_store import BoardQuoteStore

        valid_date(date)
        parent = self.plan(date)
        plan = BoardCollectionPlan(self.root).load(date, parent_id=parent["plan_id"] if parent else None)
        # Store construction only binds Redis/root; it does not initialize a journal.
        return board_shadow_report(
            parent, plan, BoardQuoteStore(backend=self.backend, archive_root=self.root / "market_archive")
        )

    def diagnostics(self, date):
        valid_date(date)
        plan = self.plan(date)
        scope = [row["code"] for row in plan["rows"]] if plan else []
        statuses = []
        for code in scope:
            stock = self.stock(date, code)
            statuses.append(dict(code=code, live=stock["live"]))
        folder = self.root / "market_archive"
        pending = None
        path = folder / "ingestion.sqlite3"
        if path.exists():
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = con.execute("SELECT count(*),min(received_at) FROM market_events WHERE archived=0").fetchone()
                pending = dict(count=row[0], oldest_received_at=row[1])
            finally:
                con.close()
        from core.realtime.evidence_lane import EvidenceLane

        return dict(
            ok=True,
            market_date=date,
            scope_count=len(scope),
            stocks=statuses,
            pending=pending,
            collector=self.backend.get_json(EvidenceLane.HEALTH_KEY) or {},
            archive=self.backend.get_json("realtime:archive:health") or {},
            research_coverage=self.research.coverage(date=date),
            jobs=self.research.jobs(),
            remote_fetch_performed=False,
            formal_eligible=False,
        )

    def experiments(self):
        folder = self.root / "experiments" / "eltdx_evidence"
        rows = []
        if folder.exists():
            for path in sorted(folder.glob("*.json"), reverse=True)[:50]:
                if path.stat().st_size > 64 * 1024 * 1024:
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    rows.append(
                        dict(
                            report_id=path.stem,
                            day_count=value["day_count"],
                            complete_days=value["complete_days"],
                            A_count=sum(day["A"]["confirmed_count"] for day in value["days"]),
                            C_count=sum(day["C"]["confirmed_count"] for day in value["days"]),
                            unknown_count=sum(day["C"]["unknown_count"] for day in value["days"]),
                            profitability_measured=bool(value.get("profitability_measured")),
                        )
                    )
                except (ValueError, KeyError, TypeError):
                    rows.append(dict(report_id=path.stem, status="unreadable"))
        return dict(ok=True, rows=rows)
