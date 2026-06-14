"""龙头生命周期注册中心（Phase 1 地基）单测。

覆盖 classify_phase 的阶段划分、交接带标记、淘汰，以及 Registry.query 订阅。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.pattern.dragon_lifecycle import (
    DragonLifecycleRegistry,
    DragonPhase,
    DragonState,
    WEAK_TO_STRONG_PHASES,
    DRAGON_SECOND_WAVE_PHASES,
    classify_phase,
)

CFG = {
    "wts_max_watch_days": 5,
    "wts_max_drawdown": 0.20,
    "dsw_min_adjust_days": 2,
    "dsw_max_adjust_days": 15,
    "dsw_min_adjust_depth": 0.05,
    "dsw_max_adjust_depth": 0.30,
    "handoff_overlap_enabled": True,
    "expire_max_days_since_peak": 20,
}


def _classify(**kw):
    base = dict(
        weakened=True, days_since_weakening=1, days_since_peak=1,
        max_drawdown=0.08, same_day_recovered=False, adjust_depth=0.08, cfg=CFG,
    )
    base.update(kw)
    return classify_phase(**base)


def test_not_weakened_is_leader():
    st = _classify(weakened=False)
    assert st.phase == DragonPhase.LEADER


def test_flash_recovery_same_day():
    st = _classify(same_day_recovered=True)
    assert st.phase == DragonPhase.FLASH_RECOVERY
    assert st.same_day is True


def test_expired_by_drawdown():
    # 回调超龙二波最深容忍(0.30) → A杀淘汰
    st = _classify(max_drawdown=0.40)
    assert st.phase == DragonPhase.EXPIRED


def test_same_day_overrides_expiry():
    # 日内反转优先于淘汰判定：即便回调很深，当日收复仍判 FLASH_RECOVERY
    st = _classify(same_day_recovered=True, max_drawdown=0.40, days_since_peak=25)
    assert st.phase == DragonPhase.FLASH_RECOVERY


def test_expired_by_days():
    # 既过弱转强窗(距走弱30>5)、又不在龙二波域(距高点25>15)、距高点>20 → 淘汰
    st = _classify(days_since_weakening=30, days_since_peak=25, adjust_depth=0.10)
    assert st.phase == DragonPhase.EXPIRED


def test_recent_weakening_old_peak_still_wts():
    # 关键回归：距高点久(25天)但近日才走弱(2天)、浅回调 → 仍属弱转强域，不应淘汰
    st = _classify(days_since_weakening=2, days_since_peak=25, max_drawdown=0.10)
    assert st.phase in WEAK_TO_STRONG_PHASES


def test_weak_to_strong_window():
    # 距走弱 1 天、浅调 → 观察域
    st = _classify(days_since_weakening=1, days_since_peak=1)
    assert st.phase in (DragonPhase.WEAKENING, DragonPhase.WATCHING)


def test_dragon_second_wave_window():
    # 距走弱已过弱转强窗(6>5)、距高点 8 天、深度 0.18 在区间 → 龙二波域
    st = _classify(days_since_weakening=6, days_since_peak=8, adjust_depth=0.18, max_drawdown=0.18)
    assert st.phase in DRAGON_SECOND_WAVE_PHASES


def test_handoff_band_marked():
    # 距走弱 4 天（∈[2,5]）→ 交接带标记
    st = _classify(days_since_weakening=4, days_since_peak=4)
    assert st.both_eligible is True


def test_registry_query_by_phase():
    reg = DragonLifecycleRegistry(cfg=CFG)
    reg.upsert(DragonState(stock_code="000001", phase=DragonPhase.WATCHING))
    reg.upsert(DragonState(stock_code="000002", phase=DragonPhase.ADJUSTING))
    reg.upsert(DragonState(stock_code="000003", phase=DragonPhase.EXPIRED))

    wts = reg.query(WEAK_TO_STRONG_PHASES, include_handoff=False)
    assert [s.stock_code for s in wts] == ["000001"]

    dsw = reg.query(DRAGON_SECOND_WAVE_PHASES, include_handoff=False)
    assert [s.stock_code for s in dsw] == ["000002"]


def test_registry_query_includes_handoff():
    reg = DragonLifecycleRegistry(cfg=CFG)
    s = DragonState(stock_code="000004", phase=DragonPhase.ADJUSTING, both_eligible=True)
    reg.upsert(s)
    # 查弱转强域时，交接带票也应被纳入
    hit = reg.query(WEAK_TO_STRONG_PHASES, include_handoff=True)
    assert "000004" in [x.stock_code for x in hit]