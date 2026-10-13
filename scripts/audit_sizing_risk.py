"""Independently reconcile sealed sizing accounts and shared signal prices."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.operations.replay_research import FrozenEvidence, ReplayResearch, atomic_json, digest


def reconcile_daily_marks(state, evidence):
    cash, holdings, checked = float(state['initial_capital']), {}, 0
    for nav in state['daily_nav']:
        date = nav['date']
        for code, position in holdings.items():
            if evidence.read('suspension', code, date).get('state') == 'full_day':
                continue
            bar = evidence.read('daily', code, date)
            reference = float(bar.get('pre_close') or 0)
            ratio = reference / position['last_close'] if position['last_close'] > 0 else 0
            if reference > 0 and .2 <= ratio <= 5 and abs(ratio - 1) > .001:
                position['shares'] /= ratio
        for trade in [t for t in state['trade_history'] if t['date'] == date]:
            code = trade['stock_code']
            if trade['action'] == 'BUY':
                cash -= float(trade['position_size'])
                holdings[code] = {'shares': float(trade['shares']), 'market_value': 0.,
                                  'last_close': float(trade['entry_price'])}
            else:
                position = holdings.pop(code)
                if not math.isclose(position['shares'], float(trade['shares']), abs_tol=1e-5, rel_tol=0):
                    raise ValueError(f'Historical reference quantity mismatch: {date}/{code}')
                cash += float(trade['position_size']) + float(trade['pnl'])
        for code, position in holdings.items():
            if evidence.read('suspension', code, date).get('state') == 'full_day':
                continue
            close = float(evidence.read('daily', code, date).get('close') or 0)
            if close <= 0:
                raise ValueError(f'Missing close in independent valuation: {date}/{code}')
            position['last_close'] = close
            position['market_value'] = position['shares'] * close
        value = sum(p['market_value'] for p in holdings.values())
        if (len(holdings) != nav['position_count'] or
                any(not math.isclose(actual, float(nav[key]), abs_tol=1e-5, rel_tol=0)
                    for key, actual in [('cash', cash), ('position_value', value), ('total_value', cash + value)])):
            raise ValueError(f'Independent daily close NAV mismatch: {date}')
        checked += 1
    return checked


def audit(directory, summary):
    result, signals, plans = {}, {}, {}
    for arm, report in summary["results"].items():
        checkpoint = json.loads((directory / report["state_path"]).read_text(encoding="utf-8"))
        state = checkpoint["state"]
        if checkpoint["state_hash"] != digest(state):
            raise ValueError("Account checkpoint changed")
        initial = float(state["initial_capital"])
        holdings = state["current_positions"]
        equity = float(state["cash"]) + sum(float(p["market_value"]) for p in holdings.values())
        if not math.isclose(equity, float(state["total_capital"]), abs_tol=1e-5, rel_tol=0):
            raise ValueError(f"Cash plus holdings != equity: {arm}")
        buys = {(t["stock_code"], t["date"]): t for t in state["trade_history"] if t["action"] == "BUY"}
        sells = [t for t in state["trade_history"] if str(t["action"]).startswith("SELL")]
        closed_costs, fees = {}, 0.
        for t in state["trade_history"]:
            if t["action"] == "BUY":
                fees += float(t["position_size"]) - float(t["entry_price"]) * float(t["shares"])
            else:
                key = (t["stock_code"], t["entry_date"])
                if key not in buys:
                    raise ValueError("Exit has no entry")
                closed_costs[key] = closed_costs.get(key, 0.) + float(t["position_size"])
                fees += float(t["exit_price"]) * float(t["shares"]) - float(t["position_size"]) - float(t["pnl"])
        for key, cost in closed_costs.items():
            if key[0] not in holdings and not math.isclose(cost, float(buys[key]["position_size"]), abs_tol=1e-5, rel_tol=0):
                raise ValueError(f"Exit cost basis differs from original entry: {arm}/{key}")
        pnl = sum(float(t["pnl"]) for t in sells)
        if not holdings and not math.isclose(initial + pnl, equity, abs_tol=1e-5, rel_tol=0):
            raise ValueError("Flat-account PnL does not reconcile to equity")
        if not math.isclose(fees, report["summary"]["fees"], abs_tol=1e-5, rel_tol=0):
            raise ValueError("Fees do not reconcile")
        nav = state["daily_nav"]
        exposures = [float(r["position_value"]) / float(r["total_value"]) for r in nav]
        groups = {}
        for t in sells:
            key = t["strategy_id"]
            bucket = groups.setdefault(key, {"closed": 0, "net_pnl": 0., "wins": 0})
            bucket["closed"] += 1
            bucket["net_pnl"] += float(t["pnl"])
            bucket["wins"] += float(t["pnl"]) > 0
        result[arm] = {"equity": equity, "net_closed_pnl": pnl, "fees": fees,
            "closed_trades": len(sells), "open_positions": len(holdings), "days": len(nav),
            "mean_close_exposure_pct": sum(exposures) / len(exposures) * 100,
            "max_close_exposure_pct": max(exposures) * 100,
            "strategy_metrics": groups, "cash_cost_and_pnl_reconciled": True}
        signals[arm] = {(r["date"], r["stock_code"]):
            (r.get("entry_time"), r.get("confirm_time"), float(r.get("entry_price") or 0), r.get("entry_mode"))
            for r in state["entry_attempts"] if r.get("status") == "filled"}
        plans[arm] = digest({p.name: p.read_bytes().hex() for p in (directory / "plans" / arm).glob("*.csv")})
    if len(set(plans.values())) != 1:
        raise ValueError("Candidate plans differ across sizing arms")
    parity = []
    for arm in ("fixed_risk", "tiered_risk"):
        shared = signals["baseline"].keys() & signals[arm].keys()
        mismatches = [key for key in shared if signals["baseline"][key] != signals[arm][key]]
        if mismatches:
            raise ValueError(f"Sizing changed common signal timing or price: {mismatches}")
        parity.append({"arm": arm, "shared_fillable_signals": len(shared), "timing_price_mode_mismatches": 0})
    return {"results": result, "common_signal_parity": parity,
            "identical_candidate_plans": True, "release_allowed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    service = ReplayResearch()
    service.verify(summary["experiment_id"])
    output = audit(service._directory(summary["experiment_id"]), summary)
    directory = service._directory(summary['experiment_id'])
    evidence = FrozenEvidence(directory)
    for arm, report in summary['results'].items():
        state = json.loads((directory / report['state_path']).read_text(encoding='utf-8'))['state']
        output['results'][arm]['independently_reconciled_daily_closes'] = reconcile_daily_marks(state, evidence)
    atomic_json(args.summary.parent / "independent_audit.json", output)
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
