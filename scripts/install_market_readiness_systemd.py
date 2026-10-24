"""Install an opt-in daily Linux evidence-audit timer, never trading workers."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.install_market_systemd import systemd_path

SERVICE = "a-stock-readiness.service"
TIMER = "a-stock-readiness.timer"


def render_units(root, user):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*[$]?", user):
        raise ValueError("Invalid service user")
    root = str(PurePosixPath(root))
    systemd_path(root)
    service = f"""[Unit]
Description=A Stock read-only market evidence readiness audit
After=network-online.target

[Service]
Type=oneshot
User={user}
WorkingDirectory={systemd_path(root)}
EnvironmentFile={systemd_path(root + "/.env")}
Environment=PYTHONUNBUFFERED=1
ExecStart={systemd_path(root + "/.venv/bin/python")} -X utf8 {systemd_path(root + "/scripts/check_market_readiness.py")} --output-dir {systemd_path(root + "/outputs/market-readiness")}
TimeoutStartSec=600
Nice=10
IOSchedulingClass=idle
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal
"""
    timer = f"""[Unit]
Description=A Stock trading-day preopen, intraday and postclose data audits

[Timer]
OnCalendar=*-*-* 09:20:00 Asia/Shanghai
OnCalendar=*-*-* 09:35,40,45,50,55:20 Asia/Shanghai
OnCalendar=*-*-* 10:00/5:20 Asia/Shanghai
OnCalendar=*-*-* 11:00,05,10,15,20,25:20 Asia/Shanghai
OnCalendar=*-*-* 13:05,10,15,20,25,30,35,40,45,50,55:20 Asia/Shanghai
OnCalendar=*-*-* 14:00/5:20 Asia/Shanghai
OnCalendar=*-*-* 15:15:00 Asia/Shanghai
AccuracySec=1s
RandomizedDelaySec=0
Persistent=false
Unit={SERVICE}

[Install]
WantedBy=timers.target
"""
    return {SERVICE: service, TIMER: timer}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/readiness-systemd")
    parser.add_argument("--install", action="store_true", help="Linux root only; enable timer but do not start it")
    args = parser.parse_args(argv)
    units = render_units(args.project_root, args.user)
    destination = args.output_dir
    if args.install:
        if sys.platform != "linux" or os.geteuid() != 0:
            parser.error("--install requires Linux root")
        import pwd

        pwd.getpwnam(args.user)
        root = Path(args.project_root)
        if (
            not (root / "scripts/check_market_readiness.py").is_file()
            or not (root / ".env").is_file()
            or not os.access(root / ".venv/bin/python", os.X_OK)
        ):
            parser.error("Existing project, .env and virtualenv Python are required")
        destination = Path("/etc/systemd/system")
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in units.items():
        path = destination / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o644)
        print(path)
    if args.install:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", TIMER], check=True)
        print(f"Enabled, not started. Start explicitly: sudo systemctl start {TIMER}")
    else:
        print("Preview only. No services installed or started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
