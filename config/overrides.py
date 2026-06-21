"""运行期配置覆盖中心（单一事实来源：webdata/config_overrides.json）。

把"网页上改的参数"统一落到一个 JSON 文件，并在各配置入口处套用。三个作用域：

  settings  -> config.settings 模块属性（在 settings.py 末尾、所有默认定义之后套用）
  yaml      -> config_loader 管理的 YAML 配置（加载/重载后套用）
  risk      -> RiskConfig 字段（RiskConfig.load 套用）

设计要点：
  - 本模块只依赖标准库，可被 settings.py 安全 import（绝无循环依赖）。
  - 保存即写文件；分析流水线（main.py / scheduler）下次运行时读取生效。
  - 对 settings 作用域，首次套用前快照默认值，使"重置 + 重套"能精确回到默认。
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Dict, List

_BASE = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = _BASE / "webdata" / "config_overrides.json"

SCOPES: tuple = ("settings", "yaml", "risk")
_lock = threading.RLock()

# settings 作用域默认值快照（进程级，首次套用时填充）
_settings_defaults: Dict[str, Any] = {}
_settings_captured = False


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def load_overrides() -> Dict[str, Any]:
    """读取覆盖文件，缺失/损坏时返回规范化空结构。"""
    data: Dict[str, Any]
    try:
        if OVERRIDES_PATH.exists():
            raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
        else:
            data = {}
    except Exception:
        data = {}
    for s in SCOPES:
        if not isinstance(data.get(s), dict):
            data[s] = {}
    return data


def save_overrides(data: Dict[str, Any]) -> None:
    """规范化后写回覆盖文件（只保留已知作用域）。"""
    with _lock:
        OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
        clean = {s: (data.get(s) or {}) for s in SCOPES}
        OVERRIDES_PATH.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# dotted 路径工具
# ---------------------------------------------------------------------------
def set_dotted(container: Dict[str, Any], dotted: str, value: Any) -> None:
    """在嵌套字典里按 dotted 路径写值，缺失层级自动建 dict。"""
    parts = dotted.split(".")
    cur = container
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def get_dotted(container: Any, dotted: str, default: Any = None) -> Any:
    cur = container
    for p in dotted.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur


def del_dotted(container: Dict[str, Any], dotted: str) -> None:
    """删除 dotted 路径，并清理因此变空的父级 dict。"""
    parts = dotted.split(".")
    stack: List = []
    cur = container
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return
        stack.append((cur, p))
        cur = cur[p]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key, None)


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """把 overlay 递归合并到 base 的深拷贝上并返回（不改动入参）。"""
    out = copy.deepcopy(base)

    def _merge(b: Dict[str, Any], o: Dict[str, Any]) -> None:
        for k, v in o.items():
            if isinstance(v, dict) and isinstance(b.get(k), dict):
                _merge(b[k], v)
            else:
                b[k] = copy.deepcopy(v)

    _merge(out, overlay)
    return out


# ---------------------------------------------------------------------------
# settings 作用域
# ---------------------------------------------------------------------------
def apply_settings_overrides(g: Dict[str, Any]) -> None:
    """把 settings 作用域覆盖套用到 settings 模块的全局字典 g。

    首次调用时快照全部大写常量为默认值；之后每次调用都"先恢复默认、再套用覆盖"，
    这样删除某项覆盖（重置）后再调用即可精确回到默认值。
    """
    global _settings_captured
    with _lock:
        if not _settings_captured:
            for k, v in list(g.items()):
                if k.isupper():
                    try:
                        _settings_defaults[k] = copy.deepcopy(v)
                    except Exception:
                        pass
            _settings_captured = True

        # 1) 恢复默认（确保重置项回退）
        for k, v in _settings_defaults.items():
            g[k] = copy.deepcopy(v)

        # 2) 套用覆盖
        ov = load_overrides().get("settings", {})
        for dotted, value in ov.items():
            head, _, rest = dotted.partition(".")
            if head not in g:
                continue
            if not rest:
                g[head] = value
            elif isinstance(g[head], dict):
                set_dotted(g[head], rest, value)


def get_settings_default(dotted: str, default: Any = None) -> Any:
    """读取 settings 默认值快照里的 dotted 路径值（重置/比较用）。"""
    head, _, rest = dotted.partition(".")
    if head not in _settings_defaults:
        return default
    if not rest:
        return copy.deepcopy(_settings_defaults[head])
    return copy.deepcopy(get_dotted(_settings_defaults[head], rest, default))


# ---------------------------------------------------------------------------
# yaml 作用域
# ---------------------------------------------------------------------------
def apply_yaml_overrides(configs: Dict[str, Any]) -> None:
    """把 yaml 作用域覆盖就地套用到 config_loader 的 _configs。"""
    ov = load_overrides().get("yaml", {})
    for dotted, value in ov.items():
        head, _, rest = dotted.partition(".")
        cfg = configs.get(head)
        if isinstance(cfg, dict) and rest:
            set_dotted(cfg, rest, value)


# ---------------------------------------------------------------------------
# risk 作用域
# ---------------------------------------------------------------------------
def apply_risk_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """把 risk 作用域覆盖合并到 RiskConfig 的原始 dict（顶层字段）上。"""
    ov = load_overrides().get("risk", {})
    if not ov:
        return dict(data or {})
    out = dict(data or {})
    out.update(ov)
    return out


# ---------------------------------------------------------------------------
# 通用：设置/清除单个覆盖项
# ---------------------------------------------------------------------------
def set_override(scope: str, path: str, value: Any) -> None:
    """写入一个覆盖项并落盘。"""
    if scope not in SCOPES:
        raise ValueError(f"未知作用域: {scope}")
    with _lock:
        data = load_overrides()
        data[scope][path] = value
        save_overrides(data)


def clear_override(scope: str, path: str) -> None:
    """删除一个覆盖项并落盘。"""
    if scope not in SCOPES:
        raise ValueError(f"未知作用域: {scope}")
    with _lock:
        data = load_overrides()
        data[scope].pop(path, None)
        save_overrides(data)


def clear_scope(scope: str | None = None) -> None:
    """清空某作用域（或全部）覆盖并落盘。"""
    with _lock:
        if scope is None:
            save_overrides({})
            return
        if scope not in SCOPES:
            raise ValueError(f"未知作用域: {scope}")
        data = load_overrides()
        data[scope] = {}
        save_overrides(data)
