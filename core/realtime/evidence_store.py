"""Durable evidence before hot publication; isolated backfill storage and cursors."""

from __future__ import annotations

from core.realtime.market_evidence import fingerprint, timestamp
from core.realtime.trading_session import shanghai_now


class EvidenceStore:
    def __init__(self, journal, backend=None):
        self.journal, self.backend = journal, backend

    @staticmethod
    def key(date, code, kind):
        return f"realtime:evidence:v1:{date}:{code}:{kind}"

    @staticmethod
    def cursor_key(date, code, plan_id, capture_mode):
        return f"{capture_mode}:{date}:{code}:{plan_id}"

    def cursor(self, date, code, plan_id="", capture_mode="live"):
        return self.journal.evidence_cursor(self.cursor_key(date, code, plan_id, capture_mode))

    def write(self, result, *, plan_id="", capture_mode="live"):
        value = result.envelope(plan_id=plan_id, capture_mode=capture_mode)
        if capture_mode == "live" and not plan_id:
            raise ValueError("Live evidence requires a frozen collection plan")
        # Backfills must use their own journal tree, never the live replay archive.
        if capture_mode == "backfill" and self.backend is not None:
            raise ValueError("Backfill cannot publish into live cache")
        if result.kind == "opening_match" and result.rows and self.backend is not None:
            reference = self.backend.get_json(f"realtime:auction:{result.market_date}:{value['code']}") or {}
            row = result.rows[0]
            conflicts = []
            from core.realtime.market_evidence import number

            for field, old_key, tolerance in [("price", "开盘价", 0.011), ("amount_yuan", "竞价成交额", 1.0)]:
                old, new = number(reference.get(old_key)), number(row.get(field))
                if old is not None and old > 0 and new is not None and abs(old - new) > tolerance:
                    conflicts.append(field)
            if conflicts:
                value["quality_status"] = result.status = "partial"
                value["quality_reasons"] = result.reasons = list(result.reasons) + ["opening_reference_conflict"]
                value["coverage"] = {**value["coverage"], "conflicting_fields": conflicts}
        cursor = None
        if result.kind == "trade_batch" and result.coverage.get("sequence_verified"):
            cursor = (
                self.cursor_key(result.market_date, value["code"], plan_id, capture_mode),
                {key: result.coverage[key] for key in ("anchor", "next_sequence")},
            )
        if capture_mode == "backfill" and result.status in {"ok", "empty"} and result.coverage.get("complete", True):
            cursor = (
                f"backfill-complete:{result.market_date}:{value['code']}:{result.kind}",
                {"complete": True, "received_at": result.received_at, "rows": len(result.rows)},
            )
        value["observation_id"] = fingerprint(value)
        self.journal.append(result.kind, result.market_date, [value], cursor=cursor)
        if capture_mode == "live" and self.backend is not None:
            # Cache publication precedes the durable observation of publication.
            # Replay uses the later marker and never invents an exact Redis commit time.
            self.backend.set_json(self.key(result.market_date, value["code"], result.kind), value, 86400)
            published = shanghai_now().isoformat(timespec="milliseconds")
            marker = dict(
                code=value["code"],
                received_at=published,
                published_at=published,
                observation_id=value["observation_id"],
                capture_mode="live",
                collection_plan_id=plan_id,
                market_date=result.market_date,
            )
            self.journal.append("evidence_publication", result.market_date, [marker])
            value["published_at"] = published
        return value

    def read(self, date, code, kind, *, now=None, stale_seconds=15):
        if self.backend is None:
            return {}
        value = self.backend.get_json(self.key(date, code, kind))
        if not value:
            return {}
        age = (shanghai_now(now) - timestamp(value["received_at"])).total_seconds()
        return {**value, "age_seconds": age, "is_stale": not 0 <= age <= stale_seconds}
