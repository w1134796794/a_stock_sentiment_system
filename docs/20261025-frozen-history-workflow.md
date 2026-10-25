# Frozen Historical Backtest Workflow

The `/portfolio/backtest` administrator page now has five explicit stages. Each action
runs a separate Python child process. The Web handler only validates the request and
starts/reads jobs; it never queries a market API itself.

1. **Read saved pools**: read the already-generated leader and decision pools for the
   previous trading day of each selected market date. Prefer original preopen bundles;
   otherwise read the postclose `leader_signal_history` partition and saved decision JSON.
   Preserve archived membership, including currently disabled strategies, and deduplicate
   stock codes. Save a job-owned copy and hash. No ETL, factor calculation, screening,
   model training or market API calls occur. Missing archived pools are reported rather
   than rebuilt. The postclose fallback preserves all archived leader members, not a
   reconstructed historical top-30 quote plan.
2. **Prefetch**: gather the union of saved pool members and potential carried holdings.
   Under the new `trend_score` policy, full THS constituent acquisition is disabled;
   use only existing mapped board-index archives for optional scoring. The explicit
   legacy `breadth_required` contract still gathers complete THS constituent groups.
   Fetch available stock minute/auction/opening/suspension
   evidence separately. Reuse existing amount profiles read-only; do not train models
   or recalculate daily factors as part of market-data backfill.
   Pool/possible-held securities have priority over constituent downloads; minute
   downloads precede remote opening-price completion. Live progress reports success,
   failure, confirmed full-day suspension skips, provider and row count. Same-date
   authoritative cached full-day suspensions avoid futile minute/auction/open requests,
   without deleting members from the frozen sector denominator. Unknown status is not
   inferred from empty prices. Final acquisition counts and failure previews are saved.
   Missing source data is not interpolated or inferred from absence.
3. **Validate and seal**: validate full-session minutes, real opens, previous closes,
   previous amounts, required auction evidence, same-day daily/minute closing prices,
   suspension status and, for the legacy contract only, minute-by-minute constituent coverage. A weak market does not
   mean missing evidence. Missing required evidence blocks sealing. Freeze compressed
   day packets, profiles, risk/account configuration, calendar, and available original
   snapshot/runtime evidence with content hashes and a code version.
4. **Read-only run**: use the existing production-rule `BacktestEngine` with a storage-only
   frozen loader. No DataManager/provider/current profile/calendar construction is needed.
   Strategy mode restrictions and entry thresholds are unchanged. No market fetching,
   notification delivery, default paper-account replacement or automatic result adoption.
   Save daily account checkpoints under the frozen contract; resuming skips committed days.
   Modified input/checkpoint/code versions are rejected. New source data requires a new seal.
5. **Reconcile**: run archived-snapshot replay using the evidence captured at sealing,
   attach the captured immutable runtime/notification records and show minute vs snapshot
   confirmation timestamps. Missing or incomplete original snapshots remain explicit;
   backfilled minutes and closing quotes never become historical 3-second observations.
   A comparison is diagnostic, not proof of real execution or historical code equivalence.

## Operation

- Independent acquisition: **System > 历史行情补数**, at `/market-backfill`, exposes
  only stages 1 through 3. It reuses the same resumable jobs and caches, without running
  live strategy workers, account trades or notifications. After sealing, **使用此版本回测** opens that exact
  job on the historical backtest page. No data is re-fetched by the read-only run.
- Daily factors and pool artifacts must already exist. Market sources follow the current
  DataManager configuration: valid local minute caches, then local AxData SDK, eltdx historical minutes
  and Ashare fallback. Per-security daily gaps and historical 09:25 auctions also prefer
  AxData. Whole-market factor inputs and THS membership identities are not replaced.
  See `20261025-axdata-backfill.md`. Provider history/permissions may still
  leave gaps, which remain explicit validation failures. Backfills do not publish live
  Redis quotes or replace original 3-second archives.
- `20261025-board-trend-score.md` describes the new optional board trend score, preserved
  legacy frozen configuration and reduced collection universe. Missing optional board
  archives do not make otherwise complete stock evidence fail validation.
- Administrator: fill the existing start/end fields, click **Create task**, then execute
  stages 1 through 5. Re-select tasks to resume after Web/worker restarts.
- Prefetch is refused on trading days between 09:15 and 15:15 Shanghai time. Reading saved
  pool lists is local and may run during trading hours.
- Start/end must be completed real trading days, earlier than today. Jobs are bounded to
  60 trading days; use smaller windows first because constituent acquisition can be large.
- The skip-missing-candidates shortcut is not offered for this strict workflow. The old
  replay APIs remain compatible; running the new workflow does not rewrite paper holdings.
- The workflow result summary, trades and JSON reports belong to the selected frozen task.
  The existing account section below it still displays previously imported accounts.
- Cancellation is cooperative: it is checked at the next progress boundary, after an
  in-flight API request finishes. Cached data and committed
  daily account checkpoints remain available. No source data is removed.
- Optional source/API availability still governs whether historical data can be retrieved.
  A successful interface call is not enough: incomplete returned minutes fail validation.

## Files and CLI

Jobs live under `WEB_DATA_DIR/history_workflows/JOB_ID/`:

```text
status.json                     # stage status and bounded missing-evidence preview
worker.log                      # child-process log; admin API exposes only its final 16 KB
pools/DATE.json.gz              # read-only copies of previously generated pool membership
pool_manifest.json              # integrity hashes for those copies
staging/DATE.json.gz             # mutable, independently acquired inputs
validation.json                 # full missing-evidence report
frozen/VERSION/manifest.json     # sealed configuration, calendar, file/code hashes
frozen/VERSION/days/*.json.gz    # immutable minute research inputs
frozen/VERSION/snapshots/*.gz    # only actual archives, if available
checkpoint-VERSION.json         # completed daily state and integrity hash
result.json                     # frozen research results; not a live-account update
reconciliation.json             # original-archive/runtime reconciliation or missing status
```

From the project root, under the same service user and `.env`:

```bash
.venv/bin/python -X utf8 scripts/history_backtest_workflow.py create --start 20261021 --end 20261023
MARKET_DATA_NODE_ROLE=server .venv/bin/python -X utf8 scripts/history_backtest_workflow.py step JOB_ID prepare
MARKET_DATA_NODE_ROLE=collector .venv/bin/python -X utf8 scripts/history_backtest_workflow.py step JOB_ID prefetch
.venv/bin/python -X utf8 scripts/history_backtest_workflow.py step JOB_ID freeze
.venv/bin/python -X utf8 scripts/history_backtest_workflow.py step JOB_ID run
.venv/bin/python -X utf8 scripts/history_backtest_workflow.py step JOB_ID reconcile
.venv/bin/python -X utf8 scripts/history_backtest_workflow.py status JOB_ID
```

The Web launcher sets `MARKET_DATA_NODE_ROLE=collector` only for its prefetch
child, and `server` for the read-only stages. It does not change global worker settings.
CLI users must explicitly select the allowed acquisition role when needed. No service
restart, systemd installation, real notification or scheduled backtest is added.

## VWAP Compatibility

Zero cumulative volume now uses numeric missing denominators (`where`) instead of
replacing zero with `pd.NA`, which coerced numeric arrays to `object`. This removes the
pandas implicit-downcasting warning without disabling warnings globally. Existing
provider-average precedence, cumulative volume/amount calculations, fallback prices,
volume units and point-in-time prefixes are preserved.
