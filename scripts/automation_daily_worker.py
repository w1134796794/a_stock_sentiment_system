"""Run the heavy daily automation pipeline in an isolated process."""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep native numerical libraries from spawning one thread per core inside the
# isolated worker. This materially lowers peak memory on 2C/4G deployments.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_name, "1")
os.environ.setdefault("DUCKDB_THREADS", "1")


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--auto-backtest", action="store_true")
    args = parser.parse_args()
    trade_date = str(args.date)
    started_at = datetime.now().isoformat(timespec="seconds")
    progress: Dict[str, Any] = {
        "status": "running", "job": "daily", "trade_date": trade_date,
        "started_at": started_at, "stage": "starting", "stages": {},
    }
    _write(args.result, progress)
    try:
        from main import SentimentSystem
        from core.reports.daily_journal import DailyJournalService

        system = SentimentSystem()
        fetched = system.fetch_post_close_data(trade_date, skip_existing=True)
        progress["stages"]["fetch"] = bool(fetched.ok)
        progress["stage"] = "factors"
        progress["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
        _write(args.result, progress)
        factors = system.run_factor_calculation(trade_date)
        progress["stages"]["factors"] = bool(factors.ok)
        progress["stage"] = "screening"
        progress["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
        _write(args.result, progress)
        screening = system.run_screening_strategy(trade_date)
        progress["stages"]["screening"] = bool(screening.ok)
        progress["stage"] = "journal"
        progress["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
        _write(args.result, progress)
        journal = DailyJournalService().generate(trade_date, capital=args.capital)
        backtest: Dict[str, Any] = {"ok": False, "reason": "自动回测已关闭"}
        if args.auto_backtest:
            from core.reports.auto_backtest import AutoBacktestReportService

            backtest = AutoBacktestReportService().run(trade_date, capital=args.capital)
        payload = {
            "status": "done",
            "job": "daily",
            "trade_date": trade_date,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "pipeline_ok": bool(fetched.ok and factors.ok and screening.ok),
            "stages": {
                "fetch": bool(fetched.ok),
                "factors": bool(factors.ok),
                "screening": bool(screening.ok),
            },
            "journal": journal,
            "backtest": backtest,
        }
        _write(args.result, payload)
        return 0 if payload["pipeline_ok"] else 2
    except Exception as exc:  # noqa: BLE001
        _write(args.result, {
            "status": "error",
            "job": "daily",
            "trade_date": trade_date,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "message": str(exc),
            "traceback": traceback.format_exc(),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
