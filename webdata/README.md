# Webdata Boundary

`webdata/` is the default local runtime-data directory. Files produced by the
web UI, daily generation, realtime refresh, screening, and backtests are not
source artifacts and must not be committed.

Mutable databases such as `app.sqlite`, `auth.sqlite`, `kb.sqlite`, and
`factors.duckdb` are created or updated at runtime and are ignored by Git.
Production should set `WEB_DATA_DIR`, `CACHE_DIR`, and `OUTPUT_DIR` to writable
directories outside the checked-out repository.

If a release genuinely needs a prebuilt database, put a sanitized, schema-only
SQLite file in `webdata/bootstrap/`. Never include live sessions, login logs,
passwords, tokens, customer data, market snapshots, or backtest output.
