# AxData Historical Acquisition

Explicit historical prefetch workers prefer AxData **locally**, without an HTTP
service. `AxDataClient(mode="local", data_root=...)` and `client.call(...)` are used, not
`client.daily(...)`, which queries already-collected AxData core tables. An ambient
`AXDATA_API_BASE` cannot change this adapter into a remote client.

## Installation

The verified package is `axdata==0.1.4`, requiring Python **3.11 or newer**. It is included
in `requirements.txt` with a Python version marker. Existing Python 3.10 installations
keep their previous sources and log that AxData is unavailable; this change does not
replace the server's interpreter or silently upgrade its virtual environment.

```bash
.venv/bin/python --version
.venv/bin/python -m pip install axdata==0.1.4
```

No API URL/token, API service or Web console is required. The bundled TDX provider must
be available/enabled and its upstream hosts reachable. Optional project configuration:

```dotenv
HISTORY_AXDATA_FIRST=true
# Defaults to WEB_DATA_DIR/axdata/data, outside the project-root AxData defaults.
HISTORY_AXDATA_ROOT=/srv/a-stock/axdata/data
```

## Scope and Contracts

- Pool preparation only reads previously generated leader/decision membership. It never
  invokes AxData, rebuilds factors, reruns screening or trains an amount profile.
- Valid existing caches are reused. Only missing/invalid historical evidence calls sources.
- Minute order: **AxData → eltdx → Ashare**. AxData uses `stock_kline_minute_tdx`, `period=1m`,
  `adjust=none`. Its returned recent history is strictly filtered by the requested trading
  date; this endpoint does not accept a date parameter. Missing dates fall back.
- `stock_intraday_history_tdx` is a price/volume timeline, **not OHLC**. It is not silently
  expanded into supposedly real minute bars. Seconds K-lines do not become original 3s
  quote snapshots or prove historical receipt times.
- Daily gaps use `stock_kline_daily_tdx`, unadjusted. Volume is hands; minute amount is yuan;
  daily `amount` is converted to the existing thousand-yuan contract while retaining
  `amount_yuan`. Missing prior close/return fields are marked unavailable, not reconstructed
  from a possibly gapped adjacent day. Existing whole-market Tushare ETL remains unchanged.
- Historical auctions use `stock_auction_result_history_tdx` and require the requested date,
  exactly 09:25, distinct trade indices and consistent price/hand-volume/yuan-amount.
  This is a final matching result, not a historical auction-process series.
- Wrong codes, timestamps, periods, OHLC, units, partial days and duplicates are rejected;
  they cannot replace a useful minute cache. Origin/interface/capture time remain in rows.
- Constituent stock minutes can use AxData. THS sector identities/membership denominators
  are unchanged; TDX/other-provider same-name sectors are not substituted.
- SDK unavailability or bad results fall back. Transport/provider failures have a 30-second
  per-interface cooldown, avoiding identical failures for every constituent.
- Only explicit historical workers set `prefer_axdata_history`. Server-role and cache-only
  readers do not initialize AxData. Frozen runs and archived replay remain storage-only.

The workflow context records the SDK version/source policy; frozen minute rows retain
their actual source. Capturing data now does not prove it was available intraday then.

## Official References

- [Package and direct local SDK](https://pypi.org/project/axdata/)
- [Minute K-lines](https://electkismet.github.io/AxData/interfaces/stock_kline_minute_tdx.html)
- [Historical timeline](https://electkismet.github.io/AxData/interfaces/stock_intraday_history_tdx.html)
- [Daily K-lines](https://electkismet.github.io/AxData/interfaces/stock_kline_daily_tdx.html)
- [Historical final auction](https://electkismet.github.io/AxData/interfaces/stock_auction_result_history_tdx.html)
