"""指标因子页面状态构建。

把"因子启用开关 / 当前启用指标 / 最新指标数据"汇总成网页可渲染的结构。
写入复用既有覆盖通道（/api/config -> config_registry.apply_updates）：

  - 因子开关 -> scope=yaml, path=factor_registry.factors.<id>.enabled  (bool)

读取时先 reload，保证回显与覆盖文件一致。
"""
from __future__ import annotations

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

def build_factor_state(active_profile: Optional[str] = None) -> Dict[str, Any]:
    """构建指标因子页面完整状态。active_profile 为最新快照实际生效的方案（仅展示用）。"""
    from config import overrides as ov
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

    latest_factor_data = _latest_factor_data()

    return {
        "factor_groups": factor_groups,
        "factor_total": total,
        "factor_enabled": enabled,
        "enabled_factor_list": enabled_factor_list,
        "profiles": profiles,
        "profile_names": [p["name"] for p in profiles],
        "active_profile": active_profile or "",
        "snapshot_enabled_factors": latest_factor_data.get("snapshot_enabled_factors", []),
        "latest_factor_trade_date": latest_factor_data.get("trade_date", ""),
        "latest_factor_summary": latest_factor_data.get("rows", []),
        "override_count": _count(yaml_store),
    }


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
