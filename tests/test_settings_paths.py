import os
import subprocess
import sys
from pathlib import Path


def test_cache_dir_can_be_configured_from_environment(tmp_path):
    root = Path(__file__).resolve().parents[1]
    expected = (tmp_path / "runtime-cache").resolve()
    env = os.environ.copy()
    env["CACHE_DIR"] = str(expected)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config.settings import CACHE_DIR; print(CACHE_DIR)",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == expected
