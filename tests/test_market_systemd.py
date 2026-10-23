from pathlib import Path

import pytest

from scripts.install_market_systemd import TARGET, WORKERS, main, render_units


def test_units_supervise_independent_workers():
    units = render_units("/opt/a-stock", "astock")
    assert set(units) == {TARGET, *WORKERS}
    assert "WantedBy=multi-user.target" in units[TARGET]
    assert "Wants=" + " ".join(WORKERS) in units[TARGET]
    for name, (_, script) in WORKERS.items():
        unit = units[name]
        assert f'ExecStart="/opt/a-stock/.venv/bin/python" -X utf8 "/opt/a-stock/scripts/{script}"' in unit
        assert "User=astock\n" in unit
        assert 'EnvironmentFile=/opt/a-stock/.env\n' in unit
        assert "Restart=always\n" in unit
        assert f"PartOf={TARGET}\n" in unit
        assert "KillMode=control-group\n" in unit
        assert "--notifications" not in unit
        assert "run_realtime_execution.py" not in unit


def test_notification_and_custom_paths_are_explicit():
    units = render_units("/srv/stock app", "worker", python="/srv/venv/bin/python",
                         env_file="/etc/a-stock/shared.env", notifications=True)
    assert '--notifications\n' in units["a-stock-strategy.service"]
    assert all("--notifications" not in units[name] for name in WORKERS if "strategy" not in name)
    assert 'WorkingDirectory=/srv/stock app\n' in units["a-stock-strategy.service"]
    assert 'EnvironmentFile=/etc/a-stock/shared.env\n' in units["a-stock-strategy.service"]


@pytest.mark.parametrize("root", ["relative", "/opt/%n", "/opt/$USER", "/opt/a\nUser=root", '/opt/a"b'])
def test_unsafe_paths_rejected(root):
    with pytest.raises(ValueError):
        render_units(root, "astock")


def test_invalid_user_rejected():
    with pytest.raises(ValueError):
        render_units("/opt/a-stock", "astock\nUser=root")


def test_preview_has_no_systemctl_side_effects(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.install_market_systemd.subprocess.run",
                        lambda *a, **k: pytest.fail("preview must not control services"))
    assert main(["--project-root", "/opt/a-stock", "--user", "astock",
                 "--output-dir", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.iterdir()} == {TARGET, *WORKERS}
    assert (tmp_path / TARGET).read_bytes().endswith(b"\n")


def test_deployment_restarts_market_only_when_installed():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "systemctl is-enabled --quiet a-stock-market.target" in workflow
    assert "sudo systemctl restart a-stock-market.target" in workflow
