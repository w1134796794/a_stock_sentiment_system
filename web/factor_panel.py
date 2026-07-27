"""指标因子页面状态构建。

把"因子启用开关 / 当前启用指标 / 最新指标数据"汇总成网页可渲染的结构。
写入复用既有覆盖通道（/api/config -> config_registry.apply_updates）：

  - 因子开关 -> scope=yaml, path=factor_registry.factors.<id>.enabled  (bool)

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
    "behavior": "行为周期",
    "lhb": "龙虎榜",
    "cross_cycle": "跨周期",
}

CORE_FACTOR_GROUPS: Dict[str, Dict[str, Any]] = {
    "market": {
        "label": "市场与情绪",
        "factor_ids": [
            "mkt_market_score",
            "mkt_limit_up_count",
            "mkt_limit_down_count",
            "mkt_broken_rate",
            "F1_cycle_duration",
            "F2_market_emotion_divergence",
            "prev_limit_up_premium",
        ],
    },
    "first_board_launch": {
        "label": "首板启动",
        "factor_ids": [
            "limit_progress",
            "stk_behavior_attention",
            "stk_liquidity_percentile",
            "stk_behavior_decay",
            "stk_lhb_crowding_risk",
            "stk_behavior_repair",
            "stk_intraday_seal_quality",
            "stk_crowding_decay_5d",
            "stk_sector_resonance_score",
            "stk_sector_rotation_momentum",
            "stk_amount_ratio_5d",
            "stk_board_height",
        ],
    },
    "weak_to_strong": {
        "label": "弱转强修复",
        "factor_ids": [
            "stk_behavior_repair",
            "stk_sector_resonance_score",
            "stk_liquidity_percentile",
            "stk_behavior_decay",
            "stk_sector_mainline_score",
            "stk_intraday_seal_quality",
            "tech_score",
            "stk_relative_strength_sector",
            "stk_amount_ratio_5d",
            "stk_capital_flow_consensus",
            "stk_sector_rotation_momentum",
        ],
    },
    "mainline_leader": {
        "label": "主线龙头",
        "factor_ids": [
            "stk_sector_mainline_score",
            "stk_sector_resonance_score",
            "stk_relative_strength_sector",
            "stk_behavior_decay",
            "stk_lhb_crowding_risk",
            "stk_kpl_leader_quality",
            "stk_sector_persistence_score",
            "stk_board_position",
            "stk_intraday_seal_quality",
            "stk_behavior_acceleration",
            "stk_lhb_sector_resonance",
        ],
    },
}

CORE_FACTOR_IDS = {
    factor_id
    for group in CORE_FACTOR_GROUPS.values()
    for factor_id in group["factor_ids"]
}

STRATEGY_FACTOR_ROLES: List[Dict[str, Any]] = [
    {
        "strategy_id": "first_board_launch",
        "label": "首板启动",
        "required": ["涨停进度", "首次关注度", "流动性", "市场温度", "涨停家数"],
        "excluded": ["行为衰退", "龙虎榜拥挤"],
        "ranked": ["分歧修复", "封板质量", "拥挤衰减", "板块共振", "成交额与流动性"],
    },
    {
        "strategy_id": "weak_to_strong",
        "label": "弱转强修复",
        "required": ["分歧修复", "板块共振", "流动性", "市场温度"],
        "excluded": ["行为衰退"],
        "ranked": ["分歧修复", "主线地位", "封板质量", "相对强度", "资金共识"],
    },
    {
        "strategy_id": "mainline_leader",
        "label": "主线龙头",
        "required": ["主线地位", "板块共振", "相对强度", "市场温度", "跌停与炸板率"],
        "excluded": ["行为衰退", "龙虎榜拥挤"],
        "ranked": ["主线地位", "KPL龙头质量", "板块持续性", "相对强度", "身位与封板质量"],
    },
]

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
    definitions = {
        f.factor_id: f
        for f in reg._factors.values()  # noqa: SLF001 - 面板只读访问
        if f.factor_id in CORE_FACTOR_IDS
    }
    for f in definitions.values():
        # 龙虎榜属于资金行为，但在操作层面需要独立开关和观察，避免藏在资金流分组里。
        cat = "lhb" if f.sub_category == "lhb" else f.category.value
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

    latest_factor_data = _latest_factor_data(factor_ids=CORE_FACTOR_IDS)
    core_catalog = []
    for group_id, group in CORE_FACTOR_GROUPS.items():
        rows = []
        for factor_id in group["factor_ids"]:
            definition = definitions.get(factor_id)
            rows.append({
                "factor_id": factor_id,
                "name": definition.name if definition else factor_id,
                "description": definition.description if definition else "由计算层生成的核心指标",
                "available": definition is not None,
            })
        core_catalog.append({
            "group_id": group_id,
            "label": group["label"],
            "factors": rows,
        })

    return {
        "factor_groups": factor_groups,
        "factor_total": total,
        "factor_enabled": enabled,
        "enabled_factor_list": enabled_factor_list,
        "active_profile": active_profile or "",
        "snapshot_enabled_factors": latest_factor_data.get("snapshot_enabled_factors", []),
        "latest_factor_trade_date": latest_factor_data.get("trade_date", ""),
        "latest_factor_summary": latest_factor_data.get("rows", []),
        "override_count": _count(yaml_store),
        "core_catalog": core_catalog,
        "strategy_factor_roles": STRATEGY_FACTOR_ROLES,
    }


def _count(d: Any) -> int:
    return len(d) if isinstance(d, dict) else 0


def _latest_factor_data(
    limit: int = 120,
    factor_ids: Optional[set[str]] = None,
) -> Dict[str, Any]:
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
            where_factor = ""
            params: List[Any] = [trade_date]
            if factor_ids:
                ordered_ids = sorted(factor_ids)
                where_factor = f" AND factor_id IN ({','.join('?' for _ in ordered_ids)})"
                params.extend(ordered_ids)
            params.append(int(limit))
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
                {where_factor}
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
                """.format(where_factor=where_factor),
                params,
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


