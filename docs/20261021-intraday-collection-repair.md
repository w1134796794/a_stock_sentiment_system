# 20261021 Intraday Collection Repair

## Changes

- Execution-priority quotes (candidates, leaders, holdings) no longer wait for a
  full constituent sweep. Sector quotes use a separate bounded worker lane and
  publish each 80-symbol result immediately, preserving original receive times.
- `ELTDX_CONSTITUENT_WORKERS` defaults to 4, bounded to 1-8. The quote freshness
  threshold, sector coverage requirement, entry rules and push gates are unchanged.
- Actual constituent quotes are retained as minute price points in
  `stock/sector_quote_minutes/YYYYMMDD.sqlite3` under the cache directory and in
  Redis for two days. Redis stores individual minute fields rather than rewriting
  a growing full-day JSON. Only observed, closed minutes are retained. These are not
  OHLC bars, volumes, reconstructed 3-second snapshots or executable live signals.
- Minute diagnostics and production backtests share the retained sector points
  and existing eltdx opening/previous-close evidence. Saved replay inputs include
  these points. Candidate minute bars are still required independently.
- Background production and observation views archive reason/status snapshots,
  deduplicated per minute and reason. Observations cannot cause a buy or push.
  The report displays the original observation and quote receive times.
- Missing runtime evidence is a warning, not a green success state.

## Deployment And Verification

Upgrade and restart both the Web process and the actual eltdx collector. Updating
only the Web node cannot change a remote collector's cadence or archives. Existing
preopen bundles and past reports must not be overwritten.

Verify the collector's `quote_lane=execution_priority`, constituent sweep duration
and missing counts. Inspect original quote receive ages and per-stock sector
coverage; never increase timestamps or lower coverage to obtain a confirmation.

New quote-minute evidence starts when the updated collector receives data. Older
gaps remain missing. Fetching real historical minutes can repair a rule diagnostic,
but cannot establish timely delivery, retroactively create runtime observations,
or authorize backfilled notifications. No server deployment is implied by local
tests passing.
