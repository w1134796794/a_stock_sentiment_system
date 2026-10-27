"""Read unified strategy diagnostics without starting a strategy or fetching data."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from config.settings import WEB_DATA_DIR
    from core.realtime.trading_session import shanghai_now

    parser = argparse.ArgumentParser(description="查询统一盘中debug日志；默认显示最后50条")
    parser.add_argument("--date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--code", default="")
    parser.add_argument("--stage", choices=("evaluation", "health_gate", "error", "cycle"))
    parser.add_argument("--since", help="上海时间 HH:MM:SS，含起点")
    parser.add_argument("--until", help="上海时间 HH:MM:SS，含终点")
    parser.add_argument("--reason", default="", help="原因文本子串")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--root", type=Path, default=Path(WEB_DATA_DIR) / "logs" / "intraday_debug")
    args = parser.parse_args()
    date = datetime.strptime(args.date, "%Y%m%d")
    if not 1 <= args.limit <= 10000:
        parser.error("--limit must be 1..10000")
    clauses, params = [], []
    for field, value in (("code", args.code), ("stage", args.stage)):
        if value:
            clauses.append(field + "=?")
            params.append(value)
    for option, op in ((args.since, ">="), (args.until, "<=")):
        if option:
            datetime.strptime(option, "%H:%M:%S")
            clauses.append("substr(recorded_at,1,19)" + op + "?")
            params.append(date.strftime("%Y-%m-%dT") + option)
    if args.reason:
        clauses.append("instr(reason,?)>0")
        params.append(args.reason)
    path = args.root / f"{args.date}.sqlite3"
    if not path.is_file():
        print(f"debug日志不存在：{path}")
        return 1
    query = "SELECT payload_zlib FROM debug_events"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        rows = con.execute(query, [*params, args.limit]).fetchall()
    for raw, in reversed(rows):
        print(json.dumps(json.loads(zlib.decompress(raw)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
