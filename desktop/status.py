"""概览页数据：健康检查 + 关键产物统计。仅用轻量依赖（标准库 + 配置/快照读取）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from config.settings import (
    BASE_DIR,
    KB_DB_PATH,
    LLM_CONFIG,
    OUTPUT_DIR,
    SNAPSHOT_DIR,
    TUSHARE_TOKEN,
    WEB_DATA_DIR,
)
from snapshot.reader import SnapshotReader

DRAGON_POOL_PATH = Path(BASE_DIR) / "dragon_pools.json"
LOG_PATH = Path(BASE_DIR) / "logs" / "system.log"


def _ok(passed: bool) -> str:
    return "ok" if passed else "warn"


def _read_dragon_pools() -> Dict[str, Any]:
    try:
        if DRAGON_POOL_PATH.exists():
            return json.loads(DRAGON_POOL_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def health_items() -> List[Dict[str, Any]]:
    """返回若干健康检查项，供概览页渲染（状态 ok / warn）。"""
    reader = SnapshotReader(SNAPSHOT_DIR)
    dates = reader.list_dates()
    latest = reader.latest()

    pools = _read_dragon_pools()
    dragon_n = len(pools.get("dragon_pool", {}) or {})
    weak_n = len(pools.get("weakening_pool", {}) or {})

    token_ok = bool((TUSHARE_TOKEN or "").strip())
    ai_key = (LLM_CONFIG.get("api_key") or "").strip()
    ai_ok = bool(ai_key) and ai_key != "your-api-key-here"

    items: List[Dict[str, Any]] = [
        {
            "title": "Tushare Token",
            "status": _ok(token_ok),
            "value": "已配置" if token_ok else "未配置",
            "detail": "收盘分析需要 Tushare 历史数据接口；可在「参数配置」或 .env 中设置 TUSHARE_TOKEN。",
            "badge": "正常" if token_ok else "缺失",
        },
        {
            "title": "AI 解读 / 问答",
            "status": _ok(ai_ok),
            "value": "已配置" if ai_ok else "未配置（降级为离线词法检索）",
            "detail": "配置 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY 后启用 AI 每日解读与问答。",
            "badge": "正常" if ai_ok else "可选",
        },
        {
            "title": "最新快照",
            "status": _ok(latest is not None),
            "value": (f"{latest}（共 {len(dates)} 天）" if latest else "暂无快照"),
            "detail": f"快照目录：{SNAPSHOT_DIR}",
            "badge": "已生成" if latest else "缺失",
        },
        {
            "title": "龙头池",
            "status": _ok(DRAGON_POOL_PATH.exists()),
            "value": (
                f"龙头 {dragon_n} 只 · 走弱 {weak_n} 只"
                if DRAGON_POOL_PATH.exists()
                else "暂无 dragon_pools.json"
            ),
            "detail": f"更新时间：{pools.get('update_time', '—')}",
            "badge": "已就绪" if DRAGON_POOL_PATH.exists() else "缺失",
        },
        {
            "title": "数据目录",
            "status": _ok(Path(WEB_DATA_DIR).exists()),
            "value": str(WEB_DATA_DIR),
            "detail": f"报告输出：{OUTPUT_DIR} · 知识库：{KB_DB_PATH}",
            "badge": "存在" if Path(WEB_DATA_DIR).exists() else "缺失",
        },
    ]
    return items


def overview() -> Dict[str, Any]:
    from desktop.runner import CONTROLLER

    reader = SnapshotReader(SNAPSHOT_DIR)
    pools = _read_dragon_pools()
    return {
        "checks": health_items(),
        "latest": reader.latest(),
        "snapshot_count": len(reader.list_dates()),
        "dragon_count": len(pools.get("dragon_pool", {}) or {}),
        "weak_count": len(pools.get("weakening_pool", {}) or {}),
        "pool_update_time": pools.get("update_time", ""),
        "run": {
            "state": CONTROLLER.state,
            "date": CONTROLLER.date,
            "started_at": CONTROLLER.started_at,
            "finished_at": CONTROLLER.finished_at,
            "error": CONTROLLER.error,
        },
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base_dir": str(BASE_DIR),
    }


def tail_log(lines: int = 500) -> str:
    """读取 system.log 末尾若干行（从文件尾部读约 512KB，避免读超大文件）。"""
    try:
        if not LOG_PATH.exists():
            return ""
        size = LOG_PATH.stat().st_size
        chunk = min(size, 512 * 1024)
        with open(LOG_PATH, "rb") as f:
            if size > chunk:
                f.seek(size - chunk)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        rows = text.splitlines()
        n = max(1, min(int(lines or 500), 5000))
        return "\n".join(rows[-n:])
    except Exception as exc:  # noqa: BLE001
        return f"[读取日志失败] {exc!r}"


def dragon_pools() -> Dict[str, Any]:
    """返回龙头池/走弱池的精简表格（供龙头池页面渲染）。"""

    # 枚举名 → 中文展示（dragon_pools.json 里存的是 "DragonType.TREND" / "TREND" 形式）
    _ENUM_ZH = {
        "CONTINUOUS": "连板龙头", "TREND": "趋势龙头", "SPACE": "空间龙头",
        "MONITORING": "观察中", "WEAKENING": "已走弱", "RECOVERING": "转强中", "EXPIRED": "已过期",
    }

    def _clean(v: Any) -> Any:
        if isinstance(v, str) and "." in v and v.split(".")[0] in ("DragonType", "DragonStatus"):
            v = v.split(".", 1)[1]
        if isinstance(v, str):
            return _ENUM_ZH.get(v, v)
        return v

    def _stat_interval(entry_date: Any) -> str:
        """统计区间：与「10日涨幅/涨停数」同口径——截至入池日的最近 10 个交易日。

        返回 ``MM-DD~MM-DD``；交易日历不可用或日期非法时返回空串。
        """
        d = str(entry_date or "")[:8]
        if len(d) != 8 or not d.isdigit():
            return ""
        try:
            from core.utils.date_utils import get_last_n_trade_dates

            dates = get_last_n_trade_dates(10, d)  # 倒序，最新在前
            if not dates:
                return ""
            end, start = dates[0], dates[-1]
            return f"{start[4:6]}-{start[6:8]}~{end[4:6]}-{end[6:8]}"
        except Exception:
            return ""

    pools = _read_dragon_pools()

    def _rows(section: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for code, item in (pools.get(section, {}) or {}).items():
            out.append(
                {
                    "代码": code,
                    "名称": item.get("stock_name", ""),
                    "类型": _clean(item.get("dragon_type", "")),
                    "板块": item.get("sector_name", ""),
                    "涨停数": item.get("limit_up_count", ""),
                    "10日涨幅": (
                        f"{round(float(item.get('total_rise_10d', 0)) * 100, 1)}%"
                        if item.get("total_rise_10d") is not None
                        else ""
                    ),
                    "状态": _clean(item.get("status", "")),
                    "入池日": item.get("entry_date", ""),
                    "统计区间": _stat_interval(item.get("entry_date", "")),
                    "走弱类型": item.get("weakening_type", ""),
                    "走弱日": item.get("weakening_date", ""),
                }
            )
        return out

    return {
        "update_time": pools.get("update_time", ""),
        "dragon_rows": _rows("dragon_pool"),
        "weak_rows": _rows("weakening_pool"),
        "exists": DRAGON_POOL_PATH.exists(),
        "path": str(DRAGON_POOL_PATH),
    }