"""Install pressure-triggered storage maintenance; installation does not run cleanup."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.install_market_systemd import systemd_path


def render_units(root, data_root, user):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", user) or user == "root":
        raise ValueError("Choose a non-root application user")
    project, data = systemd_path(root), systemd_path(data_root)
    python = systemd_path(root + "/.venv/bin/python")
    script = systemd_path(root + "/scripts/maintain_storage.py")
    return {
        "a-stock-storage-cleanup.service": f"""[Unit]
Description=A Stock pressure-triggered diagnostic retention
After=local-fs.target

[Service]
Type=oneshot
User=root
WorkingDirectory={project}
ExecStart={python} -X utf8 {script} --root {data} --user {user} --threshold 70 --minimum-free-gib 4 --keep-days 7 --apply
TimeoutStartSec=infinity
Nice=15
IOSchedulingClass=idle
UMask=0077
StandardOutput=journal
StandardError=journal
""",
        "a-stock-storage-cleanup.timer": """[Unit]
Description=Check A Stock storage pressure every 30 minutes

[Timer]
OnCalendar=*-*-* *:00,30:00 Asia/Shanghai
AccuracySec=30s
RandomizedDelaySec=30
Persistent=true
Unit=a-stock-storage-cleanup.service

[Install]
WantedBy=timers.target
""",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-root", default="/opt/a_stock_sentiment_system")
    p.add_argument("--data-root", default="/srv/a-stock/webdata")
    p.add_argument("--user", default="deploy")
    p.add_argument("--output-dir", type=Path, default=ROOT / "outputs/storage-cleanup-systemd")
    p.add_argument("--install", action="store_true")
    args = p.parse_args()
    units = render_units(args.project_root, args.data_root, args.user)
    dest = args.output_dir
    if args.install:
        if sys.platform != "linux" or os.geteuid() != 0:
            p.error("Installation requires Linux root")
        import pwd
        if pwd.getpwnam(args.user).pw_uid == 0:
            p.error("Application user must be non-root")
        project = Path(args.project_root)
        if not (project / "scripts/maintain_storage.py").is_file() or not os.access(project / ".venv/bin/python", os.X_OK):
            p.error("Deploy code and existing virtualenv first")
        if not Path(args.data_root).is_dir() or Path(args.data_root).resolve() != Path(args.data_root):
            p.error("Data directory missing or symlinked")
        dest = Path("/etc/systemd/system")
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in units.items():
        path = dest / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.chmod(0o644)
        tmp.replace(path)
        print(path)
    if args.install:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "a-stock-storage-cleanup.timer"], check=True)
        print("Installed and enabled for boot; start timer explicitly when ready")


if __name__ == "__main__":
    main()
