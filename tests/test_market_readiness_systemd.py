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


HOST_WARNINGS = (
    "/lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' in section 'Service', ignoring.\n"
    "/etc/systemd/system/cloudmonitor.service:8: Unit configured to use KillMode=none. This is unsafe.\n"
    "/etc/systemd/system/cloudmonitor.service:9: PIDFile= references a path below legacy directory /var/run/.\n"
)


def test_unrelated_host_warnings_do_not_block_valid_units(monkeypatch, capsys):
    from subprocess import CompletedProcess

    monkeypatch.setattr(
        "scripts.install_market_readiness_systemd.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "", HOST_WARNINGS),
    )
    verify_units(render_units("/opt/a-stock", "deploy"))
    assert "Unrelated host unit warnings" in capsys.readouterr().err


@pytest.mark.parametrize(
    "code,diagnostic",
    [
        (1, HOST_WARNINGS),
        (
            0,
            HOST_WARNINGS
            + "/tmp/a-stock-readiness-x/a-stock-readiness.service:8: WorkingDirectory= path is not absolute",
        ),
        (0, HOST_WARNINGS + "/etc/systemd/system/a-stock-readiness.service:8: WorkingDirectory= path is not absolute"),
        (0, HOST_WARNINGS + "/etc/systemd/system/a-stock-readiness.service.d/override.conf:2: Unknown key name"),
        (0, HOST_WARNINGS + "Unknown unclassified verifier warning"),
    ],
)
def test_host_noise_never_hides_own_errors_or_nonzero_exit(monkeypatch, code, diagnostic):
    from subprocess import CompletedProcess

    monkeypatch.setattr(
        "scripts.install_market_readiness_systemd.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], code, "", diagnostic),
    )
    with pytest.raises(RuntimeError, match="Readiness unit verification failed"):
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
