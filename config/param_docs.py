"""参数中文说明：为配置页每个字段提供一句中文解释。

来源（按作用域）：
  - patterns：集中维护的 PATTERN_DESC（原硬编码注释迁移而来）
  - settings：从 config/settings.py 行内注释自动抽取（与源码同步，免重复维护）
  - yaml    ：从对应 YAML 文件行内注释自动抽取
  - risk    ：从 risk/risk_config.py 字段行内注释自动抽取

抽取失败/缺注释时返回空串，页面只显示参数名，不影响功能。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict

_CFG_DIR = Path(__file__).resolve().parent
_BASE = _CFG_DIR.parent

# ---------------------------------------------------------------------------
# 策略 / 打分 / 大盘参数中文说明（group -> leaf -> 说明）
# ---------------------------------------------------------------------------
PATTERN_DESC: Dict[str, Dict[str, str]] = {
    "dragon_second_wave": {
        "recent_days": "近期记忆天数（蓄势窗口）",
        "max_adjust_days": "最大调整天数（见顶后允许的调整时长）",
        "min_adjust_days": "最小调整天数（见顶后至少调整几个交易日）",
        "max_break_days": "允许断板天数",
        "min_adjust_depth": "最小调整深度（强势调整）",
        "max_adjust_depth": "最大调整深度（容忍的最深回调）",
        "ma10_tolerance": "回踩 MA10 的容忍度",
        "use_ma20_fallback": "MA10 不满足时是否用 MA20 备选",
        "min_first_wave": "第一波最少连板数（轨道1）",
        "max_first_wave": "第一波连板上限",
        "min_rise_5d": "轨道2：5日累计涨幅下限",
        "min_rise_10d": "轨道2：10日累计涨幅下限",
        "min_limit_up_count": "轨道3：10天内最少涨停次数",
        "min_volume_ratio": "启动量能最小倍数（资金记忆苏醒）",
        "min_seal_ratio": "最小封单强度（市场认可）",
        "volume_abs_max": "量能绝对上限倍数（防异常放量）",
        "max_limit_up_time": "二波最晚封板时间",
        "max_float_cap": "流通市值上限（亿）",
        "max_5d_rise": "5日涨幅上限（低位启动）",
        "max_break_count": "开板次数上限",
    },
    "weak_to_strong": {
        "min_board_height": "连板龙头最少连板数",
        "min_total_rise": "趋势龙头近10日最小涨幅",
        "min_limit_up_count": "趋势龙头期间最少涨停数",
        "max_limit_up_gap": "趋势龙头涨停最大间隔天数",
        "min_slope_daily": "趋势龙头日均最小斜率",
        "min_r_squared": "趋势龙头最小 R²（拟合优度）",
        "min_board_height_for_space": "空间龙头最少连板数（之后回撤）",
        "weakening_types": "走弱类型清单（命中即视为走弱）",
        "max_drawdown_for_recovery": "最大允许回调幅度（防 A 杀）",
        "max_monitor_days": "最长观察天数",
        "min_gap": "最小高开幅度",
        "ideal_gap": "理想高开幅度",
        "max_gap": "最大高开幅度（避免追高）",
        "min_auction_vol_ratio": "最小竞价量比",
        "ideal_auction_vol_ratio": "理想竞价量比",
        "min_auction_amount": "最小竞价金额（元）",
        "max_open_drop": "开盘最大回踩幅度",
        "max_time_to_limit": "涨停最迟用时（分钟）",
        "enable_flexible_scoring": "是否启用弹性评分",
        "market_sentiment_weight": "市场情绪权重",
        "sector_strength_weight": "板块强度权重",
        "stock_momentum_weight": "个股动量权重",
        "dynamic_params_enabled": "是否启用动态参数调整",
        "sentiment_bullish_boost": "牛市情绪下阈值下调幅度",
        "sentiment_bearish_penalty": "熊市情绪下阈值上调幅度",
    },
    "second_board_dragon": {
        "min_seal_ratio": "最小封单额占流通市值比",
        "ideal_turnover": "理想换手率区间(%)",
        "min_concept_heat": "首板当日同概念最少涨停数",
        "min_gap": "次日最小高开",
        "max_gap": "次日最大高开",
        "min_auction_vol": "最小竞价量比",
        "min_auction_amount": "最小竞价金额（元）",
        "max_time_to_limit": "涨停最迟用时（分钟）",
        "min_seal_growth": "封单最小增长（尾盘相对开盘）",
        "max_sector_second_board": "同板块最多二板数",
    },
    "second_board_dragon_strict": {
        "min_gap": "竞价最小高开",
        "max_gap": "竞价最大高开",
        "min_auction_vol": "最小竞价量比",
        "max_auction_vol": "最大竞价量比",
        "max_limit_up_time": "最晚封板时间",
        "min_turnover": "最小实际换手率",
        "min_sector_first_board": "同板块最少首板助攻数",
        "skip_first_board_seal": "首板一字板是否放弃",
        "skip_tail_board_time": "尾盘二板放弃时间",
    },
    "first_board_breakout": {
        "max_5d_rise": "近5日涨幅上限（低位要求）",
        "max_float_cap": "流通市值上限（亿）",
        "hot_sector_heat_threshold": "板块3日涨停数下限（确认热点）",
        "fast_limit_max_time": "早盘秒封最晚时间",
        "max_break_count": "开板次数上限",
        "min_sector_limit_up": "板块最少涨停家数（避免独狼板）",
        "skip_tail_board_time": "尾盘板放弃时间",
        "volume_max_ratio": "量能上限倍数",
        "volume_abs_max": "量能绝对上限倍数",
        "platform_days_min": "横盘最少天数",
        "platform_days_max": "横盘最多天数",
        "max_distance_from_high": "距前高最大距离",
    },
    "multi_factor_weights": {
        "pattern_quality": "模式质量权重",
        "sector_strength": "板块强度权重",
        "stock_position": "个股地位权重",
        "emotion_fit": "情绪适配权重",
    },
    "layer1_index_trend_weights": {
        "sh": "上证指数权重",
        "sz": "深证成指权重",
        "cyb": "创业板指权重",
        "kcb": "科创50权重",
        "bj": "北证50权重",
    },
    "layer1_weights": {
        "trend": "趋势得分权重",
        "volume": "量能得分权重",
        "width": "市场宽度得分权重",
    },
    "layer1_trend_params": {
        "ma_short": "短期均线周期",
        "ma_mid": "中期均线周期",
        "ma_long": "长期均线周期",
        "bull_threshold": "多头阈值（指数在 MA20 上方比例）",
        "bear_threshold": "空头阈值（指数在 MA20 下方比例）",
    },
    "layer1_volume_params": {
        "expand_ratio": "放量阈值（量比）",
        "shrink_ratio": "缩量阈值（量比）",
        "lookback_days": "均量计算天数",
    },
    "layer1_width_params": {
        "strong_threshold": "强势阈值（上涨家数占比）",
        "normal_threshold": "正常阈值（上涨家数占比）",
        "weak_threshold": "弱势阈值（上涨家数占比）",
    },
}

# settings 基础标量补充说明（源码无行内注释时的兜底）
_SETTINGS_FALLBACK: Dict[str, str] = {
    "TRADE_HOUR": "跑批时刻 · 小时",
    "TRADE_MINUTE": "跑批时刻 · 分钟",
}


# ---------------------------------------------------------------------------
# 行内注释抽取
# ---------------------------------------------------------------------------
def _strip_comment(line: str):
    """返回 (代码部分, 注释文本)；注释取第一个 ' #' 之后内容。"""
    m = re.search(r"#\s*(.*)$", line)
    if not m:
        return line.rstrip(), ""
    return line[: m.start()].rstrip(), m.group(1).strip()


def _clean_comment(text: str) -> str:
    """清理分隔型注释，如 '---- 趋势因子 ----' / '==== X ===='。"""
    return re.sub(r"^[\-=\s]+|[\-=\s]+$", "", text).strip()


def _extract_yaml_comments(path: Path) -> Dict[str, str]:
    """按缩进栈解析 YAML，产出 {dotted: 注释}。

    既取行尾内联注释，也取键上方的整行注释（因子配置多用分块标题注释），
    并为父级键也记录注释，供叶子向上回退查找。
    """
    res: Dict[str, str] = {}
    if not path.exists():
        return res
    stack = []  # (indent, key)
    pending = ""  # 最近一条整行注释（清理后）
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            c = _clean_comment(s.lstrip("#"))
            if c:
                pending = c
            continue
        indent = len(raw) - len(raw.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()
        code, inline = _strip_comment(raw)
        m = re.match(r"^\s*(?:-\s*)?[\"']?([\w\u4e00-\u9fff]+)[\"']?\s*:\s*(.*)$", code)
        if not m:
            pending = ""
            continue
        key, val = m.group(1), m.group(2).strip()
        dotted = ".".join([st[1] for st in stack] + [key])
        comment = inline or pending
        if comment:
            res[dotted] = comment
        pending = ""
        if val == "":
            stack.append((indent, key))
    return res


def _extract_settings_comments() -> Dict[str, str]:
    """解析 config/settings.py 顶层常量与字典字面量，产出 {dotted: 注释}。"""
    res: Dict[str, str] = {}
    path = _CFG_DIR / "settings.py"
    if not path.exists():
        return res
    stack = []  # (indent, key)
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()
        code, comment = _strip_comment(raw)
        mtop = re.match(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", code)
        if indent == 0 and mtop:
            name, rest = mtop.group(1), mtop.group(2).rstrip()
            if rest.endswith("{"):
                stack = [(indent, name)]
            else:
                stack = []
                if comment:
                    res[name] = comment
            continue
        ment = re.match(r"^\s*[\"']?([\w\u4e00-\u9fff]+)[\"']?\s*:\s*(.*)$", code)
        if ment and stack:
            key, rest = ment.group(1), ment.group(2).rstrip()
            dotted = ".".join([s[1] for s in stack] + [key])
            if rest.endswith("{"):
                stack.append((indent, key))
            elif comment:
                res[dotted] = comment
    return res


def _extract_risk_comments() -> Dict[str, str]:
    """解析 risk/risk_config.py 的 dataclass 字段行内注释，产出 {field: 注释}。"""
    res: Dict[str, str] = {}
    path = _BASE / "risk" / "risk_config.py"
    if not path.exists():
        return res
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s{4}([a-z_][a-z0-9_]*)\s*:\s*[^=]+=\s*[^#]+#\s*(.*)$", raw)
        if m:
            res[m.group(1)] = m.group(2).strip()
    return res


@lru_cache(maxsize=1)
def _settings_map() -> Dict[str, str]:
    return _extract_settings_comments()


@lru_cache(maxsize=1)
def _risk_map() -> Dict[str, str]:
    return _extract_risk_comments()


@lru_cache(maxsize=1)
def _yaml_maps() -> Dict[str, Dict[str, str]]:
    from config.config_loader import get_config_loader

    loader = get_config_loader()
    out: Dict[str, Dict[str, str]] = {}
    for name, rel in loader.ALL_CONFIG_FILES.items():
        out[name] = _extract_yaml_comments(loader.config_dir / rel)
    return out


# ---------------------------------------------------------------------------
# 对外：描述查询
# ---------------------------------------------------------------------------
def describe(scope: str, group: str, path: str, leaf: str) -> str:
    """返回某字段的中文说明（无则空串）。"""
    try:
        if scope == "patterns":
            if group == "multi_factor_emotion_fit":
                parts = leaf.split(".")
                if len(parts) == 2:
                    return f"{parts[0]}下「{parts[1]}」模式的适配分(0-100)"
                return "情绪周期×模式 适配分(0-100)"
            return PATTERN_DESC.get(group, {}).get(leaf, "")
        if scope == "settings":
            return _settings_map().get(path, "") or _SETTINGS_FALLBACK.get(path, "")
        if scope == "yaml":
            name, _, rel = path.partition(".")
            m = _yaml_maps().get(name, {})
            cur = rel
            while cur:                      # 叶子无注释时向上回退到最近的父级注释
                if cur in m:
                    return m[cur]
                if "." not in cur:
                    break
                cur = cur.rsplit(".", 1)[0]
            return ""
        if scope == "risk":
            return _risk_map().get(path, "")
    except Exception:
        return ""
    return ""
