"""Menu and route permission rules for the web app."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

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
                "label": "概览",
                "href": "/",
                "prefix": "/",
                "exact": True,
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>'),
            },
            {
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
                "label": "指标筛选",
                "href": "/data/strategy",
                "prefix": "/data/strategy",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/></svg>'),
            },
            {
                "label": "板块热度",
                "href": "/data/sector",
                "prefix": "/data/sector",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5M3 17l9 5 9-5"/></svg>'),
            },
            {
                "label": "涨停数据",
                "href": "/data/limitup",
                "prefix": "/data/limitup",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6"/><rect x="12" y="8" width="3" height="10"/><rect x="17" y="4" width="3" height="14"/></svg>'),
            },
            {
                "label": "龙头池",
                "href": "/dragon",
                "prefix": "/dragon",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 8-8"/><path d="M21 7v5h-5"/></svg>'),
            },
            {
                "label": "盘中转强",
                "href": "/intraday",
                "prefix": "/intraday",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'),
            },
            {
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
                "label": "模拟交易",
                "href": "/backtest",
                "prefix": "/backtest",
                "roles": ALL_ROLES,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M19 9l-5 5-3-3-4 4"/></svg>'),
            },
            {
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
                "label": "用户管理",
                "href": "/admin/users",
                "prefix": "/admin/users",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>'),
            },
            {
                "label": "生成数据",
                "href": "/run",
                "prefix": "/run",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="6 4 20 12 6 20 6 4"/></svg>'),
            },
            {
                "label": "参数配置",
                "href": "/config",
                "prefix": "/config",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-2.92.67 1.65 1.65 0 01-3.16 0 1.65 1.65 0 00-2.92-.67l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>'),
            },
            {
                "label": "指标因子",
                "href": "/factors",
                "prefix": "/factors",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><circle cx="4" cy="12" r="2"/><circle cx="12" cy="10" r="2"/><circle cx="20" cy="14" r="2"/></svg>'),
            },
            {
                "label": "日志",
                "href": "/logs",
                "prefix": "/logs",
                "roles": ADMIN_ONLY,
                "icon": _icon('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg>'),
            },
            {
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
    "/ask",
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


def can_access_path(user: Optional[Dict[str, Any]], path: str, method: str = "GET") -> bool:
    if is_public_path(path):
        return True
    if not user:
        return False
    if requires_admin(path, method):
        return _role(user) == ROLE_ADMIN
    return _role(user) in ALL_ROLES


def visible_menu_groups(user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    role = _role(user)
    groups: List[Dict[str, Any]] = []
    for group in MENU_GROUPS:
        items = [
            deepcopy(item)
            for item in group.get("items", [])
            if role in tuple(item.get("roles") or ())
        ]
        if items:
            groups.append({"label": group.get("label", ""), "items": items})
    return groups
