"""Product navigation groups existing pages without changing access controls."""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode

from web.permissions import PERMISSION_ITEMS, _effective_permission, can_access_path, get_role_permission_overrides

MODULES = (
    ("workspace", "交易工作台", "panels-top-left", (
        ("workspace", "交易总览", "/workspace", "workbench", "query"),
        ("leaders", "龙头池", "/dragon", "dragon", "query"),
        ("candidates", "候选池", "/data/strategy", "strategy", "path"),
        ("signals", "盘中确认", "/intraday", "intraday", "query"),
        ("quotes", "实时行情", "/realtime", "realtime", ""),
        ("evidence", "行情证据", "/market-evidence", "intraday", ""),
    )),
    ("market", "市场复盘", "chart-no-axes-combined", (
        ("limitup", "涨停梯队", "/data/limitup", "limitup", "path"),
        ("sectors", "板块主线", "/data/sector", "sector", "path"),
        ("lhb", "龙虎榜", "/data/lhb", "lhb", "path"),
    )),
    ("research", "策略研究", "sliders-horizontal", (
        ("screening", "选股任务", "/screening-run", "screening_run", ""),
        ("combinations", "策略组合", "/strategies", "strategies", ""),
        ("laboratory", "策略实验室", "/strategy-lab", "strategy_lab", ""),
        ("calculation", "因子计算", "/run", "run", ""),
        ("factors", "指标因子", "/factors", "factors", ""),
    )),
    ("validation", "回测验证", "flask-conical", (
        ("paper", "模拟交易", "/portfolio", "portfolio", ""),
        ("history", "历史回测", "/portfolio/backtest", "portfolio", ""),
        ("evaluation", "策略评估", "/strategy-evaluation", "strategy_evaluation", ""),
        ("drawdown", "回撤分析", "/drawdown", "drawdown", ""),
        ("replay", "诊断回放", "/intraday/replay", "intraday", ""),
    )),
    ("collection", "行情采集", "database", (
        ("collection", "行情采集中心", "/market-collection", "fetch", ""),
    )),
    ("settings", "系统设置", "settings-2", (
        ("configuration", "参数配置", "/config", "config", ""),
        ("users", "用户管理", "/admin/users", "users", ""),
        ("permissions", "权限配置", "/admin/permissions", "permissions", ""),
        ("logs", "运行日志", "/logs", "logs", ""),
        ("about", "关于", "/about", "about", ""),
    )),
)

ALIASES = {"/market-backfill": "/market-collection", "/fetch": "/market-collection", "/backtest": "/portfolio/backtest"}


def _date(value):
    text = str(value or "").replace("-", "")
    if not re.fullmatch(r"\d{8}", text):
        return ""
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return ""
    return text


def product_navigation(user, path, date=None, query=None):
    """Pure local metadata: no provider, data query, task or notification side effects."""
    query = query or {}
    selected_date = _date(date) or _date(query.get("date")) or _date(path.rsplit("/", 1)[-1])
    path = ALIASES.get(path, path)
    role = str((user or {}).get("role") or "")
    overrides = get_role_permission_overrides() if role in {"admin", "viewer"} else {}
    matches = [(len(tab[2]), module[0], tab[0]) for module in MODULES for tab in module[3]
               if path == tab[2] or (tab[2] != "/" and path.startswith(tab[2] + "/"))]
    _, current_module, current_tab = max(matches, default=(0, "", ""))
    modules = []
    for key, label, icon, entries in MODULES:
        tabs = []
        for tab_key, title, href, permission, date_style in entries:
            item = PERMISSION_ITEMS[permission]
            allowed = _effective_permission(role, item, overrides)["can_access"] and can_access_path(user, href)
            # Old "hidden from sidebar" defaults do not hide module tabs. Explicit user overrides still do.
            hidden = (overrides.get(role, {}).get(permission) or {}).get("menu_visible") is False
            if not allowed or (hidden and (current_module, current_tab) != (key, tab_key)):
                continue
            link = href
            if selected_date and key == current_module:
                if date_style == "path":
                    link += "/" + selected_date
                elif date_style == "query":
                    link += "?" + urlencode({"date": selected_date})
            tabs.append({"key": tab_key, "label": title, "href": link,
                         "active": (key, tab_key) == (current_module, current_tab)})
        if tabs:
            modules.append({"key": key, "label": label, "icon": icon, "href": tabs[0]["href"],
                            "active": key == current_module, "tabs": tabs})
    current = next((module for module in modules if module["active"]), None)
    return {"modules": modules, "current": current, "date": selected_date}
