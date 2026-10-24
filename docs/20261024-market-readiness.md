# Server Market Data Readiness Audit

## Purpose

`scripts/check_market_readiness.py` checks whether evidence needed by the live strategy
and archived-snapshot replay exists, is continuous, and is aligned to the frozen universe.
It does not run a strategy, fetch market data, backfill history, send a notification, or change an account.
It does not change formal entry thresholds. A ready result is not a buy signal.
Frozen JSON is read without creating lock files. SQLite archives/ledgers use `mode=ro`,
never schema creation or repair; SQLite may maintain its standard WAL/SHM coordination files
in existing database directories. Do not use `immutable=1` against live WAL databases:
it could hide committed events still in the WAL. Source tables and business payloads remain unchanged.

The script reads the real `data/trade_calendar.csv`. Unknown dates or missing calendars fail closed;
there is no weekday approximation. Confirmed holidays return `skipped_non_trading_day` with exit code 0.

## Manual Checks

Run from the deployed repository with the same environment and service account as the workers:

```bash
cd /opt/a_stock_sentiment_system
sudo -u deploy .venv/bin/python -X utf8 scripts/check_market_readiness.py --output-dir outputs/market-readiness
```

`config.settings` loads the project's environment configuration. The systemd service explicitly uses
the same `.env` file as the market workers. A standalone shell does not inherit a unit's extra environment
overrides; keep Redis URL/prefix and paths consistent across all services.

Historical full-day audit, without Redis or market connections:

```bash
sudo -u deploy .venv/bin/python -X utf8 scripts/check_market_readiness.py \
  --date 20261023 --phase postclose --offline --output-dir outputs/market-readiness
```

Optional `--board-shadow` also checks the frozen TDX mapping and board snapshot continuity.
It is automatically enabled when `ELTDX_BOARD_SHADOW_ENABLED=true`.
Missing shadow evidence produces a separate warning, never a formal THS buy-point veto.

## Automatic Daily Execution

After deploying these scripts, execute in the server's **root terminal**:

```bash
cd /opt/a_stock_sentiment_system
.venv/bin/python -X utf8 scripts/install_market_readiness_systemd.py \
  --project-root /opt/a_stock_sentiment_system --user deploy --install
systemctl start a-stock-readiness.timer
systemctl list-timers a-stock-readiness.timer --all --no-pager
```

Installation enables the timer for boot but does not start it or change market workers.
Starting the timer schedules future checks; it does not restart the collector/archiver/strategy.
No new sudo privilege for `deploy` is required. Install the timer once as root.

Schedule in **Asia/Shanghai**:

- 09:20: frozen universe, configuration, historical references, amount model and service/Redis availability.
- 09:35 through 11:25 and 13:05 through 14:55: approximately every five minutes, at second 20.
- 15:15: the full trading day, including the morning/afternoon session boundaries.
- Calendar holiday: skip data/Redis checks. Midday break is not counted as a data gap.

The timer triggers daily and the script applies the real exchange calendar.
There is no missed-run catch-up (`Persistent=false`), no retry loop, and no overlapping invocation
of the same oneshot unit. Checks run at low CPU/I/O priority. Timeout is ten minutes.

To test the installed service immediately:

```bash
systemctl start a-stock-readiness.service
journalctl -u a-stock-readiness.service -n 100 --no-pager
```

Exit code 2 means data is not ready (the service will show failed, with details in its JSON/journal).
The timer will invoke it again at the next scheduled time. Exit code 0 means ready, warning,
or a confirmed non-trading-day skip. No real alert messages are sent automatically.

## Report Interpretation

Reports are written only when `--output-dir` is supplied:

```text
outputs/market-readiness/latest.json
outputs/market-readiness/YYYYMMDD/HHMMSS-PHASE.json
outputs/market-readiness/YYYYMMDD/HHMMSS-PHASE.md
```

The service user must be able to write this report directory and read the frozen inputs, archive,
runtime ledger and Redis namespace. Reports are private (`UMask=0077` in systemd).
Report retention is not automatic; no existing files or data are deleted.

- `live_ready`: current real-time prerequisites; null outside preopen/live phases.
- `replay_ready`: evidence in the reported archive window, including original runtime observations.
- `whole_day_replay_ready`: set only by a postclose full-day audit. Intraday success never implies it.
- `board_shadow_ready`: optional, separate from both formal readiness fields.
- `checks`: exact failing component, code, missing minutes, coverage denominator, gap examples and ages.
- `status`: `ready`, `warning`, `not_ready` or `skipped_non_trading_day`.

Preopen readiness intentionally excludes today's future opening/auction/minute snapshots.
Intraday archive scans use the last 180 seconds; minute responses use the latest actually received
response per stock, rather than decoding all earlier growing responses.
Postclose scans cover 09:30-11:30 and 13:00-15:00. The final minute/publication may legitimately
be received after 15:00; the report separately states the receipt cutoff (up to 15:05).
That does not claim those records were available earlier in the trading session.

### Evidence Checked

- Frozen plan/bundle hashes, previous real trading day, preopen health and executable candidate scope.
- Candidate previous close/amount and frozen minute-amount profile.
- Code-bound THS constituent identities with the same delisting exclusions/full denominator as production.
- Original archived snapshot identity, quality, true open/previous close, cumulative fields and time progression.
- Snapshot head/tail coverage and gaps without interpolation; lunch is excluded.
- Minute identities, full expected session grid and receipt lag.
- Required auction evidence for strategies explicitly requiring it.
- Closed constituent-minute publication records, current coverage and three-minute same-reference pairing.
- Immutable production runtime observations and their continuity; no buy confirmation is required.
- Separate per-stock evaluation duration, quote age **at completion**, and current full-cycle duration.
- Live Redis quotes, hot snapshot sequences, actual minute cache, actual sector-minute cache,
  collector lease/plan, archiver backlog, strategy plan/heartbeat, and optional systemd service states.

An example of 654 ms evaluation duration and 3.6 seconds completion quote age is not automatically
an error. Quote ages use the configured quote freshness budget (default 12 seconds); snapshot gaps
default to 8 seconds. A slow full cycle is reported separately as a throughput warning.
The audit records its thresholds; it does not modify the strategy's configuration.

Old archives without snapshot/sector publication/runtime evidence remain explicitly unready.
Current closing quotes or a complete-looking minute chart cannot repair that missing history.