def _dynamic_weight_state(trade_date: str, profile: str) -> Dict[str, Any]:
    try:
        from core.factors.factor_library import DynamicWeightRepository, FactorLibraryTrainer

        profile = profile if profile in {"default", "momentum_repair"} else "default"
        artifact = DynamicWeightRepository().resolve(trade_date or "99999999", profile)
        prior = FactorLibraryTrainer().prior_weights(profile)
        if artifact is None:
            return {
                "source": "冷启动先验",
                "effective_date": "",
                "model_type": "manual_prior",
                "rows": [
                    {"factor": factor, "weight": weight, "prior": weight, "ic_mean": None, "ic_ir": None}
                    for factor, weight in sorted(prior.items(), key=lambda item: item[1], reverse=True)
                ],
            }
        metrics = artifact.payload.get("factor_metrics") or {}
        confidence_profiles = artifact.payload.get("confidence_profiles") or {}
        confidence_profile = confidence_profiles.get("all") or {}
        candidate_model = artifact.payload.get("candidate_model") or {}
        calibration = (
            candidate_model.get("calibration")
            if candidate_model.get("active")
            else confidence_profile.get("calibration")
        ) or {}
        conformal = (
            candidate_model.get("return_conformal")
            if candidate_model.get("active")
            else confidence_profile.get("conformal")
        ) or {}
        regime_model = artifact.payload.get("market_regime_model") or {}
        rows = []
        for factor, weight in sorted(artifact.weights.items(), key=lambda item: item[1], reverse=True):
            row = metrics.get(factor) or {}
            rows.append({
                "factor": factor,
                "weight": float(weight),
                "prior": float((artifact.payload.get("prior_weights") or {}).get(factor, 0.0)),
                "ic_mean": row.get("ic_mean"),
                "ic_ir": row.get("ic_ir"),
                "positive_ratio": row.get("positive_ratio"),
            })
        versions = []
        for path in sorted(artifact.path.parent.glob("weights_*.json"))[-12:]:
            try:
                import json

                payload = json.loads(path.read_text(encoding="utf-8"))
                oos = payload.get("oos_evaluation") or {}
                versions.append({
                    "effective_date": payload.get("effective_date") or path.stem.removeprefix("weights_"),
                    "model_type": payload.get("model_type") or "",
                    "rank_ic": oos.get("rank_ic"),
                    "top_excess_return": oos.get("top_excess_return"),
                    "top_excess_win_rate": oos.get("top_excess_win_rate"),
                })
            except Exception:
                continue
        bins = list(confidence_profile.get("bins") or [])
        deciles = []
        if bins:
            groups = [group for group in __import__("numpy").array_split(bins, min(10, len(bins))) if len(group)]
            for index, group in enumerate(groups, start=1):
                weights = [max(int(row.get("sample_size") or 0), 1) for row in group]
                total_weight = sum(weights)
                deciles.append({
                    "decile": index,
                    "expected_return": sum(float(row.get("expected_return") or 0.0) * weight for row, weight in zip(group, weights)) / total_weight,
                    "success_probability": sum(float(row.get("success_probability") or 0.0) * weight for row, weight in zip(group, weights)) / total_weight,
                })
        drift = _latest_screening_drift(trade_date)
        return {
            "source": "LightGBM + IC/IR" if candidate_model.get("active") else "IC/IR 动态权重",
            "effective_date": artifact.effective_date,
            "model_type": artifact.payload.get("model_type", "ic_ir"),
            "train_start": artifact.payload.get("train_start", ""),
            "train_end": artifact.payload.get("train_end", ""),
            "training_days": artifact.payload.get("training_days", 0),
            "selected_hyperparameters": artifact.payload.get("selected_hyperparameters") or {},
            "calibration": calibration,
            "conformal": conformal,
            "bootstrap": confidence_profile.get("bootstrap") or {},
            "adwin": confidence_profile.get("adwin") or {},
            "deciles": deciles,
            "versions": versions,
            "candidate_model": candidate_model,
            "market_regime_model": regime_model,
            "feature_drift": drift,
            "publication_gate": artifact.payload.get("publication_gate") or {},
            "rows": rows,
        }
    except Exception as exc:  # noqa: BLE001
        return {"source": "读取失败", "error": str(exc), "rows": []}


def _latest_screening_drift(trade_date: str) -> Dict[str, Any]:
    try:
        import json
        from pathlib import Path

        from config.settings import WEB_DATA_DIR

        directory = Path(WEB_DATA_DIR) / "screening"
        path = directory / f"screening_{trade_date}.json" if trade_date else None
        if path is None or not path.exists():
            paths = sorted(directory.glob("screening_*.json"))
            path = paths[-1] if paths else None
        if path is None:
            return {"status": "unknown"}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ((payload.get("weight_metadata") or {}).get("feature_drift") or {"status": "unknown"})
    except Exception:
        return {"status": "unknown"}
