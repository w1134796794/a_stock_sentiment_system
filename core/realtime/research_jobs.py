"""Sequential, resumable research capture and source-isolated integrity audit."""

from __future__ import annotations

from core.realtime.market_evidence import fingerprint
from core.realtime.research_store import ResearchStore
from core.realtime.trading_session import shanghai_now


def run_research(root, provider, *, codes, kinds, start, end, period="day", max_pages=20, refresh=False):
    store = ResearchStore(root)
    config = dict(codes=codes, kinds=kinds, start=start, end=end, period=period, max_pages=max_pages)
    job = dict(
        job_id=fingerprint([config, shanghai_now().isoformat()]),
        state="running",
        config=config,
        tasks=[],
        started_at=shanghai_now().isoformat(),
        research_only=True,
        formal_eligible=False,
    )
    store.save_job(job)
    for code in codes:
        for kind in kinds:
            request = dict(
                code=code,
                kind=kind,
                start=start,
                end=end,
                period=period,
                adjust={"bars_raw": "none", "bars_qfq": "qfq", "bars_hfq": "hfq"}.get(kind),
            )
            try:
                if not refresh and store.completed(request):
                    item = dict(code=code, kind=kind, status="skipped_complete")
                else:
                    value = provider.get_research(kind, code, start, end, period=period, max_pages=max_pages)
                    identity = store.write(value)
                    item = dict(
                        code=code,
                        kind=kind,
                        status=value["quality_status"],
                        rows=len(value["rows"]),
                        version_id=identity,
                        reasons=value["quality_reasons"],
                    )
            except Exception as exc:
                item = dict(code=code, kind=kind, status="failed", reasons=[str(exc)])
            job["tasks"].append(item)
            store.save_job(job)
    job.update(
        state="done" if all(r["status"] in {"ok", "skipped_complete"} for r in job["tasks"]) else "partial",
        finished_at=shanghai_now().isoformat(),
    )
    store.save_job(job)
    return job


def audit_prices(raw_version, other_version, actions=None):
    """Compare only identical period, unadjusted prices. Never overwrite either source."""
    if raw_version.get("adjust") != "none" or other_version.get("adjust") != "none":
        raise ValueError("Price audit requires unadjusted sources")
    if raw_version.get("code") != other_version.get("code") or raw_version.get("period") != other_version.get("period"):
        raise ValueError("Price audit scope mismatch")
    right = {row["event_time"]: row for row in other_version["rows"]}
    action_days = {str(row.get("date", "")).replace("-", "") for row in (actions or {}).get("rows", [])}
    differences = []
    for row in raw_version["rows"]:
        other = right.get(row["event_time"])
        if not other:
            continue
        fields = [key for key in ("open", "high", "low", "close") if abs(row[key] - other[key]) > 0.011]
        if fields:
            differences.append(
                dict(event_time=row["event_time"], fields=fields, corporate_action_date=row.get("date") in action_days)
            )
    return dict(
        compared_count=len(set(right) & {r["event_time"] for r in raw_version["rows"]}),
        differences=differences,
        source_data_modified=False,
        formal_eligible=False,
    )
