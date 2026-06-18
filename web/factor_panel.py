"""指标因子页面状态构建。

把"因子启用开关 / 情绪周期 profile / 各策略置信度模式"汇总成网页可渲染的结构。
写入复用既有覆盖通道（/api/config -> config_registry.apply_updates）：

  - 因子开关   -> scope=yaml,      path=factor_registry.factors.<id>.enabled  (bool)
  - 置信度模式 -> scope=patterns,  path=<group>.confidence_mode               (str: legacy/deduction)
  - 强制profile-> scope=settings,  path=FACTOR_PROFILE_OVERRIDE                (str: 空=按周期自动)

读取时先 reload，保证回显与覆盖文件一致。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# 因子大类中文标签（与 FactorCategory.value 对应）
CATEGORY_LABELS: Dict[str, str] = {
    "market_env": "大盘环境",
    "emotion": "情绪周期",
    "sector": "板块",
    "stock_tech": "个股技术",
    "moneyflow": "资金流",
    "cross_cycle": "跨周期",
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
    """构建指标因子页面完整状态。active_profile 为最新快照实际生效的方案（仅展示用）。"""
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
        g["enabled_count"] = sum(1 for x in g["factors"] if x["enabled"])
        g["overridden_count"] = sum(1 for x in g["factors"] if x["overridden"])
        factor_groups.append(g)

    total = sum(len(g["factors"]) for g in factor_groups)
    enabled = sum(1 for g in factor_groups for x in g["factors"] if x["enabled"])
    enabled_factor_list = [
        {
            **x,
            "category": g["category"],
            "category_label": g["label"],
        }
        for g in factor_groups
        for x in g["factors"]
        if x["enabled"]
    ]

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

    # ---- 生命周期 / 仲裁等可调参数组（Phase 6：暴露标量参数，经 patterns 覆盖即时生效）----
    param_groups = _build_tunable_param_groups(pat_store)
    latest_factor_data = _latest_factor_data()

    return {
        "factor_groups": factor_groups,
        "factor_total": total,
        "factor_enabled": enabled,
        "enabled_factor_list": enabled_factor_list,
        "strategies": strategies,
        "confidence_modes": CONFIDENCE_MODES,
        "profiles": profiles,
        "profile_names": [p["name"] for p in profiles],
        "forced_profile": forced_profile,
        "active_profile": active_profile or "",
        "snapshot_enabled_factors": latest_factor_data.get("snapshot_enabled_factors", []),
        "latest_factor_trade_date": latest_factor_data.get("trade_date", ""),
        "latest_factor_summary": latest_factor_data.get("rows", []),
        "param_groups": param_groups,
        "override_count": _count(yaml_store) + _count(pat_store) + _count(settings_store),
    }


# 面板暴露的可调参数组（仅标量参数；嵌套 dict/list 如 emotion_routing 不在此编辑）
TUNABLE_PARAM_GROUPS = ("dragon_lifecycle", "arbitration")
_PARAM_GROUP_LABELS = {
    "dragon_lifecycle": "龙头生命周期 · 阶段窗口/切源",
    "arbitration": "跨策略仲裁 · 择主/共振/情绪路由",
}


def _build_tunable_param_groups(pat_store: Dict[str, Any]) -> List[Dict[str, Any]]:
    from config import pattern_params as pp
    from config.param_docs import PATTERN_DESC

    out: List[Dict[str, Any]] = []
    for grp in TUNABLE_PARAM_GROUPS:
        eff = pp.get_params(grp) or {}
        ov_keys = (pat_store.get(grp, {}) or {})
        params: List[Dict[str, Any]] = []
        for key, val in eff.items():
            if isinstance(val, bool):
                ptype = "bool"
            elif isinstance(val, (int, float)):
                ptype = "num"
            elif isinstance(val, str):
                ptype = "str"
            else:
                continue  # 跳过嵌套 dict/list（如 emotion_routing/emotion_gate）
            params.append({
                "key": key,
                "value": val,
                "type": ptype,
                "desc": PATTERN_DESC.get(grp, {}).get(key, ""),
                "override_path": f"{grp}.{key}",
                "overridden": key in ov_keys,
            })
        if params:
            out.append({
                "group": grp,
                "label": _PARAM_GROUP_LABELS.get(grp, grp),
                "params": params,
            })
    return out


def _count(d: Any) -> int:
    return len(d) if isinstance(d, dict) else 0


def _latest_factor_data(limit: int = 120) -> Dict[str, Any]:
    try:
        import duckdb  # type: ignore

        from pathlib import Path

        from config.settings import FACTOR_DB_PATH, SNAPSHOT_DIR
        from snapshot.reader import SnapshotReader

        reader = SnapshotReader(SNAPSHOT_DIR)
        latest = reader.latest()
        snap = reader.load(latest) if latest else None
        meta = (snap or {}).get("meta", {}) or {}
        enabled = list(meta.get("enabled_factors") or [])
        db_path = Path(FACTOR_DB_PATH)
        if not db_path.exists():
            return {"trade_date": latest or "", "snapshot_enabled_factors": enabled, "rows": []}

        with duckdb.connect(str(db_path), read_only=True) as con:
            exists = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'factor_value_long'"
            ).fetchone()[0]
            if not exists:
                return {"trade_date": latest or "", "snapshot_enabled_factors": enabled, "rows": []}
            trade_date = str(latest or "")
            if not trade_date or not con.execute(
                "SELECT COUNT(*) FROM factor_value_long WHERE trade_date = ?",
                [trade_date],
            ).fetchone()[0]:
                trade_date = str(con.execute("SELECT MAX(trade_date) FROM factor_value_long").fetchone()[0] or "")
            if not trade_date:
                return {"trade_date": "", "snapshot_enabled_factors": enabled, "rows": []}
            df = con.execute(
                """
                SELECT
                    entity_type,
                    factor_id,
                    COUNT(*) AS entity_count,
                    ROUND(AVG(raw_value), 4) AS avg_raw_value,
                    ROUND(AVG(score), 2) AS avg_score,
                    ROUND(MIN(score), 2) AS min_score,
                    ROUND(MAX(score), 2) AS max_score
                FROM factor_value_long
                WHERE trade_date = ?
                GROUP BY entity_type, factor_id
                ORDER BY
                    CASE entity_type
                        WHEN 'market' THEN 1
                        WHEN 'sector' THEN 2
                        WHEN 'stock' THEN 3
                        ELSE 9
                    END,
                    factor_id
                LIMIT ?
                """,
                [trade_date, int(limit)],
            ).fetchdf()
        raw_rows = df.to_dict(orient="records") if df is not None and not df.empty else []
        rows = [{k: _json_scalar(v) for k, v in row.items()} for row in raw_rows]
        return {"trade_date": trade_date, "snapshot_enabled_factors": enabled, "rows": rows}
    except Exception:
        return {"trade_date": "", "snapshot_enabled_factors": [], "rows": []}


def _json_scalar(value: Any) -> Any:
    try:
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    return value
