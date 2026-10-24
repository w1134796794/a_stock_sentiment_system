"""Read-only evidence diagnostics; backfills cannot become live rule evidence."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from core.realtime.market_evidence import EVIDENCE_KINDS, auction_summary, fingerprint, timestamp, trade_summary


def load_evidence_events(root, date, *, research_backfill=False):
    from datetime import datetime

    datetime.strptime(date, "%Y%m%d")
    folder = Path(root) / "market_archive"
    if research_backfill:
        folder /= "backfill"
    found, conflicts, failures = {}, set(), []
    # Spool first: archiver commits archive before removing spool entries.
    for path in (folder / "ingestion.sqlite3", folder / f"market_{date}.sqlite3"):
        if not path.exists():
            continue
        try:
            con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                con.row_factory = sqlite3.Row
                sql = "SELECT event_id,kind,code,received_at,payload FROM market_events"
                args = ()
                if path.name == "ingestion.sqlite3":
                    sql += " WHERE market_date=? AND archived=0"
                    args = (date,)
                for raw in con.execute(sql, args):
                    if raw["kind"] not in EVIDENCE_KINDS:
                        continue
                    row = dict(raw)
                    row["payload"] = json.loads(row["payload"])
                    if not isinstance(row["payload"], dict):
                        raise ValueError("Invalid evidence payload")
                    key = row["event_id"]
                    if key in found and found[key] != row:
                        conflicts.add(key)
                    found[key] = row
            finally:
                con.close()
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            failures.append(dict(evidence="unreadable_evidence_archive", file=path.name, reason=str(exc)))
    if conflicts:
        failures.append(dict(evidence="conflicting_evidence_ids", count=len(conflicts)))
    return [row for key, row in found.items() if key not in conflicts], failures


def replay_evidence(events, *, date, until, plan_id="", codes=None, research_backfill=False, embedded_snapshots=None):
    end = timestamp(until)
    if end.strftime("%Y%m%d") != date:
        raise ValueError("Cutoff date mismatch")
    allowed = set(codes) if codes is not None else None
    publications, observations, invalid = {}, [], []
    for event in events:
        try:
            value = event["payload"]
            receipt = timestamp(event["received_at"])
            if event["code"] != value["code"] or timestamp(value["received_at"]) != receipt:
                raise ValueError("Evidence identity/receipt mismatch")
            if value.get("market_date") != date or (allowed is not None and value["code"] not in allowed):
                continue
            if value.get("capture_mode") != ("backfill" if research_backfill else "live"):
                continue
            if not research_backfill and (not plan_id or value.get("collection_plan_id") != plan_id):
                continue
            if not research_backfill and (receipt.strftime("%Y%m%d") != date or receipt > end):
                continue
            if event["kind"] == "evidence_publication":
                key = value["observation_id"]
                publications[key] = min(publications.get(key, receipt), receipt)
            else:
                if value.get("kind") != event["kind"] or value.get("schema_version") != 1:
                    raise ValueError("Unknown evidence schema/kind")
                original = {key: item for key, item in value.items() if key != "observation_id"}
                if fingerprint(original) != value.get("observation_id"):
                    raise ValueError("Evidence observation hash mismatch")
                observations.append((receipt, value))
        except (ValueError, TypeError, KeyError) as exc:
            invalid.append(dict(evidence="invalid_supplemental_event", reason=str(exc)))
    latest, counts, trades, conflicts, trade_codes = {}, Counter(), {}, set(), {}
    unpublished = 0
    for receipt, value in sorted(observations, key=lambda item: item[0]):
        publication = publications.get(value["observation_id"])
        if not research_backfill and (publication is None or publication < receipt):
            unpublished += 1
            continue
        visible = publication or receipt
        rows = value["rows"]
        if research_backfill:
            # Research may use later downloads, but only source times <= the cutoff.
            rows = [row for row in rows if row.get("event_time") and timestamp(row["event_time"]) <= end]
        kind, code = value["kind"], value["code"]
        counts[kind] += 1
        latest[(code, kind)] = {
            **value,
            "rows": rows,
            "summary": (
                trade_summary(rows)
                if kind == "trade_batch" and value.get("coverage", {}).get("sequence_verified")
                else auction_summary(rows)
                if kind == "auction_series" and value["quality_status"] == "ok"
                else {}
            ),
            "is_stale": (end - receipt).total_seconds() > 15 if kind in {"trade_batch", "depth"} else False,
            "published_at": publication.isoformat() if publication else None,
            "available_at": visible.isoformat(),
            "research_only": research_backfill,
        }
        if kind == "trade_batch" and value.get("coverage", {}).get("sequence_verified"):
            for row in rows:
                if row.get("event_kind") != "trade":
                    continue
                key = row["trade_id"]
                if key in trades and trades[key] != row:
                    conflicts.add(key)
                trades[key] = row
                trade_codes[key] = code
    for key in conflicts:
        trades.pop(key, None)
    if conflicts:
        invalid.append(dict(evidence="conflicting_trade_ids", count=len(conflicts)))
    embedded = {}
    if not research_backfill:
        for event in embedded_snapshots or []:
            try:
                raw = event["payload"]
                code = event["code"]
                receipt = timestamp(event["received_at"])
                if (
                    event["kind"] != "snapshot"
                    or "depth" not in raw
                    or code != raw.get("code")
                    or raw.get("date") != date
                    or receipt > end
                    or timestamp(raw["received_at"]) != receipt
                    or (allowed is not None and code not in allowed)
                ):
                    continue
                if code not in embedded or timestamp(embedded[code]["received_at"]) < receipt:
                    embedded[code] = dict(
                        code=code,
                        depth=raw["depth"],
                        received_at=receipt.isoformat(),
                        is_stale=(end - receipt).total_seconds() > 15,
                        visibility_basis="snapshot_receipt_only",
                        publication_verified=False,
                        formal_eligible=False,
                    )
            except (KeyError, ValueError, TypeError):
                invalid.append(dict(evidence="invalid_embedded_depth"))
    actual = list(trades.values())
    per_code = {}
    # Trade identities are stock-specific; derive per-code summaries from visible,
    # verified batches instead of interpreting a missing batch as zero trading.
    for (code, kind), value in latest.items():
        if kind == "trade_batch":
            window = [
                row
                for key, row in trades.items()
                if trade_codes[key] == code
                and row.get("event_time")
                and 0 <= (end - timestamp(row["event_time"])).total_seconds() <= 300
            ]
            per_code[code] = dict(
                quality_status=value["quality_status"],
                coverage=value["coverage"],
                is_stale=value["is_stale"],
                window_seconds=300,
                window_boundary_precision="minute",
                window_summary=trade_summary(window),
            )
    return dict(
        schema_version=1,
        market_date=date,
        until=until,
        research_only=research_backfill,
        formal_eligible=False,
        strategy_affected=False,
        remote_fetch_performed=False,
        notification_sent=False,
        account_modified=False,
        event_counts=dict(counts),
        missing_publication_count=unpublished,
        failures=invalid,
        evidence=list(latest.values()),
        snapshot_depth=list(embedded.values()),
        trade_coverage=per_code,
        verified_trade_summary=dict(
            count=len(actual),
            volume_hand=sum(row["volume_hand"] for row in actual),
            amount_estimated_yuan=sum(row["amount_yuan"] for row in actual),
            side_counts=dict(Counter(row["side"] for row in actual)),
        ),
    )
