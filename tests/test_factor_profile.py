"""
Phase 2 验收：情绪周期 profile 机制。

校验：
1. 默认所有 profile 的禁用集为空 → apply_profile 后全因子仍启用（行为不变）。
2. profile 注入禁用因子后，该因子被禁用；再次 apply 其它 profile 会**复位**
   （幂等，防回测多日单例状态污染，R5）。
3. apply_profile 未匹配周期时回退 default。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.factors.factor_registry import get_factor_registry
from core.factors import layer3_perstock as ps


@pytest.fixture
def registry():
    reg = get_factor_registry()
    reg.reload()  # 回到磁盘配置基线
    yield reg
    reg.reload()  # 清理，避免污染其它测试


def test_default_profile_keeps_all_enabled(registry):
    registry.apply_profile("震荡期")
    assert registry.get_active_profile() in ("震荡期", "default")
    # 逐股 D/E 因子默认应全部启用
    assert set(ps.active_stock_tech_factors(registry)) == set(ps.ALL_STOCK_TECH_IDS)
    assert set(ps.active_moneyflow_factors(registry)) == set(ps.ALL_MONEYFLOW_IDS)


def test_unknown_cycle_falls_back_to_default(registry):
    prof = registry.apply_profile("不存在的周期XYZ")
    assert prof == "default"


def test_profile_disable_and_idempotent_reset(registry):
    # 注入一个禁用 D4 的临时 profile
    registry._profiles["退潮期"] = {"disabled_factors": ["D4_turnover_health"]}

    registry.apply_profile("退潮期")
    assert "D4_turnover_health" not in ps.active_stock_tech_factors(registry)
    # 其余因子不受影响
    assert "D1_n_day_high_low" in ps.active_stock_tech_factors(registry)

    # 切回默认：D4 应复位为启用（幂等复位，防状态污染）
    registry.apply_profile("default")
    assert "D4_turnover_health" in ps.active_stock_tech_factors(registry)


def test_manual_disable_is_reset_by_apply_profile(registry):
    # 模拟上一日残留：手动禁用某因子
    registry.disable_factor("E2_retail_net_ratio")
    assert "E2_retail_net_ratio" not in ps.active_moneyflow_factors(registry)

    # 新一日 apply_profile 应复位到基础启用态
    registry.apply_profile("高潮期")
    assert "E2_retail_net_ratio" in ps.active_moneyflow_factors(registry)