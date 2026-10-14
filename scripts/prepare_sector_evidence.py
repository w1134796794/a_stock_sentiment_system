"""Complete sector reference evidence before opening; never backdate a bundle."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.trade_calendar import TradeCalendar
from core.realtime.sector_evidence import prepare_preopen_sector_evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args()
    result = prepare_preopen_sector_evidence(TradeCalendar().prev(args.market_date), args.market_date,
                                             allow_remote=not args.cache_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
