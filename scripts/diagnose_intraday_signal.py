"""Read-only price diagnostics; never recreate live health or send notifications."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.minute_entry import ENTRY_WEAK, normalize_minute_bars
from backtest.trade_calendar import TradeCalendar
from config.settings import APP_DB_PATH, CACHE_DIR, HOLDING_DB_PATH
from core.realtime.entry_signal_service import classify_entry_mode
from core.realtime.trading_session import shanghai_now
from core.utils.stock_code_utils import StockCodeUtils


def price_diagnostic(raw: pd.DataFrame, daily: dict, deadline: str, *, evaluator=None) -> dict:
    data = normalize_minute_bars(raw)
    if data.empty:
        return {"status": "missing_minutes"}
    from core.signals.opening import positive_price
    from core.signals.production_rules import minute_evaluator
    evaluator = evaluator or minute_evaluator()
    previous_close = positive_price(daily.get("pre_close"))
    open_price = positive_price(daily.get("open"))
    mode = classify_entry_mode(open_price, previous_close, weak_max_gap=evaluator.weak_max_gap,
                               continuation_max_gap=evaluator.continuation_max_gap)
    opening = data[data["time"] <= "09:35:00"].head(5)
    if len(opening) < 5 or previous_close is None or open_price is None:
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
        if not codes:
            return {"status": "no_local_runtime_evidence", "rows": []}
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
                "status", "profile", "confirm_status", "confirm_time", "minute_closed_at",
                "reason", "data_status", "sector_detail", "trigger_reason", "veto_reason", "health_gate_reason",
                "quote_received_at", "sector_coverage", "sector_data_completeness", "notification_status",
                "entry_mode", "opening_evidence", "entry_parameters", "quote_stale_seconds", "board_feedback",
            )}
            if table == "signal_evidence" and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_events'").fetchone():
                row["delivery_events"] = [dict(event) for event in con.execute(
                    "SELECT kind, occurred_at, payload_json FROM trading_events WHERE signal_id=? "
                    "AND kind IN ('notification', 'notification_attempt') ORDER BY occurred_at", (row["signal_id"],)
                ).fetchall()]
            result.append(row)
        if table == "signal_evidence" and con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_observations'").fetchone():
            observations = con.execute(
                f"SELECT * FROM runtime_observations WHERE market_date=? AND code IN ({placeholders}) ORDER BY observed_at",
                [date, *codes]).fetchall()
            for item in observations:
                raw = json.loads(item["payload_json"])
                result.append({"event_id": item["observation_id"], "signal_id": item["signal_id"],
                               "code": raw.get("code"), "profile": raw.get("profile"),
                               "status": raw.get("status"), "observed_at": item["observed_at"],
                               "evidence_kind": "runtime_observation", "evidence": raw, "delivery_events": []})
    return {"status": "recorded" if result else "no_local_runtime_evidence", "rows": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--deadline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-only", action="store_true", help="Only inspect recorded confirmation and delivery; no market requests")
    args = parser.parse_args()
    codes = list(dict.fromkeys(StockCodeUtils.standardize_code(code.strip(), add_suffix=False)
                              for code in args.codes.split(",") if code.strip()))
    candidate_date = TradeCalendar().prev(args.date)
    opening_rows, previous, evaluator = {}, {}, None
    if not args.runtime_only:
        from core.realtime.day_replay import DayReplayLoader
        from core.operations.preopen_evidence import PreopenEvidenceStore
        from core.signals.production_rules import minute_evaluator
        from risk.risk_config import RiskConfig
        loader = DayReplayLoader(cache_dir=CACHE_DIR)
        loader._opening_evidence(args.date, codes, fetch_missing=False)
        opening_rows = loader.opening_evidence
        frozen = PreopenEvidenceStore().load(candidate_date, args.date)
        inputs = frozen['inputs'] if frozen else {}
        evaluator = minute_evaluator(RiskConfig.from_dict(inputs['risk_config']) if inputs.get('risk_config') else None)
        prior_path = CACHE_DIR / 'stock' / 'all_daily' / f'{candidate_date}.csv'
        prior = pd.read_csv(prior_path, dtype={'code': str, 'ts_code': str}) if prior_path.exists() else pd.DataFrame()
        for row in [*prior.to_dict('records'), *(inputs.get('daily_rows') or [])]:
            previous[StockCodeUtils.standardize_code(row.get('code') or row.get('ts_code'), add_suffix=False)] = row
    stocks = []
    for code in codes:
        ts_code = StockCodeUtils.standardize_code(code, add_suffix=True)
        path = CACHE_DIR / "stock" / "tick" / f"{ts_code}_{args.date}.csv"
        if args.runtime_only:
            stocks.append({"code": code, "status": "not_evaluated"})
            continue
        raw = pd.read_csv(path) if path.exists() else pd.DataFrame()
        from core.signals.opening import opening_context
        opening = opening_context(opening_rows.get(code, {}), previous.get(code, {}))
        stocks.append({"code": code, "minute_path": str(path),
                       "opening_evidence": opening,
                       **price_diagnostic(raw, {'open': opening['open_price'], 'pre_close': opening['pre_close']},
                                          args.deadline or evaluator.deadline, evaluator=evaluator)})
    report = {
        "market_date": args.date, "candidate_date": candidate_date,
        "generated_at": shanghai_now().isoformat(timespec="seconds"),
        "classification": "recorded_runtime_diagnostic" if args.runtime_only else "historical_price_diagnostic",
        "live_runtime_verified": False,
        "limitations": ["Minute prices cannot reconstruct historical 3-second snapshots or collector leases.",
                        "Backfilled data cannot prove it was available before the original signal.",
                        "Price triggers do not establish sector, volume, tradability or delivery acceptance."],
        "stocks": stocks,
        "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", args.date, codes),
        "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", args.date, codes),
    }
    from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
    report["recorded_runtime"] = {key: report[key] for key in ("confirmation_history", "signal_ledger")}
    attach_runtime_diagnostics(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
