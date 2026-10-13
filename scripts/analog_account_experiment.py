"""Freeze/replay isolated accounts or mature frozen holding-state observations."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.ledger import TradingLedger
from core.portfolio.analog_experiment import AccountExperimentConfig, AnalogAccountExperiment
from core.portfolio.signal_comparison import ComparisonConfig, mature_holding_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--comparison-config", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    freeze = actions.add_parser("freeze")
    freeze.add_argument("--label", default="analog-accounts")
    replay = actions.add_parser("replay")
    replay.add_argument("contract_id")
    replay.add_argument("--tape", type=Path, required=True, help="Frozen signals, quotes, market/sector times, original exits and real calendar JSON")
    replay.add_argument("--holding-states", type=Path)
    replay.add_argument("--research", action="store_true")
    replay.add_argument("--output", type=Path, required=True)
    states = actions.add_parser("mature-states")
    states.add_argument("--tape", type=Path, required=True)
    states.add_argument("--output", type=Path, required=True)
    export = actions.add_parser("export-states")
    export.add_argument("--start", required=True)
    export.add_argument("--end", required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = TradingLedger(args.ledger)
    config = ComparisonConfig(**json.loads(args.comparison_config.read_text(encoding="utf-8"))) if args.comparison_config else ComparisonConfig()
    experiment = AnalogAccountExperiment(ledger=ledger, comparison=config)
    if args.action == "freeze":
        print(json.dumps(experiment.freeze(label=args.label), ensure_ascii=False))
        return
    if args.action == "export-states":
        with ledger._connect() as conn:
            rows = conn.execute("SELECT payload_json FROM trading_events WHERE kind='holding_state' "
                                "AND json_extract(payload_json, '$.as_of')>=? "
                                "AND json_extract(payload_json, '$.as_of')<? ORDER BY occurred_at",
                                (args.start, args.end + "T23:59:59")).fetchall()
        result = {"states": [json.loads(row[0]) for row in rows], "published_at": datetime.now().isoformat()}
    else:
        tape = json.loads(args.tape.read_text(encoding="utf-8"))
    if args.action == "mature-states":
        # Each row binds a frozen observation to its exact calendar-offset close.
        from backtest.trade_calendar import TradeCalendar
        calendar = TradeCalendar()
        if not calendar.is_real:
            parser.error("Real trading calendar required")
        result = []
        for row in tape.get("states") or []:
            state = row.get("state") or row
            expected = calendar.offset(state["as_of"][:10].replace("-", ""), config.horizon_days)
            close = next((e for e in tape.get("events") or [] if e.get("session_close") and e["at"][:10].replace("-", "") == expected), {})
            horizon_end = row.get("horizon_end") or close.get("at")
            if not horizon_end:
                result.append({"state": state, "label": None, "reason": "missing_future_horizon_close"})
                continue
            if horizon_end[:10].replace("-", "") != expected:
                parser.error("Holding-state label has the wrong forward horizon")
            result.append(mature_holding_state(state, horizon_end=horizon_end,
                exit_quote=row.get("exit_quote") or (close.get("quotes") or {}).get(state["code"]) or {},
                reference_price=state.get("reference_price") or 0,
                available_at=row.get("available_at") or tape.get("published_at") or "", config=config))
    elif args.action == "replay":
        contract = ledger.experiment(args.contract_id).get("report") or {}
        experiment = AnalogAccountExperiment(ledger=ledger, config=AccountExperimentConfig(**contract.get("configuration", {})), comparison=config)
        from backtest.trade_calendar import TradeCalendar
        calendar = TradeCalendar()
        dates = tape["trading_dates"]
        if not calendar.is_real or calendar.get_trade_dates(dates[0], dates[-1]) != dates:
            parser.error("Tape must contain every date in the real trading calendar")
        historical = json.loads(args.holding_states.read_text(encoding="utf-8")) if args.holding_states else []
        result = experiment.replay(args.contract_id, tape["events"], trading_dates=dates,
                                   research=args.research, historical_states=historical)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(args.output)
    print(str(args.output))


if __name__ == "__main__":
    main()
