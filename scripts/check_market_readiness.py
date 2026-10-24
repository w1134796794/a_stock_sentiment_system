"""Daily read-only readiness checks for live strategy and archived replay."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    from config.settings import (
        ELTDX_BOARD_SHADOW_ENABLED,
        HOLDING_DB_PATH,
        REALTIME_QUOTE_STALE_SECONDS,
        REDIS_KEY_PREFIX,
        REDIS_URL,
        WEB_DATA_DIR,
    )
    from core.infrastructure.atomic_json import atomic_write_json
    from core.realtime.market_readiness import AuditLimits, RedisAuditReader, audit_market, calendar_day, phase_for
    from core.realtime.trading_session import shanghai_now

    parser = argparse.ArgumentParser(description="只读检查真实交易日的数据就绪度；不补数、不运行策略、不发送通知")
    parser.add_argument("--date", help="YYYYMMDD; defaults to today's Shanghai date")
    parser.add_argument("--phase", choices=("auto", "preopen", "live", "break", "postclose"), default="auto")
    parser.add_argument("--data-root", type=Path, default=WEB_DATA_DIR)
    parser.add_argument("--ledger", type=Path, default=HOLDING_DB_PATH)
    parser.add_argument("--calendar", type=Path, default=ROOT / "data/trade_calendar.csv")
    parser.add_argument("--output-dir", type=Path, help="Reports are written only to this optional directory")
    parser.add_argument("--no-systemd", action="store_true", help="Do not read Linux service states")
    parser.add_argument("--offline", action="store_true", help="Historical replay checks only; do not connect to Redis")
    parser.add_argument(
        "--board-shadow",
        action="store_true",
        default=ELTDX_BOARD_SHADOW_ENABLED,
        help="Also audit optional TDX shadow evidence (never a formal gate)",
    )
    parser.add_argument("--snapshot-gap", type=float, default=8.0)
    parser.add_argument("--quote-age", type=float, default=REALTIME_QUOTE_STALE_SECONDS)
    parser.add_argument("--archive-lag", type=float, default=30.0)
    parser.add_argument("--window-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    if not all(
        math.isfinite(v) and v > 0 for v in (args.snapshot_gap, args.quote_age, args.archive_lag, args.window_seconds)
    ):
        parser.error("Thresholds must be positive")
    now = shanghai_now()
    date = args.date or now.strftime("%Y%m%d")
    try:
        if len(date) != 8 or not date.isdigit() or datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d") != date:
            raise ValueError("Invalid date")
    except ValueError:
        parser.error("--date must be a real YYYYMMDD date")
    phase = phase_for(date, now) if args.phase == "auto" else args.phase
    if args.offline and phase in {"preopen", "live"}:
        parser.error("--offline requires a break or postclose phase; it cannot certify live readiness")
    limits = replace(
        AuditLimits(),
        snapshot_gap=args.snapshot_gap,
        quote_age=args.quote_age,
        archive_lag=args.archive_lag,
        window_seconds=args.window_seconds,
    )
    live, states = None, None
    try:
        try:
            trading, _ = calendar_day(args.calendar, date)
        except (OSError, ValueError, KeyError):
            trading = False
        if trading and phase in {"preopen", "live"}:
            if REDIS_URL:
                try:
                    live = RedisAuditReader(REDIS_URL, REDIS_KEY_PREFIX)
                except Exception:
                    pass  # Report only availability; never expose connection credentials.
            if sys.platform == "linux" and not args.no_systemd:
                states = {}
                for unit in ("a-stock-collector", "a-stock-archiver", "a-stock-strategy"):
                    try:
                        result = subprocess.run(
                            ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5, check=False
                        )
                        states[unit] = result.stdout.strip() or "unknown"
                    except (OSError, subprocess.TimeoutExpired):
                        states[unit] = "unknown"
        report = audit_market(
            root=args.data_root,
            calendar_path=args.calendar,
            ledger_path=args.ledger,
            date=date,
            now=now,
            phase=args.phase,
            live=live,
            limits=limits,
            service_states=states,
            board_shadow=args.board_shadow,
        )
    except (ValueError, TypeError, OSError, KeyError) as exc:
        report = {
            "classification": "market_data_readiness_v1",
            "market_date": date,
            "phase": phase,
            "generated_at": now.isoformat(),
            "status": "not_ready",
            "live_ready": None,
            "replay_ready": None,
            "whole_day_replay_ready": None,
            "read_only": True,
            "remote_market_fetch": False,
            "notification_sent": False,
            "account_modified": False,
            "checks": [{"key": "audit_error", "status": "fail", "scope": "both", "reason": str(exc)}],
        }
    finally:
        if live is not None:
            live.close()
    if args.output_dir:
        directory = args.output_dir / date
        filename = now.strftime("%H%M%S") + f"-{report['phase']}.json"
        atomic_write_json(directory / filename, report, indent=2)
        atomic_write_json(args.output_dir / "latest.json", report, indent=2)
        lines = [
            f"# {date} {report['phase']} {report['status']}",
            "",
            f"live_ready={report['live_ready']} replay_ready={report['replay_ready']}",
            "",
        ]
        lines.extend(f"- [{check['status']}] {check['key']}: {check['reason']}" for check in report["checks"])
        if report.get("board_shadow"):
            shadow = report["board_shadow"]
            lines.append(f"- [shadow] ready={shadow['ready']}: {shadow['reason']}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename.replace(".json", ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 2 if report["status"] == "not_ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
