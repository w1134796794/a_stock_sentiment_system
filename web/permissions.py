"""Menu and route permission rules for the web app."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

from web.auth_store import (
    get_role_permission_overrides,
    reset_role_permission_overrides,
    save_role_permission_overrides,
)

Role = str

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ALL_ROLES = (ROLE_ADMIN, ROLE_VIEWER)
ADMIN_ONLY = (ROLE_ADMIN,)


def _icon(svg: str) -> str:
    return svg.strip()


MENU_GROUPS: List[Dict[str, Any]] = [
    {
        "label": "",
        "items": [
            {
                "key": "overview",
                "label": "概览",
                "href": "/",
                "prefix": "/",
                "exact": True,
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>'),
            },
            {
                "key": "report",
                "label": "交易计划",
                "href": "/report",
                "prefix": "/report",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 17V9m4 8V5m4 12v-4"/><rect x="3" y="3" width="18" height="18" rx="2"/></svg>'),
            },
        ],
    },
    {
        "label": "数据浏览",
        "items": [
            {
                "key": "strategy",
                "label": "指标筛选",
                "href": "/data/strategy",
                "prefix": "/data/strategy",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/></svg>'),
            },
            {
                "key": "sector",
                "label": "板块热度",
                "href": "/data/sector",
                "prefix": "/data/sector",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5M3 17l9 5 9-5"/></svg>'),
            },
            {
                "key": "limitup",
                "label": "涨停数据",
                "href": "/data/limitup",
                "prefix": "/data/limitup",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6"/><rect x="12" y="8" width="3" height="10"/><rect x="17" y="4" width="3" height="14"/></svg>'),
            },
            {
                "key": "lhb",
                "label": "龙虎榜",
                "href": "/data/lhb",
                "prefix": "/data/lhb",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19V5"/><path d="M4 7h7l2 2h7v8h-7l-2-2H4"/><path d="M8 11h8"/></svg>'),
            },
            {
                "key": "dragon",
                "label": "龙头池",
                "href": "/dragon",
                "prefix": "/dragon",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 8-8"/><path d="M21 7v5h-5"/></svg>'),
            },
            {
                "key": "intraday",
                "label": "盘中转强",
                "href": "/intraday",
                "prefix": "/intraday",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'),
            },
            {
                "key": "realtime",
                "label": "实时行情",
                "href": "/realtime",
                "prefix": "/realtime",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h4l2-6 4 12 2-6h6"/><circle cx="5" cy="19" r="1"/><circle cx="12" cy="19" r="1"/><circle cx="19" cy="19" r="1"/></svg>'),
            },
        ],
    },
    {
        "label": "回测",
        "items": [
            {
                "key": "backtest",
                "label": "模拟交易",
                "href": "/backtest",
                "prefix": "/backtest",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M19 9l-5 5-3-3-4 4"/></svg>'),
            },
            {
                "key": "drawdown",
                "label": "回撤分析",
                "href": "/drawdown",
                "prefix": "/drawdown",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 4v16h18"/><path d="M7 8l4 6 3-3 4 5"/></svg>'),
            },
        ],
    },
    {
        "label": "系统",
        "items": [
            {
                "key": "users",
                "label": "用户管理",
                "href": "/admin/users",
                "prefix": "/admin/users",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>'),
            },
            {
                "key": "permissions",
                "label": "权限配置",
                "href": "/admin/permissions",
                "prefix": "/admin/permissions",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 9h8M8 13h5"/><path d="M17 12v4M15 14h4"/></svg>'),
            },
            {
                "key": "run",
                "label": "生成数据",
                "href": "/run",
                "prefix": "/run",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>'),
            },
            {
                "key": "config",
                "label": "参数配置",
                "href": "/config",
                "prefix": "/config",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-2.92.67 1.65 1.65 0 01-3.16 0 1.65 1.65 0 00-2.92-.67l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>'),
            },
            {
                "key": "factors",
                "label": "指标因子",
                "href": "/factors",
                "prefix": "/factors",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><circle cx="4" cy="12" r="2"/><circle cx="12" cy="10" r="2"/><circle cx="20" cy="14" r="2"/></svg>'),
            },
            {
                "key": "logs",
                "label": "日志",
                "href": "/logs",
                "prefix": "/logs",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>'),
            },
            {
                "key": "about",
                "label": "关于",
                "href": "/about",
                "prefix": "/about",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>'),
            },
        ],
    },
]

PUBLIC_PATHS = {"/login", "/logout", "/expired", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)

# Page-like paths and docs that must be admin only even for GET.
ADMIN_PAGE_PREFIXES = (
    "/admin",
    "/run",
    "/config",
    "/factors",
    "/logs",
    "/api/docs",
    "/openapi.json",
)

# GET APIs that expose admin state, config, logs, or task state.
ADMIN_GET_API_PREFIXES = (
    "/api/admin",
    "/api/run",
    "/api/logs",
    "/api/config",
    "/api/factors",
    "/api/backtest/run/status",
)

# These capabilities remain administrator-only regardless of database settings.
FORCED_ADMIN_KEYS = frozenset({"users", "permissions", "run", "config", "factors", "logs"})

PATH_PERMISSION_PREFIXES: Dict[str, tuple[str, ...]] = {
    "overview": ("/", "/api/overview"),
    "report": ("/report", "/api/etl/analysis"),
    "strategy": ("/data/strategy", "/api/etl/screening"),
    "sector": ("/data/sector",),
    "limitup": ("/data/limitup",),
    "lhb": ("/data/lhb",),
    "dragon": ("/dragon", "/api/leader-pool"),
    "intraday": ("/intraday", "/api/intraday-strength"),
    "realtime": ("/realtime", "/api/realtime"),
    "backtest": ("/backtest", "/api/backtest"),
    "drawdown": ("/drawdown",),
    "users": ("/admin/users", "/api/admin/users"),
    "permissions": ("/admin/permissions", "/api/admin/permissions"),
    "run": ("/run", "/api/run", "/api/etl/artifacts"),
    "config": ("/config", "/api/config"),
    "factors": ("/factors", "/api/factors"),
    "logs": ("/logs", "/api/logs"),
    "about": ("/about",),
}


def _permission_items() -> Dict[str, Dict[str, Any]]:
    return {
        str(item["key"]): item
        for group in MENU_GROUPS
        for item in group.get("items", [])
    }


PERMISSION_ITEMS = _permission_items()


def _role(user: Optional[Dict[str, Any]]) -> str:
    return str((user or {}).get("role") or "")


def _matches_prefix(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def is_api_path(path: str) -> bool:
    return path.startswith("/api/") or path == "/openapi.json"


def requires_admin(path: str, method: str = "GET") -> bool:
    if _matches_prefix(path, ADMIN_PAGE_PREFIXES):
        return True
    if is_api_path(path):
        if method.upper() != "GET":
            return True
        return _matches_prefix(path, ADMIN_GET_API_PREFIXES)
    return False


def _effective_permission(
    role: Role,
    item: Dict[str, Any],
    overrides: Optional[Dict[str, Dict[str, Dict[str, bool]]]] = None,
) -> Dict[str, bool]:
    key = str(item.get("key") or "")
    if key in FORCED_ADMIN_KEYS:
        allowed = role == ROLE_ADMIN
        return {"menu_visible": allowed, "can_access": allowed, "locked": True}

    default_allowed = role in tuple(item.get("roles") or ())
    source = overrides if overrides is not None else get_role_permission_overrides()
    saved = source.get(role, {}).get(key, {})
    can_access = bool(saved.get("can_access", default_allowed))
    menu_visible = bool(saved.get("menu_visible", default_allowed)) and can_access
    return {"menu_visible": menu_visible, "can_access": can_access, "locked": False}


def permission_key_for_path(path: str) -> Optional[str]:
    candidates: List[tuple[int, str]] = []
    for key, prefixes in PATH_PERMISSION_PREFIXES.items():
        for prefix in prefixes:
            if prefix == "/":
                matched = path == "/"
            else:
                matched = path == prefix or path.startswith(prefix + "/")
            if matched:
                candidates.append((len(prefix), key))
    return max(candidates, default=(0, ""))[1] or None


def can_access_path(user: Optional[Dict[str, Any]], path: str, method: str = "GET") -> bool:
    if is_public_path(path):
        return True
    if not user:
        return False
    role = _role(user)
    if role not in ALL_ROLES:
        return False
    if requires_admin(path, method) and role != ROLE_ADMIN:
        return False
    key = permission_key_for_path(path)
    if not key:
        return True
    item = PERMISSION_ITEMS.get(key)
    return bool(item and _effective_permission(role, item)["can_access"])


def visible_menu_groups(user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    role = _role(user)
    overrides = get_role_permission_overrides() if role in ALL_ROLES else {}
    groups: List[Dict[str, Any]] = []
    for group in MENU_GROUPS:
        items = [
            deepcopy(item)
            for item in group.get("items", [])
            if _effective_permission(role, item, overrides)["menu_visible"]
        ]
        if items:
            groups.append({"label": group.get("label", ""), "items": items})
    return groups


def permission_matrix() -> Dict[str, Any]:
    overrides = get_role_permission_overrides()
    groups: List[Dict[str, Any]] = []
    for group in MENU_GROUPS:
        rows: List[Dict[str, Any]] = []
        for item in group.get("items", []):
            row = {
                "key": item["key"],
                "label": item["label"],
                "href": item["href"],
                "forced_admin": item["key"] in FORCED_ADMIN_KEYS,
                "permissions": {},
            }
            for role in ALL_ROLES:
                row["permissions"][role] = _effective_permission(role, item, overrides)
            rows.append(row)
        groups.append({"label": group.get("label") or "常用", "items": rows})
    return {"roles": list(ALL_ROLES), "groups": groups}


def _permission_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} 必须是布尔值")


def update_permission_matrix(updates: Any) -> Dict[str, Any]:
    if not isinstance(updates, list):
        raise ValueError("updates 必须是数组")
    rows: List[Dict[str, Any]] = []
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("权限配置格式不正确")
        role = str(update.get("role") or "")
        key = str(update.get("permission_key") or "")
        if role not in ALL_ROLES or key not in PERMISSION_ITEMS:
            raise ValueError("角色或权限项不存在")
        menu_visible = _permission_bool(update.get("menu_visible"), "menu_visible")
        can_access = _permission_bool(update.get("can_access"), "can_access")
        if key in FORCED_ADMIN_KEYS:
            expected = role == ROLE_ADMIN
            if menu_visible != expected or can_access != expected:
                raise ValueError(f"{PERMISSION_ITEMS[key]['label']} 是固定管理员权限")
            continue
        rows.append(
            {
                "role": role,
                "permission_key": key,
                "menu_visible": menu_visible and can_access,
                "can_access": can_access,
            }
        )
    save_role_permission_overrides(rows)
    return permission_matrix()


def reset_permission_matrix() -> Dict[str, Any]:
    reset_role_permission_overrides()
    return permission_matrix()
