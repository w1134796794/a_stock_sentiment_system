"""Run the heavy daily automation pipeline in an isolated process."""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import signal
import subprocess
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
faulthandler.enable(all_threads=True)


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _exit_reason(returncode: int) -> str:
    if returncode >= 0:
        return f"自动回测子进程退出码 {returncode}"
    try:
        signal_name = signal.Signals(-returncode).name
    except ValueError:
        signal_name = f"SIGNAL_{-returncode}"
    return f"自动回测子进程收到 {signal_name}（{-returncode}）"


def _run_auto_backtest(trade_date: str, capital: float, result_path: Path) -> Dict[str, Any]:
    backtest_result = result_path.with_name(f"{result_path.stem}_backtest.json")
    backtest_result.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-X",
        "faulthandler",
        str(ROOT / "scripts" / "automation_backtest_worker.py"),
        "--date",
        trade_date,
        "--capital",
        str(capital),
        "--result",
        str(backtest_result),
    ]
    timeout = max(60, int(os.getenv("AUTOMATION_BACKTEST_TIMEOUT", "3600") or 3600))
    try:
        completed = subprocess.run(command, cwd=str(ROOT), timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"自动回测超过{timeout}秒，已终止"}
    payload: Dict[str, Any] = {}
    if backtest_result.exists():
        try:
            payload = json.loads(backtest_result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload = {"ok": False, "reason": f"自动回测结果读取失败：{exc}"}
    if completed.returncode != 0:
        payload.update({
            "ok": False,
            "reason": str(payload.get("reason") or _exit_reason(completed.returncode)),
            "worker_exit_code": completed.returncode,
        })
    return payload or {"ok": False, "reason": "自动回测未生成结果"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--auto-backtest", action="store_true")
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help="Preserve successful post-close caches and retry only missing sources.",
    )
    args = parser.parse_args()
    trade_date = str(args.date)
    started_at = datetime.now().isoformat(timespec="seconds")
    progress: Dict[str, Any] = {
        "status": "running", "job": "daily", "trade_date": trade_date,
        "started_at": started_at, "stage": "starting", "stages": {},
    }
    _write(args.result, progress)
    try:
        from core.reports.daily_journal import DailyJournalService
        from main import SentimentSystem

        system = SentimentSystem()
        # The scheduled job is the authoritative post-close run. Force-refresh
        # current-date caches so an early/manual partial fetch cannot poison all
        # later retries with a missing Silver partition.
        fetched = system.fetch_post_close_data(
            trade_date,
            skip_existing=False,
            force_refresh=not args.repair_existing,
        )
        progress["stages"]["fetch"] = bool(fetched.ok)
        if not fetched.ok:
            missing = (fetched.silver_summary or {}).get("missing") or []
            raise RuntimeError(
                "盘后取数未生成因子必需的 Silver 数据"
                + (f"：{', '.join(map(str, missing))}" if missing else "")
            )
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
            progress["stage"] = "backtest"
            progress["heartbeat_at"] = datetime.now().isoformat(timespec="seconds")
            _write(args.result, progress)
            backtest = _run_auto_backtest(trade_date, args.capital, args.result)
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
