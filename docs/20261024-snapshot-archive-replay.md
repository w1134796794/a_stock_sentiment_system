# Archived Snapshot Replay

## Entry Points

On the intraday replay page, select `归档快照驱动` under `回放依据`.
The existing background-job progress, cancellation, saved JSON and baseline comparison are retained.
Snapshot mode disables remote backfill and uses the frozen 10-day leader pool / production decision scope.
The existing minute diagnostic remains separate and keeps its previous defaults.

```bash
python -X utf8 scripts/replay_snapshot_archive.py --market-date 20261023 --until 10:00:03 --profile leader_pool
```

An optional `--data-root` selects the Web data directory. It is not the cache directory.
The CLI prints JSON; it does not create an archive or send a notification.

## Required Evidence

- `collection_plans/YYYYMMDD.json`: immutable preopen universe and its hash.
- `collection_plans/YYYYMMDD.inputs.json`: matching frozen candidates, risk settings, daily references and amount profiles.
- `market_archive/ingestion.sqlite3`: committed events not yet moved by the archiver.
- `market_archive/market_YYYYMMDD.sqlite3`: daily incremental event archive.
- Event kinds: `snapshot`, `minutes`, `auction`, `sector_minutes`.

Every SQLite input is opened with `mode=ro`. Missing files are never created.
Read the ingestion spool before the daily archive, because the archiver commits the destination before deleting the source.
Deduplicate event IDs; conflicting identities and corrupt rows/databases are reported, not repaired.

## Temporal Boundaries

Replay advances using the original timezone-aware `received_at`, including milliseconds.
All events sharing an exact receipt boundary become visible before its evaluation.
Only the last 180 seconds of observed stock snapshots enter the common snapshot-first evaluator.
Prices, true opens, cumulative volume/amount and receipt times are never interpolated.
Previously received minute responses are clipped to completed minutes at their original receipt time.
A later response cannot provide earlier opening structure, VWAP or volume evidence.

The collector now journals `sector_minutes` **after** publishing a closed constituent minute.
Each record keeps the minute's actual observation and the later publication observation separately.
Replay uses this publication boundary, the full frozen constituent denominator and the same closed-minute checker as live logic.
Old sector-minute files without a publication record are **not** used to invent past availability.
TDX board shadow quotes remain separate and never replace formal THS constituent evidence.

## Interpretation

The report separates first rule confirmation, ever-confirmed state, last evaluation and cutoff freshness state.
Each transition includes the selected snapshot/minute source, fallback reason, individual gates and sector evidence.
Sample counts and observed gaps describe archived observations, not a guarantee of exact three-second sampling.
At the cutoff the last quote is rechecked without creating a quote or simulated trade.
Saved baselines retain their captured archive inputs and cannot silently load newer observations.
Production observations/notifications after the selected cutoff are excluded from reconciliation.

This is a **receipt-time rule replay**, not an exact scheduler/Redis-publication reconstruction.
Stock/minute receipt times precede their actual cache commit; original leases, worker cycles and health gates may be incomplete.
Current code/settings versions are recorded, not represented as the original production version.
Rule confirmation does not grant live eligibility, prove a real fill or prove delivery.
All paths keep `remote_fetch_performed=false`, `notification_sent=false`, `account_modified=false`,
`formal_eligible=false` and `live_runtime_verified=false`.

## Deployment

The new sector publication journal requires deployment and a collector restart before future capture.
Historical missing publication records remain explicitly missing; do not backfill them from closing quotes.
No change to formal buy-point thresholds, allowed entry modes, notification policy or board-shadow enablement is required.
