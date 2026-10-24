"""Opt-in post-close child process. Failure cannot fail the production pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from core.realtime.trading_session import shanghai_now


def run_optional_research(trade_date, *, now=None):
    if os.getenv("ELTDX_RESEARCH_ENABLED", "false").lower() not in {"true", "1", "yes"}:
        return dict(status="disabled", production_pipeline_affected=False)
    clock = shanghai_now(now)
    if trade_date != clock.strftime("%Y%m%d") or clock.strftime("%H:%M:%S") < "16:00:00":
        return dict(
            status="skipped",
            reason="Only current post-close collection is scheduled",
            production_pipeline_affected=False,
        )
    from datetime import timedelta

    from config.settings import BASE_DIR, WEB_DATA_DIR

    start = (clock - timedelta(days=40)).strftime("%Y%m%d")
    command = [
        sys.executable,
        str(Path(BASE_DIR) / "scripts" / "prefetch_eltdx_research.py"),
        "--start",
        start,
        "--end",
        trade_date,
        "--collection-date",
        trade_date,
        "--data-root",
        str(WEB_DATA_DIR),
        "--kinds",
        "bars_raw,corporate_actions,money_flow,company_profile,announcements,finance_report,topics",
        "--apply",
    ]
    try:
        # Output remains in the existing supervised daily worker log; job details
        # are independently persisted by ResearchStore for the admin read API.
        process = subprocess.run(command, cwd=BASE_DIR, timeout=1800, check=False)
        return dict(
            status="done" if process.returncode == 0 else "partial",
            exit_code=process.returncode,
            production_pipeline_affected=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(status="failed", reason=type(exc).__name__, production_pipeline_affected=False)
