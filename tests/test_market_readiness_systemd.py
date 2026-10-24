import getpass
import shutil
import sys

import pytest

from scripts.install_market_readiness_systemd import SERVICE, render_units, verify_units


@pytest.mark.parametrize("root", ["/opt/a_stock_sentiment_system", "/opt/a stock"])
def test_working_directory_is_a_literal_path_not_a_shell_argument(root):
    service = render_units(root, "deploy")[SERVICE]
    assert f"WorkingDirectory={root}\n" in service
    assert f'WorkingDirectory="{root}"' not in service
    assert f"EnvironmentFile={root}/.env\n" in service
    assert f'ExecStart="{root}/.venv/bin/python"' in service


def test_unit_verification_rejects_ignored_directive_warnings(monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setattr(
        "scripts.install_market_readiness_systemd.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "", "EnvironmentFile= path is not absolute, ignoring"),
    )
    with pytest.raises(RuntimeError, match="ignoring"):
        verify_units(render_units("/opt/a-stock", "deploy"))


@pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("systemd-analyze"), reason="Requires Linux systemd parser"
)
def test_complete_units_pass_the_real_linux_parser(tmp_path):
    root = tmp_path / "project root"
    binary = root / ".venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    (root / ".env").touch()
    verify_units(render_units(str(root), getpass.getuser()))
