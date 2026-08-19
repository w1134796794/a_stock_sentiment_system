"""Run the optional automatic backtest outside the daily pipeline process."""
from __future__ import annotations

import argparse
import faulthandler
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

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_name, "1")
os.environ.setdefault("DUCKDB_THREADS", "1")
faulthandler.enable(all_threads=True)


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    try:
        from core.reports.auto_backtest import AutoBacktestReportService

        payload = dict(AutoBacktestReportService().run(args.date, capital=args.capital) or {})
        payload.setdefault("ok", True)
        payload["worker_finished_at"] = datetime.now().isoformat(timespec="seconds")
        _write(args.result, payload)
        return 0 if payload.get("ok") else 2
    except Exception as exc:  # noqa: BLE001
        _write(args.result, {
            "ok": False,
            "reason": str(exc),
            "traceback": traceback.format_exc(),
            "worker_finished_at": datetime.now().isoformat(timespec="seconds"),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
