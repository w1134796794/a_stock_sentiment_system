"""Phase 4：因子面板状态构建。

把"因子启用开关 / 情绪周期 profile / 各策略置信度模式"汇总成网页可渲染的结构。
写入复用既有覆盖通道（/api/config -> config_registry.apply_updates）：

  - 因子开关   -> scope=yaml,      path=factor_registry.factors.<id>.enabled  (bool)
  - 置信度模式 -> scope=patterns,  path=<group>.confidence_mode               (str: legacy/deduction)
  - 强制profile-> scope=settings,  path=FACTOR_PROFILE_OVERRIDE                (str: 空=按周期自动)

读取时先 reload，保证回显与覆盖文件一致。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 因子大类中文标签（与 FactorCategory.value 对应）
CATEGORY_LABELS: Dict[str, str] = {
    "market_env": "大盘环境 (Layer1)",
    "emotion": "情绪周期",
    "sector": "板块 (Layer2)",
    "stock_tech": "个股技术 D (Layer3)",
    "moneyflow": "资金流 E (Layer3/4)",
    "cross_cycle": "跨周期 (Layer4)",
}

# 已接入 confidence_mode 的策略组
STRATEGY_GROUPS: List[str] = [
    "second_board_dragon",
    "weak_to_strong",
    "first_board_breakout",
    "dragon_second_wave",
]

CONFIDENCE_MODES = ["legacy", "deduction"]


def build_factor_state(active_profile: Optional[str] = None) -> Dict[str, Any]:
    """构建因子面板完整状态。active_profile 为最新快照实际生效的 profile（仅展示用）。"""
    from config import overrides as ov
    from config import pattern_params as pp
    from config.config_loader import get_config_loader
    from core.factors.factor_registry import get_factor_registry

    # 先让 config_loader / 注册中心反映最新覆盖文件
    try:
        get_config_loader().reload_config()
    except Exception:
        pass
    reg = get_factor_registry()
    try:
        reg.reload()
    except Exception:
        pass

    store = ov.load_overrides()
    yaml_store = store.get("yaml", {}) or {}
    pat_store = store.get("patterns", {}) or {}
    settings_store = store.get("settings", {}) or {}

    # ---- 因子开关（按大类分组）----
    cat_order = list(CATEGORY_LABELS.keys())
    groups: Dict[str, Dict[str, Any]] = {}
    for f in reg._factors.values():  # noqa: SLF001 - 面板只读访问
        cat = f.category.value
        path = f"factor_registry.factors.{f.factor_id}.enabled"
        groups.setdefault(cat, {
            "category": cat,
            "label": CATEGORY_LABELS.get(cat, cat),
            "factors": [],
        })["factors"].append({
            "factor_id": f.factor_id,
            "name": f.name,
            "sub_category": f.sub_category,
            "description": f.description,
            "enabled": bool(f.enabled),
            "overridden": path in yaml_store,
            "override_path": path,
        })
    factor_groups: List[Dict[str, Any]] = []
    for cat in sorted(groups.keys(), key=lambda c: (cat_order.index(c) if c in cat_order else 99, c)):
        g = groups[cat]
        g["factors"].sort(key=lambda x: (x["sub_category"], x["factor_id"]))
        factor_groups.append(g)

    total = sum(len(g["factors"]) for g in factor_groups)
    enabled = sum(1 for g in factor_groups for x in g["factors"] if x["enabled"])

    # ---- 各策略置信度模式 ----
    strategies: List[Dict[str, Any]] = []
    for grp in STRATEGY_GROUPS:
        eff = pp.get_params(grp)
        strategies.append({
            "group": grp,
            "label": pp.PATTERN_GROUP_LABELS.get(grp, grp),
            "mode": eff.get("confidence_mode", "legacy"),
            "overridden": "confidence_mode" in (pat_store.get(grp, {}) or {}),
            "override_path": f"{grp}.confidence_mode",
        })

    # ---- 情绪周期 profile ----
    profiles_raw = reg.get_profiles() or {}
    profiles: List[Dict[str, Any]] = []
    for name, prof in profiles_raw.items():
        prof = prof or {}
        profiles.append({
            "name": name,
            "disabled_factors": list(prof.get("disabled_factors") or []),
            "enabled_factors": list(prof.get("enabled_factors") or []),
            "description": prof.get("description", ""),
        })
    profiles.sort(key=lambda p: p["name"])
    forced_profile = str(settings_store.get("FACTOR_PROFILE_OVERRIDE", "") or "")

    return {
        "factor_groups": factor_groups,
        "factor_total": total,
        "factor_enabled": enabled,
        "strategies": strategies,
        "confidence_modes": CONFIDENCE_MODES,
        "profiles": profiles,
        "profile_names": [p["name"] for p in profiles],
        "forced_profile": forced_profile,
        "active_profile": active_profile or "",
        "override_count": _count(yaml_store) + _count(pat_store) + _count(settings_store),
    }


def _count(d: Any) -> int:
    return len(d) if isinstance(d, dict) else 0