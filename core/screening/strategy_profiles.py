"""Versioned, user-managed screening strategy combinations.

Strategies select how existing factor data is filtered and ranked. They never
trigger data fetching or factor computation.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml


STRATEGY_ID = re.compile(r"^[A-Za-z0-9_-]{2,48}$")
FACTOR_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,80}$")
SUPPORTED_OPERATORS = (">=", ">", "<=", "<", "==", "!=", "between", "in", "not_in")
WEIGHT_SOURCES = ("manual", "ic_ir", "lightgbm", "xgboost")
MARKET_REGIMES = ("strong", "neutral", "weak")
STOCK_POOLS = ("all", "liquid", "near_limit")
TRAINING_SCOPES = ("all", "near_limit")
ENHANCEMENTS = ("capital_flow", "attention", "leader", "margin", "risk")
ENTRY_MODES = ("weak_to_strong", "continuation", "acceleration")
DEFAULT_EXECUTION = {
    "allowed_entry_modes": ["weak_to_strong", "continuation"],
    "confirmation_deadline": "10:00:00",
    "candidate_max_age_days": 1,
    "max_positions": 0,
}

_LOCK = threading.RLock()


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _weight_rows(weights: Mapping[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"factor": str(factor), "weight": float(weight)}
        for factor, weight in weights.items()
        if FACTOR_ID.match(str(factor)) and float(weight) != 0.0
    ]


def _execution_config(source: Any) -> Dict[str, Any]:
    """Normalize the execution contract shared by realtime and backtests."""
    raw = dict(source or {}) if isinstance(source, Mapping) else {}
    modes = [str(item).strip() for item in raw.get("allowed_entry_modes") or []]
    modes = [item for item in modes if item in ENTRY_MODES]
    deadline = str(raw.get("confirmation_deadline") or DEFAULT_EXECUTION["confirmation_deadline"])
    if not re.match(r"^(09|10):[0-5]\d:[0-5]\d$", deadline):
        deadline = DEFAULT_EXECUTION["confirmation_deadline"]
    candidate_max_age_days = int(raw.get("candidate_max_age_days") or DEFAULT_EXECUTION["candidate_max_age_days"])
    max_positions = int(raw.get("max_positions") or 0)
    return {
        "allowed_entry_modes": modes or list(DEFAULT_EXECUTION["allowed_entry_modes"]),
        "confirmation_deadline": deadline,
        "candidate_max_age_days": max(1, min(candidate_max_age_days, 5)),
        "max_positions": max(0, min(max_positions, 20)),
    }


def _strategy_version(profile: Mapping[str, Any]) -> str:
    """Stable fingerprint: results remain reproducible after later edits."""
    payload = {key: value for key, value in profile.items() if key != "version"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


class StrategyProfileRepository:
    """Persist strategy combinations separately from generated market data."""

    def __init__(
        self,
        path: Optional[Path] = None,
        base_profile_path: Optional[Path] = None,
    ) -> None:
        from config.settings import BASE_DIR

        self.path = Path(path or BASE_DIR / "config" / "strategy_combinations.yaml")
        self.base_profile_path = Path(
            base_profile_path or BASE_DIR / "config" / "screening_profiles.yaml"
        )

    def _base_profiles(self) -> Dict[str, Dict[str, Any]]:
        if not self.base_profile_path.exists():
            return {}
        payload = yaml.safe_load(self.base_profile_path.read_text(encoding="utf-8")) or {}
        return payload.get("screening_profiles") or {}

    def _payload(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "strategies": {}}
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        payload.setdefault("version", 1)
        payload.setdefault("strategies", {})
        return payload

    def list_profiles(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        payload = self._payload()
        rows = []
        for profile_id in payload.get("strategies") or {}:
            row = self.get_profile(profile_id)
            if row and (not enabled_only or row.get("enabled")):
                rows.append(row)
        rows.sort(key=lambda item: (not bool(item.get("primary")), str(item.get("name"))))
        return rows

    def get_profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        raw = (self._payload().get("strategies") or {}).get(str(profile_id))
        if not isinstance(raw, Mapping):
            return None
        base_name = str(raw.get("base_profile") or "default")
        base = deepcopy((self._base_profiles().get(base_name) or {}))
        ranking = base.get("ranking") or {}
        weights = ranking.get("prior_weights") or ranking.get("weights") or {}
        enhancements = raw.get("enhancements") or {}
        profile = {
            "id": str(profile_id),
            "name": str(raw.get("name") or profile_id),
            "description": str(raw.get("description") or base.get("description") or ""),
            "enabled": bool(raw.get("enabled", True)),
            "primary": bool(raw.get("primary", False)),
            "protected": bool(raw.get("protected", False)),
            "base_profile": base_name,
            "stock_pool": str(raw.get("stock_pool") or "all"),
            "training_scope": str(raw.get("training_scope") or "all"),
            "required_filters": deepcopy(raw.get("required_filters", base.get("hard_filters") or [])),
            "exclusion_filters": deepcopy(raw.get("exclusion_filters") or []),
            "ranking_factors": deepcopy(raw.get("ranking_factors") or _weight_rows(weights)),
            "weight_source": str(raw.get("weight_source") or "ic_ir"),
            "weight_profile": str(raw.get("weight_profile") or base_name),
            "enhancements": {
                "lhb": bool(enhancements.get("lhb", (base.get("lhb_enhancement") or {}).get("enabled", True))),
                "capital_flow": bool(enhancements.get("capital_flow", True)),
            },
            "market_regimes": list(raw.get("market_regimes") or MARKET_REGIMES),
            "top_n": int(raw.get("top_n") or ranking.get("top_n") or 10),
            "position_cap_pct": float(raw.get("position_cap_pct") or 0.0),
            "execution": _execution_config(raw.get("execution")),
        }
        profile["version"] = _strategy_version(profile)
        return profile

    def resolve(self, profile_id: str) -> Dict[str, Any]:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"策略组合不存在: {profile_id}")
        base = deepcopy((self._base_profiles().get(profile["base_profile"]) or {}))
        ranking = base.setdefault("ranking", {})
        weights = {
            str(row["factor"]): float(row["weight"])
            for row in profile["ranking_factors"]
            if float(row.get("weight") or 0.0) != 0.0
        }
        ranking["prior_weights"] = weights
        ranking["weights"] = weights
        ranking["top_n"] = int(profile["top_n"])
        base["hard_filters"] = deepcopy(profile["required_filters"])
        base["exclusion_filters"] = deepcopy(profile["exclusion_filters"])
        base["allowed_market_regimes"] = list(profile["market_regimes"])
        base["strategy_weight_source"] = profile["weight_source"]
        base["strategy_weight_profile"] = profile["weight_profile"]
        base["strategy_training_scope"] = profile["training_scope"]
        base["position_cap_pct"] = float(profile.get("position_cap_pct") or 0.0)
        base["strategy_id"] = profile["id"]
        base["strategy_name"] = profile["name"]
        base["strategy_version"] = profile["version"]
        base["strategy_execution"] = deepcopy(profile["execution"])
        base["description"] = profile["description"]
        base.setdefault("lhb_enhancement", {})["enabled"] = bool(profile["enhancements"]["lhb"])
        base["enhancements"] = {
            "enabled": ["capital_flow"] if profile["enhancements"]["capital_flow"] else []
        }
        self._inject_stock_pool(base, profile["stock_pool"])
        return base

    @staticmethod
    def _inject_stock_pool(config: Dict[str, Any], stock_pool: str) -> None:
        rules = config.setdefault("hard_filters", [])
        if stock_pool == "liquid":
            rules.insert(0, {
                "name": "股票池流动性门槛", "factor": "stk_liquidity_percentile",
                "op": ">=", "value": 35, "reason": "属于流动性合格股票池",
            })
        elif stock_pool == "near_limit":
            rules.insert(0, {
                "name": "涨停强势股票池", "factor": "limit_progress",
                "op": ">=", "value": 0.90, "reason": "属于涨停或接近涨停股票池",
            })

    def save(self, profile_id: str, data: Mapping[str, Any]) -> Dict[str, Any]:
        profile_id = str(profile_id or "").strip()
        if not STRATEGY_ID.match(profile_id):
            raise ValueError("策略标识只能使用2-48位字母、数字、下划线或短横线")
        normalized = self._validate(profile_id, data)
        with _LOCK:
            payload = self._payload()
            strategies = payload.setdefault("strategies", {})
            if normalized["primary"]:
                for key, row in strategies.items():
                    if key != profile_id and isinstance(row, dict):
                        row["primary"] = False
            existing = strategies.get(profile_id) or {}
            normalized["protected"] = bool(existing.get("protected", data.get("protected", False)))
            strategies[profile_id] = normalized
            self._write(payload)
        return self.get_profile(profile_id) or {}

    def delete(self, profile_id: str) -> None:
        with _LOCK:
            payload = self._payload()
            strategies = payload.get("strategies") or {}
            current = strategies.get(profile_id)
            if not current:
                raise ValueError("策略组合不存在")
            if current.get("protected"):
                raise ValueError("内置策略不能删除，可以停用或复制后调整")
            strategies.pop(profile_id, None)
            self._write(payload)

    def validate_selection(self, profile_ids: Iterable[str]) -> List[str]:
        selected: List[str] = []
        for value in profile_ids:
            profile_id = str(value or "").strip()
            profile = self.get_profile(profile_id)
            if profile is None:
                raise ValueError(f"策略组合不存在: {profile_id}")
            if not profile.get("enabled"):
                raise ValueError(f"策略组合已停用: {profile.get('name')}")
            if profile_id not in selected:
                selected.append(profile_id)
        if not selected:
            raise ValueError("请至少选择一个策略组合")
        return selected

    def _validate(self, profile_id: str, data: Mapping[str, Any]) -> Dict[str, Any]:
        base_profile = str(data.get("base_profile") or "default")
        if base_profile not in self._base_profiles():
            raise ValueError("基础方案不存在")
        stock_pool = str(data.get("stock_pool") or "all")
        if stock_pool not in STOCK_POOLS:
            raise ValueError("股票池范围不受支持")
        training_scope = str(data.get("training_scope") or "all")
        if training_scope not in TRAINING_SCOPES:
            raise ValueError("训练范围不受支持")
        weight_source = str(data.get("weight_source") or "manual")
        if weight_source not in WEIGHT_SOURCES:
            raise ValueError("权重来源不受支持")
        filters_required = self._validate_filters(data.get("required_filters") or [])
        filters_excluded = self._validate_filters(data.get("exclusion_filters") or [])
        ranking_factors = []
        for row in data.get("ranking_factors") or []:
            factor = str((row or {}).get("factor") or "").strip()
            if not FACTOR_ID.match(factor):
                raise ValueError(f"排序因子标识无效: {factor}")
            weight = float((row or {}).get("weight") or 0.0)
            if abs(weight) > 1.0:
                raise ValueError("单个因子权重绝对值不能超过1")
            if weight:
                ranking_factors.append({"factor": factor, "weight": weight})
        if not ranking_factors:
            raise ValueError("请至少选择一个非零权重排序因子")
        regimes = [str(item) for item in data.get("market_regimes") or [] if str(item) in MARKET_REGIMES]
        if not regimes:
            raise ValueError("请至少选择一种适用市场状态")
        top_n = int(data.get("top_n") or 10)
        if not 1 <= top_n <= 100:
            raise ValueError("输出数量必须在1-100之间")
        position_cap_pct = float(data.get("position_cap_pct") or 0.0)
        if not 0.0 <= position_cap_pct <= 100.0:
            raise ValueError("单票仓位上限必须在0-100之间")
        enhancements = data.get("enhancements") or {}
        execution = _execution_config(data.get("execution"))
        return {
            "name": str(data.get("name") or profile_id).strip()[:40],
            "description": str(data.get("description") or "").strip()[:300],
            "enabled": bool(data.get("enabled", True)),
            "primary": bool(data.get("primary", False)),
            "base_profile": base_profile,
            "stock_pool": stock_pool,
            "training_scope": training_scope,
            "required_filters": filters_required,
            "exclusion_filters": filters_excluded,
            "ranking_factors": ranking_factors,
            "weight_source": weight_source,
            "weight_profile": str(data.get("weight_profile") or base_profile),
            "enhancements": {
                "lhb": bool(enhancements.get("lhb", True)),
                "capital_flow": bool(enhancements.get("capital_flow", True)),
            },
            "market_regimes": regimes,
            "top_n": top_n,
            "position_cap_pct": position_cap_pct,
            "execution": execution,
        }

    @staticmethod
    def _validate_filters(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        output = []
        for index, source in enumerate(rows, start=1):
            row = dict(source or {})
            factor = str(row.get("factor") or "").strip()
            op = str(row.get("op") or "").strip()
            if not FACTOR_ID.match(factor):
                raise ValueError(f"第{index}条条件的因子无效")
            if op not in SUPPORTED_OPERATORS:
                raise ValueError(f"第{index}条条件的运算符无效")
            output.append({
                "name": str(row.get("name") or factor).strip()[:40],
                "factor": factor,
                "op": op,
                "value": deepcopy(row.get("value")),
                "reason": str(row.get("reason") or "").strip()[:120],
            })
        return output

    def _write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(dict(payload), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def factor_catalog() -> List[Dict[str, Any]]:
    """Return factors suitable for the strategy editor."""
    from core.factors.factor_registry import get_factor_registry

    registry = get_factor_registry()
    try:
        registry.reload()
    except Exception:
        pass
    rows = []
    for definition in registry._factors.values():  # noqa: SLF001
        rows.append({
            "id": definition.factor_id,
            "name": definition.name,
            "category": definition.category.value,
            "enabled": bool(definition.enabled),
            "description": definition.description,
        })
    extras = {
        "tech_score": "技术综合分",
        "limit_progress": "涨停进度",
        "amount_ratio": "成交额相对5日",
        "mkt_market_score": "市场环境分",
        "stk_liquidity_percentile": "流动性分位",
        "stk_new_high_20d": "20日强势位置",
        "stk_amount_ratio_5d": "5日成交额比",
        "stk_vol_ratio_5d": "5日量比",
        "stk_board_position": "打板身位",
        "stk_sector_mainline_score": "板块主线强度",
        "stk_sector_persistence_score": "板块持续性",
        "stk_sector_resonance_score": "板块共振",
        "stk_intraday_seal_quality": "封板质量",
        "stk_sector_rotation_momentum": "板块轮动动量",
        "stk_crowding_decay_5d": "拥挤衰减",
        "stk_relative_strength_sector": "相对板块强度",
        "stk_pct_chg_1d": "当日涨跌强度",
        "stk_attention_crowding_risk": "关注度拥挤风险",
    }
    known = {row["id"] for row in rows}
    rows.extend(
        {"id": key, "name": name, "category": "derived", "enabled": True, "description": "筛选派生指标"}
        for key, name in extras.items() if key not in known
    )
    known = {row["id"] for row in rows}
    repository = StrategyProfileRepository()
    referenced = set()
    for profile in repository.list_profiles():
        referenced.update(str(row.get("factor") or "") for row in profile.get("required_filters") or [])
        referenced.update(str(row.get("factor") or "") for row in profile.get("exclusion_filters") or [])
        referenced.update(str(row.get("factor") or "") for row in profile.get("ranking_factors") or [])
    rows.extend(
        {"id": factor, "name": factor, "category": "derived", "enabled": True, "description": "策略配置引用字段"}
        for factor in sorted(referenced) if FACTOR_ID.match(factor) and factor not in known
    )
    rows.sort(key=lambda row: (row["category"], row["name"], row["id"]))
    return rows


__all__ = [
    "DEFAULT_EXECUTION", "ENTRY_MODES", "MARKET_REGIMES", "STOCK_POOLS", "SUPPORTED_OPERATORS", "WEIGHT_SOURCES",
    "StrategyProfileRepository", "factor_catalog",
]
