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
        "confidence_mode": "置信度算法（legacy=旧加分制，deduction=扣分制）",
        "min_confidence": "最终信号最低置信度（0=不过滤；0.60或60表示低于60%过滤）",
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
        "intraday_recovery_pct": "盘中实时转强阈值（走弱池个股以昨收为基准的涨幅≥此值即判转强；0.07/7/7%均可）",
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
        "confidence_mode": "置信度算法（legacy=旧加分制，deduction=扣分制）",
        "min_confidence": "最终信号最低置信度（0=不过滤；0.60或60表示低于60%过滤）",
    },
    "dragon_lifecycle": {
        "wts_max_watch_days": "走弱后纳入弱转强观察的最长天数（按距走弱日）",
        "wts_max_drawdown": "弱转强允许的最大回调（超过疑似A杀淘汰）",
        "dsw_min_adjust_days": "龙二波最小调整天数（按距高点）",
        "dsw_max_adjust_days": "龙二波最大调整天数",
        "dsw_min_adjust_depth": "龙二波最小调整深度",
        "dsw_max_adjust_depth": "龙二波最大调整深度",
        "handoff_overlap_enabled": "交接带是否标记为两策略都可命中（交仲裁层裁决）",
        "expire_max_days_since_peak": "距高点超过此天数从生命周期淘汰",
        "dsw_candidate_source": "龙二波候选来源：registry=触发前前龙枚举器并入注册中心后，取今日触发∩注册中心龙二波域(身份唯一来源，对最终信号零diff)；legacy=旧逻辑(遍历今日全市场+各自重建前龙，可回滚)",
    },
    "arbitration": {
        "enabled": "是否运行跨策略仲裁（同票多策略命中时择主/去重/协同）",
        "mode": "仲裁作用模式：annotate=仅标注(零diff)；reweight=共振主信号加权；dedup=剔除被抑制信号(只留主信号)",
        "resonance_bonus": "多策略共振时对主信号置信度的加权上限",
        "resonance_max_confidence": "共振加权后置信度封顶",
        "emotion_routing": "情绪周期择主优先级偏移：{情绪:{策略:偏移}}，默认空不介入",
        "emotion_gate": "情绪周期闸门：{情绪:[本日整体抑制的策略]}，默认空不介入",
    },
    "weak_to_strong_scoring": {
        "auction_cap": "竞价量价维度满分（封顶分）",
        "auction_gap_high": "竞价高开高档阈值", "auction_gap_high_pts": "竞价高开高档加分",
        "auction_gap_mid": "竞价高开中档阈值", "auction_gap_mid_pts": "竞价高开中档加分",
        "auction_gap_low": "竞价高开低档阈值", "auction_gap_low_pts": "竞价高开低档加分",
        "auction_vol_high": "竞价量比高档阈值", "auction_vol_high_pts": "竞价量比高档加分",
        "auction_vol_mid": "竞价量比中档阈值", "auction_vol_mid_pts": "竞价量比中档加分",
        "auction_amount_threshold": "竞价金额加分阈值（元）", "auction_amount_pts": "竞价金额达标加分",
        "tech_above_ma5_pts": "收盘站上5日均线加分",
        "tech_ma5_turnup_pts": "5日均线拐头向上加分",
        "tech_vol_breakout_ratio": "量能突破5日均量倍数阈值", "tech_vol_breakout_pts": "量能突破加分",
        "tech_yang_pts": "收阳线（收>开）加分",
        "capital_lookback_days": "资金流向回溯交易日数",
        "capital_net_inflow_pts": "主力净流入为正加分",
        "capital_big_ratio_high": "大单净占比高档阈值", "capital_big_ratio_high_pts": "大单净占比高档加分",
        "capital_big_ratio_mid": "大单净占比中档阈值", "capital_big_ratio_mid_pts": "大单净占比中档加分",
        "capital_persist_days": "持续流入判定天数（近3日内净流入为正≥此天数）", "capital_persist_pts": "持续流入加分",
        "sector_limit_up_high": "板块涨停高档家数", "sector_limit_up_high_pts": "板块涨停高档加分",
        "sector_limit_up_low": "板块涨停低档家数", "sector_limit_up_low_pts": "板块涨停低档加分",
        "sector_change_high": "板块涨幅高档阈值(%)", "sector_change_high_pts": "板块涨幅高档加分",
        "sector_change_low": "板块涨幅低档阈值(%)", "sector_change_low_pts": "板块涨幅低档加分",
        "min_total_strong_dragon": "强连板龙头(最高板≥5)总分门槛",
        "min_total_dragon": "连板龙头总分门槛",
        "min_total_trend": "趋势龙头总分门槛",
        "signal_level_strong": "「强烈信号」总分门槛",
        "signal_level_confirm": "「确认信号」总分门槛",
        "intraday_reversal_confidence": "P4日内反转兜底置信度（legacy模式用；deduction模式走扣分制）",
        "intraday_fade_guard_enabled": "高开低走排除开关（仅非涨停大涨/竞价转强候选）",
        "intraday_fade_max_from_open": "收盘较开盘回落超此比例且阴线→判冲高回落淘汰",
        "intraday_fade_max_from_high": "收盘较当日最高回落超此比例→判冲高回落淘汰",
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
        "candidate_min_confidence": "候选最低置信度（兼容旧参数；优先使用 min_confidence）",
        "min_confidence": "最终信号最低置信度（默认70%；0.60或60表示低于60%过滤）",
        "confidence_mode": "置信度算法（legacy=旧加分制，deduction=扣分制）",
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
        "confidence_mode": "置信度算法（legacy=旧加分制，deduction=扣分制）",
        "min_confidence": "最终信号最低置信度（0=不过滤；0.60或60表示低于60%过滤）",
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
_SETTINGS_FALLBACK: Dict[str, str] = {}


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