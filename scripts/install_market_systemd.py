"""Render or install Linux systemd units for the independent market pipeline."""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


TARGET = "a-stock-market.target"
WORKERS = {
    "a-stock-collector.service": ("Market quote collector", "run_eltdx_quote_collector.py"),
    "a-stock-archiver.service": ("Market incremental archiver", "run_market_archiver.py"),
    "a-stock-strategy.service": ("Read-only market strategy", "run_realtime_strategy.py"),
}


def systemd_path(value: str) -> str:
    # Refuse specifiers and environment expansion instead of interpreting caller input.
    if not PurePosixPath(value).is_absolute() or any(c in value for c in '\n\r\t"\\%$'):
        raise ValueError("Use an absolute Linux path without control characters, quotes, \\, %, or $")
    return f'"{value}"'


def render_units(project_root: str, user: str, *, python: str | None = None,
                 env_file: str | None = None, notifications: bool = False, paper_execution: bool = False) -> dict[str, str]:
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*[$]?", user):
        raise ValueError("Invalid Linux service user")
    root = str(PurePosixPath(project_root))
    systemd_path(root)
    interpreter = systemd_path(python or f"{root}/.venv/bin/python")
    environment_file = env_file or f"{root}/.env"
    systemd_path(environment_file)
    units = {}
    workers = dict(WORKERS)
    if paper_execution:
        workers["a-stock-execution.service"] = ("Paper execution consumer", "run_realtime_execution.py")
    for name, (description, script) in workers.items():
        command = f"{interpreter} -X utf8 {systemd_path(f'{root}/scripts/{script}')}"
        if name == "a-stock-strategy.service" and notifications:
            command += " --notifications"
        if name == "a-stock-execution.service":
            command += " --paper --monitor"
        units[name] = f"""[Unit]
Description=A Stock {description}
Wants=network-online.target
After=network-online.target redis.service redis-server.service
PartOf={TARGET}
StartLimitIntervalSec=0

[Service]
Type=simple
User={user}
WorkingDirectory={root}
EnvironmentFile={environment_file}
Environment=PYTHONUNBUFFERED=1
Environment=REALTIME_STRATEGY_EXTERNAL=true
Environment=ELTDX_EMBEDDED_COLLECTOR_ENABLED=false
ExecStart={command}
Restart=always
RestartSec=5
TimeoutStopSec=60
KillMode=control-group
UMask=0077
StandardOutput=journal
StandardError=journal
"""
    units[TARGET] = f"""[Unit]
Description=A Stock independent market pipeline
Wants={' '.join(workers)}
After=network-online.target

[Install]
WantedBy=multi-user.target
"""
    return units


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="Use the same Linux account as the Web service")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--python", help="Absolute path to the existing Linux virtualenv Python")
    parser.add_argument("--env-file", help="Shared systemd-compatible environment file; defaults to PROJECT/.env")
    parser.add_argument("--notifications", action="store_true", help="Explicitly enable real notification delivery")
    parser.add_argument("--paper-execution", action="store_true", help="Install the separate paper execution consumer")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/systemd"))
    parser.add_argument("--install", action="store_true", help="Linux root only: install and enable, but do not start")
    args = parser.parse_args(argv)
    try:
        units = render_units(args.project_root, args.user, python=args.python,
                             env_file=args.env_file, notifications=args.notifications, paper_execution=args.paper_execution)
    except ValueError as exc:
        parser.error(str(exc))
    if args.install:
        if sys.platform != "linux" or os.geteuid() != 0:
            parser.error("--install requires Linux root; use sudo")
        import pwd
        pwd.getpwnam(args.user)
        root = Path(args.project_root)
        interpreter = Path(args.python or root / ".venv/bin/python")
        env_file = Path(args.env_file or root / ".env")
        if not os.access(interpreter, os.X_OK) or not env_file.is_file():
            parser.error("Existing executable virtualenv Python and shared environment file are required")
        for _, script in WORKERS.values():
            if not (root / "scripts" / script).is_file():
                parser.error(f"Missing worker script: {script}")
        destination = Path("/etc/systemd/system")
    else:
        destination = args.output_dir
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in units.items():
        path = destination / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(source, encoding="utf-8", newline="\n")
        temporary.chmod(0o644)
        temporary.replace(path)
        print(path)
    if args.install:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", TARGET], check=True)
        print(f"Enabled for boot. Start or restart explicitly: sudo systemctl restart {TARGET}")
    else:
        print("Preview only; no services installed or started.")
    print(f"Real notifications: {'enabled' if args.notifications else 'disabled'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
