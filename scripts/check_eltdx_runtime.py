"""Offline SDK/ABI probe: no market requests, Redis access or notifications."""
from __future__ import annotations

import importlib.metadata
import json


def probe():
    from eltdx import TdxClient
    client = TdxClient.in_memory()
    try:
        required = {
            "quotes.get_snapshots": client.quotes.get_snapshots,
            "quotes.get_depth": client.quotes.get_depth,
            "trades.today": client.trades.today,
            "trades.history": client.trades.history,
            "trades.opening_match_today": client.trades.opening_match_today,
            "trades.opening_match_history": client.trades.opening_match_history,
            "minutes.today": client.minutes.today,
            "minutes.history": client.minutes.history,
            "bars.get": client.bars.get,
            "corporate.capital_changes": client.corporate.capital_changes,
            "corporate.adjustment_factors": client.corporate.adjustment_factors,
            "money_flow.daily": client.money_flow.daily,
            "helpers.latest_stock_list": client.helpers.latest_stock_list,
            "helpers.latest_st": client.helpers.latest_st,
            "helpers.latest_suspended": client.helpers.latest_suspended,
            "auctions.series": client.auctions.series,
            "helpers.auction_data": client.helpers.auction_data,
            "helpers.board_quotes": client.helpers.board_quotes,
            "helpers.board_member_quotes": client.helpers.board_member_quotes,
        }
        checks = {name: callable(method) for name, method in required.items()}
        # Exercise Python/native argument dispatch using only the memory transport.
        dispatched = client.minutes.today("sz000001")
        checks["native_memory_dispatch"] = dispatched.get("status") == "memory_transport"
        return {"ok": all(checks.values()), "version": importlib.metadata.version("eltdx"),
                "checks": checks, "network_accessed": False, "live_data_verified": False}
    finally:
        client.close()


if __name__ == "__main__":
    try:
        report = probe()
    except Exception as exc:
        report = {"ok": False, "error_type": type(exc).__name__, "reason": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
