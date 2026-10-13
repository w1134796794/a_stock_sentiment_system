"""Read-only price diagnostics; never recreate live health or send notifications."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.minute_entry import ENTRY_WEAK, normalize_minute_bars
from backtest.trade_calendar import TradeCalendar
from config.settings import APP_DB_PATH, CACHE_DIR, HOLDING_DB_PATH
from core.realtime.entry_signal_service import classify_entry_mode
from core.utils.stock_code_utils import StockCodeUtils


def price_diagnostic(raw: pd.DataFrame, daily: dict, deadline: str) -> dict:
    data = normalize_minute_bars(raw)
    if data.empty:
        return {"status": "missing_minutes"}
    previous_close = float(daily.get("pre_close") or 0)
    open_price = float(daily.get("open") or 0)
    mode = classify_entry_mode(open_price, previous_close)
    opening = data[data["time"] <= "09:35:00"].head(5)
    if (len(opening) < 5 or not math.isfinite(previous_close) or not math.isfinite(open_price)
            or previous_close <= 0 or open_price <= 0):
        return {"status": "missing_opening_evidence", "minute_count": len(data)}
    high, low = float(opening["high"].max()), float(opening["low"].min())
    scan = data[(data["time"] > str(opening.iloc[-1]["time"])) & (data["time"] <= deadline)]
    broken = scan[scan["low"] < low * .999]
    price_mask = (scan["close"] >= scan["vwap"]) & (scan["high"] > high)
    if mode == ENTRY_WEAK:
        price_mask &= scan["close"] >= previous_close
    triggers = scan[price_mask]
    first_break = str(broken.iloc[0]["time"]) if not broken.empty else ""
    first_trigger = str(triggers.iloc[0]["time"]) if not triggers.empty else ""
    return {
        "status": "price_evidence_only", "entry_mode": mode,
        "minute_count": len(data), "first_minute": str(data.iloc[0]["time"]),
        "last_minute": str(data.iloc[-1]["time"]),
        "open": open_price, "previous_close": previous_close,
        "open_gap_pct": round((open_price / previous_close - 1) * 100, 4),
        "opening_high": high, "opening_low": low, "deadline": deadline,
        "first_price_trigger": first_trigger, "first_opening_low_break": first_break,
        "weak_price_trigger_before_break": bool(first_trigger and (not first_break or first_trigger < first_break)),
        "vwap_sources": sorted(set(data["vwap_source"].astype(str))),
        "amount_estimated": bool(data["amount_is_estimated"].any()),
        "trigger_rows": [{"time": str(row.time), "price": float(row.close), "vwap": float(row.vwap)}
                         for row in triggers.head(5).itertuples()],
    }


def runtime_evidence(path: Path, table: str, date: str, codes: list[str]) -> dict:
    if table not in {"realtime_signal_events", "signal_evidence"}:
        raise ValueError("Unsupported diagnostic table")
    if not path.exists():
        return {"status": "missing_database", "rows": []}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return {"status": "missing_table", "rows": []}
        placeholders = ",".join("?" for _ in codes)
        column = "stock_code" if table == "realtime_signal_events" else "code"
        rows = con.execute(f"SELECT * FROM {table} WHERE market_date=? AND {column} IN ({placeholders})",
                           [date, *codes]).fetchall()
        result = []
        for source in rows:
            row = dict(source)
            field = "payload_json" if table == "realtime_signal_events" else "evidence_json"
            raw = json.loads(row.pop(field))
            row["evidence"] = {key: raw.get(key) for key in (
                "confirm_status", "confirm_time", "trigger_reason", "veto_reason", "health_gate_reason",
                "quote_received_at", "sector_coverage", "sector_data_completeness", "notification_status",
            )}
            if table == "signal_evidence" and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_events'").fetchone():
                row["delivery_events"] = [dict(event) for event in con.execute(
                    "SELECT kind, occurred_at, payload_json FROM trading_events WHERE signal_id=? "
                    "AND kind IN ('notification', 'notification_attempt') ORDER BY occurred_at", (row["signal_id"],)
                ).fetchall()]
            result.append(row)
    return {"status": "recorded" if result else "no_local_runtime_evidence", "rows": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--deadline", default="10:00:00")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    codes = list(dict.fromkeys(StockCodeUtils.standardize_code(code.strip(), add_suffix=False)
                              for code in args.codes.split(",") if code.strip()))
    daily_path = CACHE_DIR / "stock" / "all_daily" / f"{args.date}.csv"
    daily = pd.read_csv(daily_path, dtype={"ts_code": str}) if daily_path.exists() else pd.DataFrame()
    stocks = []
    for code in codes:
        ts_code = StockCodeUtils.standardize_code(code, add_suffix=True)
        path = CACHE_DIR / "stock" / "tick" / f"{ts_code}_{args.date}.csv"
        raw = pd.read_csv(path) if path.exists() else pd.DataFrame()
        current = daily[daily["ts_code"] == ts_code] if not daily.empty else pd.DataFrame()
        stocks.append({"code": code, "minute_path": str(path),
                       **price_diagnostic(raw, current.iloc[0].to_dict() if not current.empty else {}, args.deadline)})
    report = {
        "market_date": args.date, "candidate_date": TradeCalendar().prev(args.date),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "classification": "historical_price_diagnostic", "live_runtime_verified": False,
        "limitations": ["Minute prices cannot reconstruct historical 3-second snapshots or collector leases.",
                        "Backfilled data cannot prove it was available before the original signal.",
                        "Price triggers do not establish sector, volume, tradability or delivery acceptance."],
        "stocks": stocks,
        "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", args.date, codes),
        "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", args.date, codes),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
