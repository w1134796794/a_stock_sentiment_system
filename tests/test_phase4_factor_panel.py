"""
Phase 4 因子面板：
- 4 个策略组均带 confidence_mode 默认（legacy）；
- FactorRegistry 经 config_loader 读取，网页对 factor_registry.yaml 的开关覆盖能生效；
- build_factor_state 结构完整；
- FACTOR_PROFILE_OVERRIDE 非空时，layer3 强制使用该 profile（压过当日情绪周期）。

注意：涉及覆盖文件的测试用 fixture 快照 + 还原，避免污染 webdata/config_overrides.json。
"""
from types import SimpleNamespace

import pytest

from config import overrides as ov
from config import pattern_params as pp


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


def test_confidence_mode_defaults_present():
    for grp in ["second_board_dragon", "weak_to_strong",
                "first_board_breakout", "dragon_second_wave"]:
        assert pp.get_default_params(grp).get("confidence_mode") == "legacy", grp


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
    # 策略含 4 组且都有 mode
    groups = {s["group"] for s in st["strategies"]}
    assert {"second_board_dragon", "weak_to_strong",
            "first_board_breakout", "dragon_second_wave"} <= groups
    for s in st["strategies"]:
        assert s["mode"] in ("legacy", "deduction")
    # profile 含 default
    assert "default" in st["profile_names"]
    assert st["confidence_modes"] == ["legacy", "deduction"]


def test_param_groups_exposed_and_tunable(overrides_sandbox):
    """Phase 6：生命周期/仲裁参数组在面板暴露，且经 patterns 覆盖即时生效。"""
    from config.config_loader import get_config_loader
    from web.factor_panel import build_factor_state

    st = build_factor_state()
    groups = {g["group"] for g in st["param_groups"]}
    assert {"dragon_lifecycle", "arbitration"} <= groups
    # 标量参数被暴露、嵌套 dict（emotion_routing）被跳过
    arb = next(g for g in st["param_groups"] if g["group"] == "arbitration")
    keys = {p["key"] for p in arb["params"]}
    assert "mode" in keys and "resonance_bonus" in keys
    assert "emotion_routing" not in keys  # 嵌套 dict 不在标量编辑面板

    # 覆盖 dragon_lifecycle.wts_max_watch_days → get_params 生效
    ov.set_override("patterns", "dragon_lifecycle.wts_max_watch_days", 8)
    get_config_loader().reload_config()
    assert pp.get_params("dragon_lifecycle")["wts_max_watch_days"] == 8
    ov.clear_override("patterns", "dragon_lifecycle.wts_max_watch_days")
    get_config_loader().reload_config()
    assert pp.get_params("dragon_lifecycle")["wts_max_watch_days"] == 5


def test_forced_profile_overrides_cycle(monkeypatch):
    """FACTOR_PROFILE_OVERRIDE 非空时压过当日情绪周期。"""
    import config.settings as settings
    from core.factors.factor_registry import get_factor_registry
    from core.pipeline.layer3_stock_selection import StockSelectionLayer

    reg = get_factor_registry()
    reg.reload()

    lyr = StockSelectionLayer.__new__(StockSelectionLayer)
    lyr._factor_registry = reg

    # 不强制：emotion_cycle="上升期"（无对应 profile）→ 回退 default
    monkeypatch.setattr(settings, "FACTOR_PROFILE_OVERRIDE", "", raising=False)
    res = SimpleNamespace(emotion_cycle="上升期", factor_profile="", enabled_factors=[])
    lyr._apply_factor_profile(res)
    assert res.factor_profile == "default"

    # 强制 退潮期 → 即使周期是上升期，也应使用退潮期 profile
    monkeypatch.setattr(settings, "FACTOR_PROFILE_OVERRIDE", "退潮期", raising=False)
    res2 = SimpleNamespace(emotion_cycle="上升期", factor_profile="", enabled_factors=[])
    lyr._apply_factor_profile(res2)
    assert res2.factor_profile == "退潮期"
