"""Inspect durable live evidence without fetching, cleaning or running a strategy."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from core.realtime.persistent_market import PersistentMarketReader
    from core.realtime.trading_session import shanghai_now
    parser = argparse.ArgumentParser(description="只读查询持久化盘中行情")
    parser.add_argument("--date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--codes", required=True, help="逗号分隔股票代码")
    parser.add_argument("--kind", choices=("snapshots", "minutes", "sector_minutes"), default="snapshots")
    parser.add_argument("--start", default="09:30:00")
    parser.add_argument("--end", default="15:00:00")
    parser.add_argument("--asof", help="上海时间HH:MM:SS；限制当时已收到并持久化的数据")
    parser.add_argument("--root", type=Path, help="market_archive目录")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("--limit must be 1..10000")
    reader = PersistentMarketReader(args.root)
    asof = shanghai_now(datetime.strptime(args.date+args.asof, "%Y%m%d%H:%M:%S")) if args.asof else shanghai_now()
    view = reader.view(args.date, asof=asof)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    if args.kind == "snapshots":
        rows = view.snapshots(codes, start=args.start, end=args.end)
    elif args.kind == "minutes":
        rows = [dict(row, code=code) for code in codes for row in view.minutes(code, start=args.start, end=args.end).to_dict("records")]
    else:
        start = datetime.strptime(args.start, "%H:%M:%S")
        end = datetime.strptime(args.end, "%H:%M:%S")
        from datetime import timedelta
        minutes = []
        while start <= end:
            minutes.append(start.strftime("%H:%M:%S"))
            start += timedelta(minutes=1)
        points = view.sector_points(codes, minutes=minutes)
        rows = [row for values in points.values() for row in values.values()]
    print(json.dumps({"market_date": args.date, "evaluation_asof": view.asof.isoformat(), "watermark": view.watermark,
                      "total": len(rows), "rows": rows[-args.limit:], "reads": view.audit},
                     ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
