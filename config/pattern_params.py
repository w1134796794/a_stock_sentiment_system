"""集中式可调参数中心（策略 / 打分 / 大盘环境类）。

历史上这些参数以硬编码 dict 写死在各类的 ``__init__`` 里（如 self.params /
self.weights），网页无法调整。现统一收敛到本文件的 ``PATTERN_DEFAULTS``：

  - 各类在 __init__ 里改为 ``self.params = get_params("<group>")`` 读取，
    默认值不变（与原硬编码逐字一致），但支持被 webdata/config_overrides.json
    的 "patterns" 作用域覆盖。
  - 配置注册表（config.config_registry）直接读取本文件的默认值枚举可编辑字段，
    无需实例化这些（依赖数据管理器的）重类。

覆盖存储结构：overrides["patterns"][group][leaf...] = value（支持嵌套深合并）。
"""
from __future__ import annotations

import copy
from typing import Any, Dict

from config.overrides import deep_merge, load_overrides

# ---------------------------------------------------------------------------
# 各组默认参数（与原类内硬编码逐字一致，仅去掉行内注释）
# ---------------------------------------------------------------------------
PATTERN_DEFAULTS: Dict[str, Dict[str, Any]] = {
    # 龙二波 DragonSecondWaveStrategyV2.params
    "dragon_second_wave": {
        "recent_days": 20,
        "max_adjust_days": 15,
        "min_adjust_days": 2,
        "max_break_days": 2,
        "min_adjust_depth": 0.05,
        "max_adjust_depth": 0.30,
        "ma10_tolerance": 0.10,
        "use_ma20_fallback": True,
        "min_first_wave": 3,
        "max_first_wave": 15,
        "min_rise_5d": 0.25,
        "min_rise_10d": 0.40,
        "min_limit_up_count": 4,
        "min_volume_ratio": 1.5,
        "min_seal_ratio": 0.02,
        "volume_abs_max": 5.0,
        "max_limit_up_time": "11:30",
        "max_float_cap": 200.0,
        "max_5d_rise": 0.15,
        "max_break_count": 1,
    },
    # 弱转强 WeakToStrong.params
    "weak_to_strong": {
        "min_board_height": 3,
        "min_total_rise": 0.30,
        "min_limit_up_count": 2,
        "max_limit_up_gap": 2,
        "min_slope_daily": 0.015,
        "min_r_squared": 0.30,
        "min_board_height_for_space": 5,
        "weakening_types": ["烂板", "断板", "尾盘板", "放量滞涨", "趋势回调"],
        "max_drawdown_for_recovery": 0.20,
        "max_monitor_days": 7,
        "min_gap": 0.03,
        "ideal_gap": 0.05,
        "max_gap": 0.08,
        "min_auction_vol_ratio": 0.10,
        "ideal_auction_vol_ratio": 0.15,
        "min_auction_amount": 5000000,
        "max_open_drop": 0.02,
        "max_time_to_limit": 15,
        "enable_flexible_scoring": True,
        "market_sentiment_weight": 0.3,
        "sector_strength_weight": 0.3,
        "stock_momentum_weight": 0.4,
        "dynamic_params_enabled": True,
        "sentiment_bullish_boost": 0.02,
        "sentiment_bearish_penalty": 0.02,
    },
    # 二板定龙 SecondBoardDragon.params（基础）
    "second_board_dragon": {
        "min_seal_ratio": 0.08,
        "ideal_turnover": [8, 20],
        "min_concept_heat": 3,
        "min_gap": 0.02,
        "max_gap": 0.08,
        "min_auction_vol": 0.08,
        "min_auction_amount": 5000000,
        "max_time_to_limit": 15,
        "min_seal_growth": 0.10,
        "max_sector_second_board": 2,
    },
    # 二板定龙 SecondBoardDragon.strict_params（严格模式）
    "second_board_dragon_strict": {
        "min_gap": 0.05,
        "max_gap": 0.08,
        "min_auction_vol": 0.08,
        "max_auction_vol": 0.15,
        "max_limit_up_time": "10:00",
        "min_turnover": 0.15,
        "min_sector_first_board": 1,
        "skip_first_board_seal": True,
        "skip_tail_board_time": "14:30",
    },
    # 首板突破 FirstBoardBreakout.params
    "first_board_breakout": {
        "max_5d_rise": 0.15,
        "max_float_cap": 100.0,
        "hot_sector_heat_threshold": 5,
        "fast_limit_max_time": "0940",
        "max_break_count": 1,
        "min_sector_limit_up": 2,
        "skip_tail_board_time": "14:30",
        "volume_max_ratio": 3.0,
        "volume_abs_max": 5.0,
        "platform_days_min": 7,
        "platform_days_max": 15,
        "max_distance_from_high": 0.25,
    },
    # 多因子打分 MultiFactorScorer.weights
    "multi_factor_weights": {
        "pattern_quality": 0.35,
        "sector_strength": 0.30,
        "stock_position": 0.20,
        "emotion_fit": 0.15,
    },
    # 多因子打分 MultiFactorScorer.emotion_fit_map（情绪周期×模式适配分）
    "multi_factor_emotion_fit": {
        "冰点期": {"弱转强": 90, "二板定龙": 70, "首板突破": 50, "龙二波": 40},
        "上升期": {"弱转强": 85, "二板定龙": 95, "首板突破": 80, "龙二波": 75},
        "高潮期": {"弱转强": 60, "二板定龙": 70, "首板突破": 50, "龙二波": 80},
        "退潮期": {"弱转强": 70, "二板定龙": 50, "首板突破": 30, "龙二波": 40},
        "震荡期": {"弱转强": 75, "二板定龙": 65, "首板突破": 60, "龙二波": 55},
    },
    # 大盘环境 MarketEnvAnalyzer.index_trend_weights
    "layer1_index_trend_weights": {
        "sh": 0.35,
        "sz": 0.25,
        "cyb": 0.20,
        "kcb": 0.10,
        "bj": 0.10,
    },
    # 大盘环境 MarketEnvAnalyzer.weights（趋势/量能/宽度）
    "layer1_weights": {
        "trend": 0.40,
        "volume": 0.30,
        "width": 0.30,
    },
    # 大盘环境 MarketEnvAnalyzer.trend_params
    "layer1_trend_params": {
        "ma_short": 5,
        "ma_mid": 20,
        "ma_long": 60,
        "bull_threshold": 0.01,
        "bear_threshold": -0.01,
    },
    # 大盘环境 MarketEnvAnalyzer.volume_params
    "layer1_volume_params": {
        "expand_ratio": 1.2,
        "shrink_ratio": 0.8,
        "lookback_days": 5,
    },
    # 大盘环境 MarketEnvAnalyzer.width_params
    "layer1_width_params": {
        "strong_threshold": 0.70,
        "normal_threshold": 0.40,
        "weak_threshold": 0.20,
    },
}

