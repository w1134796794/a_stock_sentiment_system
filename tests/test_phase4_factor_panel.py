"""指标因子面板测试。"""
import pytest

from config import overrides as ov


@pytest.fixture
def overrides_sandbox():
    """快照覆盖文件，测试后精确还原并重载 config_loader / 因子注册中心。"""
    from config.config_loader import get_config_loader
    from core.factors.factor_registry import get_factor_registry

    snapshot = ov.load_overrides()
    try:
        yield
    finally:
        ov.save_overrides(snapshot)
        try:
            get_config_loader().reload_config()
            get_factor_registry().reload()
        except Exception:
            pass


def test_factor_registry_honors_yaml_override(overrides_sandbox):
    from config.config_loader import get_config_loader
    from core.factors.factor_registry import get_factor_registry

    reg = get_factor_registry()
    reg.reload()
    # 取一个当前启用的因子
    fid = next((f.factor_id for f in reg._factors.values() if f.enabled), None)
    assert fid is not None, "没有启用中的因子可供测试"

    # 写入禁用覆盖 → 重载 → 应禁用
    ov.set_override("yaml", f"factor_registry.factors.{fid}.enabled", False)
    get_config_loader().reload_config()
    reg.reload()
    assert reg.get_factor(fid).enabled is False

    # 清除覆盖 → 重载 → 恢复启用
    ov.clear_override("yaml", f"factor_registry.factors.{fid}.enabled")
    get_config_loader().reload_config()
    reg.reload()
    assert reg.get_factor(fid).enabled is True


def test_build_factor_state_structure():
    from web.factor_panel import build_factor_state

    st = build_factor_state()
    assert st["factor_total"] > 0
    assert st["factor_groups"], "因子分组为空"
    assert st["enabled_factor_list"], "启用指标列表为空"
    assert "default" in st["profile_names"]
    assert "strategies" not in st
    assert "param_groups" not in st
    assert "confidence_modes" not in st
