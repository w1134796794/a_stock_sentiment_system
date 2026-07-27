"""可编辑参数注册表 —— 把分散在几处的"参数赋值配置"统一抽象成网页可渲染、可校验、
可保存的字段集合。

作用域（scope）：
  settings  -> config.settings 模块的大写常量（标量 + 嵌套 dict），排除路径/密钥
  yaml      -> config_loader 管理的全部 YAML（情绪周期 / 板块 / 因子等）
  risk      -> risk.risk_config.RiskConfig 字段

对外提供：
  build_registry()                  构建分组字段树（含有效值/默认值/是否覆盖）
  apply_updates(updates)            校验+写入覆盖并使 web 进程即时反映
  reset(scope=None, path=None)      重置某项/某作用域/全部
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from config import overrides as ov

# ---------------------------------------------------------------------------
# 排除规则（密钥 / 路径，不开放到网页）
# ---------------------------------------------------------------------------
_SECRET_SUBSTR = ("token", "secret", "key", "password", "app_id", "appid", "cookie")
_PATH_SUFFIX = ("_DIR", "_PATH", "_FILE")
_SKIP_SETTINGS = {"BASE_DIR", "DATA_DIR", "CACHE_DIR", "OUTPUT_DIR",
                  "WEB_DATA_DIR", "INDUSTRY_MAPPING_FILE", "TRADE_CALENDAR_FILE"}

# settings 顶层分组中文标签
_SETTINGS_LABELS = {
    "_base": "数据获取 / 基础",
    "THS_SECTOR_CONFIG": "同花顺板块追踪",
    "WECHAT_CONFIG": "微信公众号",
    "LLM_CONFIG": "大模型接口",
}

_YAML_LABELS = {
    "emotion_cycle": "情绪周期",
    "sector_tracker": "板块追踪器",
    "factor_registry": "因子注册表",
    "layer1_market_env": "因子·大盘环境",
    "emotion_cycle_factors": "因子·情绪周期",
    "layer2_sector": "因子·板块",
    "layer3_stock_select": "因子·选股",
    "layer4_trade_plan": "因子·交易计划",
}

_RISK_GROUP = "risk_control"
_RISK_LABEL = "风控参数 (RiskConfig)"
_RISK_DESCRIPTIONS = {
    "hard_stop_loss": "相对买入成本的硬止损比例。",
    "min_open_gap": "仅原固定开盘区间对照模式使用的最小高开比例。",
    "max_open_gap": "仅原固定开盘区间对照模式使用的最大高开比例。",
    "market_entry_threshold": "低于该市场评分时停止新开仓。",
    "market_strong_threshold": "达到该市场评分后才允许扩展至候选前 N 名。",
    "neutral_market_max_rank": "中性市场每日最多执行到的候选名次。",
    "trailing_activation": "持仓最高涨幅达到该比例后，开始保护利润。",
    "trailing_stop": "激活后，相对持仓最高价回撤达到该比例时全部止盈。",
    "time_stop_days": "达到该持仓交易日数且收益仍低于阈值时退出。",
    "time_stop_profit_threshold": "时间止损使用的最低收益阈值。",
}

_SIMPLE_SETTINGS = {
    "DUCKDB_MEMORY_LIMIT",
    "DUCKDB_THREADS",
    "REDIS_URL",
}

_SIMPLE_RISK = {
    "enabled",
    "initial_capital",
    "max_positions",
    "max_position_per_stock",
    "max_total_position",
    "max_sector_concentration",
    "fixed_risk_per_trade",
    "hard_stop_loss",
    "trailing_activation",
    "trailing_stop",
    "time_stop_days",
    "slippage",
    "commission_rate",
    "stamp_duty_rate",
    "market_entry_threshold",
    "market_strong_threshold",
}

_FIELD_LABELS = {
    "DUCKDB_MEMORY_LIMIT": "数据库内存上限",
    "DUCKDB_THREADS": "数据库线程数",
    "REDIS_URL": "Redis连接地址",
    "enabled": "启用风控",
    "initial_capital": "初始资金",
    "max_positions": "最多持仓只数",
    "max_position_per_stock": "单票仓位上限",
    "max_total_position": "总仓位上限",
    "max_sector_concentration": "单一板块仓位上限",
    "fixed_risk_per_trade": "单笔风险预算",
    "hard_stop_loss": "硬止损比例",
    "trailing_activation": "移动止盈启动涨幅",
    "trailing_stop": "高点回撤退出比例",
    "time_stop_days": "最长观察天数",
    "slippage": "模拟滑点",
    "commission_rate": "佣金比例",
    "stamp_duty_rate": "印花税比例",
    "market_entry_threshold": "停止开仓市场分",
    "market_strong_threshold": "强市市场分",
}


# ---------------------------------------------------------------------------
# 类型推断 / 强制转换
# ---------------------------------------------------------------------------
def _infer_type(v: Any) -> Optional[str]:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (list, tuple)):
        return "list"
    return None  # dict / 其它结构不作为叶子字段


def _coerce_scalar(text: str) -> Any:
    """把字符串元素转成 int/float/str（用于 list 元素）。"""
    s = text.strip()
    try:
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except (ValueError, TypeError):
        return s


def coerce_value(t: str, raw: Any) -> Any:
    """按字段类型把网页传入的原始值规范化。"""
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if t == "int":
        return int(round(float(raw)))
    if t == "float":
        return float(raw)
    if t == "str":
        return str(raw)
    if t == "list":
        if isinstance(raw, list):
            return [(_coerce_scalar(x) if isinstance(x, str) else x) for x in raw]
        return [_coerce_scalar(x) for x in str(raw).split(",") if x.strip() != ""]
    return raw


def _is_secret_leaf(key: str) -> bool:
    k = str(key).lower()
    return any(s in k for s in _SECRET_SUBSTR)


# ---------------------------------------------------------------------------
# 字典扁平化：产出 (dotted_leaf_path, value) 列表（仅标量 / 列表叶子）
# ---------------------------------------------------------------------------
def _flatten(prefix: str, value: Any, drop_secret: bool = False) -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    if isinstance(value, dict):
        for k, v in value.items():
            if drop_secret and _is_secret_leaf(k):
                continue
            child = f"{prefix}.{k}" if prefix else str(k)
            out.extend(_flatten(child, v, drop_secret))
    else:
        if _infer_type(value) is not None:
            out.append((prefix, value))
    return out


def _field(scope: str, group: str, group_label: str, path: str, leaf: str,
           value: Any, default: Any, overridden: bool) -> Dict[str, Any]:
    return {
        "scope": scope,
        "group": group,
        "group_label": group_label,
        "path": path,                     # 作用域内的 dotted 路径（即覆盖键）
        "key": leaf,
        "desc": "",
        "type": _infer_type(value) or "str",
        "value": value,
        "default": default,
        "overridden": overridden,
    }


# ---------------------------------------------------------------------------
# 各作用域构建
# ---------------------------------------------------------------------------
def _build_settings() -> List[Dict[str, Any]]:
    import config.settings as s

    store = ov.load_overrides().get("settings", {})
    groups: Dict[str, Dict[str, Any]] = {}

    def ensure(group: str) -> Dict[str, Any]:
        if group not in groups:
            label = _SETTINGS_LABELS.get(group, group)
            groups[group] = {"key": group, "label": label, "fields": []}
        return groups[group]

    from pathlib import Path as _Path

    base_fields: List[Dict[str, Any]] = []
    for name in sorted(vars(s).keys()):
        if not name.isupper() or name in _SKIP_SETTINGS:
            continue
        if any(name.endswith(suf) for suf in _PATH_SUFFIX):
            continue
        value = getattr(s, name)
        if isinstance(value, _Path):
            continue
        t = _infer_type(value)
        if t is not None and t != "list":
            if _is_secret_leaf(name):
                continue
            default = ov.get_settings_default(name, value)
            base_fields.append(_field("settings", "_base", _SETTINGS_LABELS["_base"],
                                      name, name, value, default, name in store))
        elif t == "list":
            default = ov.get_settings_default(name, value)
            base_fields.append(_field("settings", "_base", _SETTINGS_LABELS["_base"],
                                      name, name, list(value), list(default), name in store))
        elif isinstance(value, dict):
            grp = ensure(name)
            for dotted, leaf_val in _flatten("", value, drop_secret=True):
                full = f"{name}.{dotted}"
                default = ov.get_settings_default(full, leaf_val)
                grp["fields"].append(_field("settings", name,
                                            _SETTINGS_LABELS.get(name, name),
                                            full, dotted, leaf_val, default,
                                            full in store))

    ordered: List[Dict[str, Any]] = []
    if base_fields:
        ordered.append({"key": "_base", "label": _SETTINGS_LABELS["_base"],
                        "fields": base_fields})
    for name in groups:
        if groups[name]["fields"]:
            ordered.append(groups[name])
    return ordered


def _build_yaml() -> List[Dict[str, Any]]:
    from config.config_loader import get_config_loader

    loader = get_config_loader()
    store = ov.load_overrides().get("yaml", {})
    out: List[Dict[str, Any]] = []
    for name in loader.loaded_config_names():
        cfg = loader.get_config(name)
        if not isinstance(cfg, dict) or not cfg:
            continue
        pristine = loader.pristine_config(name)
        leaves = _flatten("", cfg)
        # factor_registry.yaml 绝大多数是因子「元数据」(name/description/data_source/
        # value_range…)，并非可调参数。这里只保留 enabled 开关：既供「因子面板」写入校验，
        # 又把 ~540 个无意义的可编辑字段从参数页清掉（因子开关请走 /factors 面板）。
        if name == "factor_registry":
            leaves = [(d, v) for d, v in leaves if d.endswith(".enabled")]
        fields: List[Dict[str, Any]] = []
        for dotted, eff_val in leaves:
            full = f"{name}.{dotted}"
            default = ov.get_dotted(pristine, dotted, eff_val)
            fields.append(_field("yaml", name, _YAML_LABELS.get(name, name),
                                 full, dotted, eff_val, default, full in store))
        if fields:
            out.append({"key": name, "label": _YAML_LABELS.get(name, name),
                        "fields": fields})
    return out


def _build_risk() -> List[Dict[str, Any]]:
    from risk.risk_config import RiskConfig

    defaults = RiskConfig().to_dict()
    store = ov.load_overrides().get("risk", {})
    fields: List[Dict[str, Any]] = []
    for key in sorted(defaults.keys()):
        default_val = defaults[key]
        if _infer_type(default_val) is None:
            continue
        eff_val = store.get(key, default_val)
        item = _field("risk", _RISK_GROUP, _RISK_LABEL, key, key,
                      eff_val, default_val, key in store)
        item["desc"] = _RISK_DESCRIPTIONS.get(key, "")
        fields.append(item)
    return [{"key": _RISK_GROUP, "label": _RISK_LABEL, "fields": fields}] if fields else []


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
_SECTION_META = [
    ("settings", "系统设置 (settings.py)", _build_settings),
    ("yaml", "YAML 配置 (情绪/板块/因子)", _build_yaml),
    ("risk", "风控参数 (risk_control)", _build_risk),
]


def build_registry() -> Dict[str, Any]:
    sections = []
    override_count = 0
    store = ov.load_overrides()
    for scope, label, builder in _SECTION_META:
        try:
            groups = builder()
        except Exception as e:  # pragma: no cover - 单个作用域失败不影响整体
            groups = []
        sections.append({"scope": scope, "label": label, "groups": groups})
    for scope in ov.SCOPES:
        sc = store.get(scope, {})
        override_count += _count_leaves(sc)
    return {"sections": sections, "override_count": override_count}


def build_simple_registry() -> Dict[str, Any]:
    """Return the small set of parameters needed for normal operation."""
    registry = build_registry()
    sections: List[Dict[str, Any]] = []
    allowed = {
        "settings": _SIMPLE_SETTINGS,
        "risk": _SIMPLE_RISK,
    }
    section_labels = {
        "settings": "运行资源",
        "risk": "账户与风控",
    }
    group_labels = {
        "settings": "数据库与缓存",
        "risk": "仓位、退出与成本",
    }
    for section in registry["sections"]:
        scope = section["scope"]
        if scope not in allowed:
            continue
        fields = []
        for group in section["groups"]:
            for item in group["fields"]:
                if item["path"] not in allowed[scope]:
                    continue
                field = dict(item)
                field["display_name"] = _FIELD_LABELS.get(item["key"], item["key"])
                fields.append(field)
        if fields:
            sections.append({
                "scope": scope,
                "label": section_labels[scope],
                "groups": [{
                    "key": f"simple_{scope}",
                    "label": group_labels[scope],
                    "fields": fields,
                }],
            })
    return {
        "sections": sections,
        "override_count": sum(
            1
            for section in sections
            for group in section["groups"]
            for field in group["fields"]
            if field["overridden"]
        ),
    }


def _count_leaves(d: Any) -> int:
    if not isinstance(d, dict):
        return 0
    n = 0
    for v in d.values():
        if isinstance(v, dict):
            n += _count_leaves(v)
        else:
            n += 1
    return n


def _index_fields() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """构建 (scope, path) -> field 索引，用于保存时校验/取类型/取默认值。"""
    idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
    reg = build_registry()
    for sec in reg["sections"]:
        for grp in sec["groups"]:
            for f in grp["fields"]:
                idx[(f["scope"], f["path"])] = f
    return idx


def apply_updates(updates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """校验并写入一批改动。

    updates: [{scope, path, value}, ...]
    规则：值等于默认值时清除该覆盖（保持存储干净 + 语义=重置）；否则写入。
    返回 {applied, skipped, errors}。
    """
    idx = _index_fields()
    applied, errors = [], []
    skipped = 0
    for upd in updates or []:
        scope = upd.get("scope")
        path = upd.get("path")
        raw = upd.get("value")
        field = idx.get((scope, path))
        if field is None:
            errors.append({"path": path, "error": "未知或不可编辑字段"})
            continue
        try:
            value = coerce_value(field["type"], raw)
        except (ValueError, TypeError) as e:
            errors.append({"path": path, "error": f"类型错误({field['type']}): {e}"})
            continue
        if value == field["default"]:
            ov.clear_override(scope, path)
            skipped += 1
        else:
            ov.set_override(scope, path, value)
            applied.append({"scope": scope, "path": path, "value": value})
    _reapply_live()
    return {"applied": applied, "skipped": skipped, "errors": errors}


def reset(scope: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
    """重置：指定 path 单项；或指定 scope 整组；或全部。"""
    if path and scope:
        ov.clear_override(scope, path)
    else:
        ov.clear_scope(scope)  # scope=None 时清空全部
    _reapply_live()
    return {"ok": True}


def _reapply_live() -> None:
    """让正在运行的 web 进程即时反映最新覆盖（settings 模块 + config_loader）。

    分析流水线为独立进程，下次跑批读取覆盖文件自动生效；此处仅保证 /config
    页面回显与 web 进程内的读取一致。
    """
    try:
        import config.settings as s
        ov.apply_settings_overrides(vars(s))
    except Exception:
        pass
    try:
        from config.config_loader import get_config_loader
        get_config_loader().reload_config()
    except Exception:
        pass
