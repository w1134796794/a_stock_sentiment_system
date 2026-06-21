"""一键打包桌面管理工具为 exe（PyInstaller，单目录产物）。

    python scripts/build_manager.py

前置：
    pip install -r requirements.txt
    pip install pyinstaller

产物：
    dist/A股情绪系统管理工具/A股情绪系统管理工具.exe   （连同同目录 _internal 一起分发）
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "manager.spec"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先运行：pip install pyinstaller", file=sys.stderr)
        return 2

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
    ]
    print("运行:", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode == 0:
        print("\n[OK] 打包完成 -> dist/A股情绪系统管理工具/")
        print("将该目录放到项目根目录旁（与 webdata/ logs/ output/ 同级）后运行其中的 exe。")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