# 分组中文标签（注册表 / 网页展示用）
PATTERN_GROUP_LABELS: Dict[str, str] = {
    "dragon_second_wave": "龙二波 · 识别参数",
    "weak_to_strong": "弱转强 · 识别参数",
    "second_board_dragon": "二板定龙 · 基础参数",
    "second_board_dragon_strict": "二板定龙 · 严格模式参数",
    "first_board_breakout": "首板突破 · 识别参数",
    "multi_factor_weights": "多因子打分 · 权重",
    "multi_factor_emotion_fit": "多因子打分 · 情绪周期适配分",
    "layer1_index_trend_weights": "大盘环境 · 指数趋势权重",
    "layer1_weights": "大盘环境 · 综合评分权重",
    "layer1_trend_params": "大盘环境 · 趋势参数",
    "layer1_volume_params": "大盘环境 · 量能参数",
    "layer1_width_params": "大盘环境 · 宽度参数",
}


def get_params(group: str) -> Dict[str, Any]:
    """读取某组参数：默认值深合并 webdata 覆盖后返回（每次返回独立副本）。"""
    defaults = PATTERN_DEFAULTS.get(group, {})
    ov = load_overrides().get("patterns", {}).get(group, {})
    if not ov:
        return copy.deepcopy(defaults)
    return deep_merge(defaults, ov)


def get_default_params(group: str) -> Dict[str, Any]:
    """读取某组的纯默认值（注册表枚举字段用）。"""
    return copy.deepcopy(PATTERN_DEFAULTS.get(group, {}))


def all_groups() -> Dict[str, Dict[str, Any]]:
    """返回 {group: 默认参数} 的副本，供注册表遍历。"""
    return {g: copy.deepcopy(v) for g, v in PATTERN_DEFAULTS.items()}