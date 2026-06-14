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
        # Phase 3/4：置信度算法。"legacy"=旧加分制（默认，行为不变）；"deduction"=统一满分扣分制
        "confidence_mode": "legacy",
        # 最终信号最低置信度。0=不过滤；0.60 或 60 均表示低于60%过滤。
        "min_confidence": 0.0,
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
        "confidence_mode": "legacy",
        # 最终信号最低置信度。0=不过滤；0.60 或 60 均表示低于60%过滤。
        "min_confidence": 0.0,
        # 盘中实时观测：走弱池个股「以昨收为基准」的涨幅 ≥ 此值即判定转强。
        # 支持 0.07 / 7 / "7%" 多种写法（>1 视为百分数）。
        "intraday_recovery_pct": 0.07,
    },
    # 龙头生命周期注册中心（Phase 1：弱转强×龙二波统一身份/阶段划分）
    # 用于把"前龙身份+所处阶段"收敛为单一事实来源；阶段窗口可经覆盖调参。
    "dragon_lifecycle": {
        # —— 弱转强信号域（早段/浅调反包）——
        "wts_max_watch_days": 5,       # 走弱后纳入「弱转强」观察的最长天数（按距走弱日）
        "wts_max_drawdown": 0.20,      # 弱转强允许的最大回调（超过疑似A杀，淘汰）
        # —— 龙二波信号域（中段/充分调整二波）——
        "dsw_min_adjust_days": 2,      # 龙二波最小调整天数（按距高点）
        "dsw_max_adjust_days": 15,     # 龙二波最大调整天数
        "dsw_min_adjust_depth": 0.05,  # 龙二波最小调整深度
        "dsw_max_adjust_depth": 0.30,  # 龙二波最大调整深度
        # —— 交接带：落在 [wts_max_watch_days, dsw_min_adjust_days] 视为两策略都可命中，交仲裁层 ——
        "handoff_overlap_enabled": True,
        # —— 全局淘汰 ——
        "expire_max_days_since_peak": 20,  # 距高点超过此天数从生命周期淘汰
        # —— 龙二波候选来源（Phase 3 切源）——
        # "registry": 候选身份改读注册中心——先用"触发前前龙枚举器"把今日涨停票里的前龙
        #             (调整窗内)并入注册中心龙二波域，再取"今日触发 ∩ 注册中心龙二波域"为候选。
        #             枚举器是龙二波 L1 身份门的超集 → 对最终信号零 diff，且身份来源唯一化。
        # "legacy":  旧逻辑（遍历今日全市场，detect_second_wave 内部各自重建前龙）。可回滚。
        "dsw_candidate_source": "registry",
    },
    # 跨策略信号仲裁（Phase 4）：同票多策略命中时择主/去重/协同
    "arbitration": {
        "enabled": True,           # 是否运行仲裁（关闭则完全不介入）
        # 作用模式：annotate=仅标注(零diff)；reweight=共振主信号加权；dedup=剔除被抑制信号
        "mode": "annotate",
        "resonance_bonus": 0.05,   # 多策略共振时主信号置信度加权上限
        "resonance_max_confidence": 0.98,  # 共振加权后的置信度封顶
        # —— Phase 5 情绪周期路由/闸门（默认空→不介入，零 diff）——
        # 择主优先级偏移：{情绪: {策略: 偏移}}，如 {"退潮期": {"弱转强": -2, "龙二波": 1}}
        "emotion_routing": {},
        # 情绪闸门：{情绪: [本日整体抑制的策略]}，如 {"退潮期": ["首板突破"]}
        "emotion_gate": {},
    },
    # 弱转强 Layer4 多维评分阈值（detect_weak_to_strong 的竞价/技术/资金/情绪维度）
    # 历史上这些阈值与分值硬编码在 pattern_recognition 里，现统一收敛，支持网页/覆盖调参。
    "weak_to_strong_scoring": {
        # —— 竞价量价维度（满分 = auction_cap）——
        "auction_cap": 25,
        "auction_gap_high": 0.05, "auction_gap_high_pts": 15,   # 高开≥5% 加分
        "auction_gap_mid": 0.03, "auction_gap_mid_pts": 10,     # 高开≥3% 加分
        "auction_gap_low": 0.02, "auction_gap_low_pts": 5,      # 高开≥2% 加分
        "auction_vol_high": 0.15, "auction_vol_high_pts": 10,   # 竞价量比≥15% 加分
        "auction_vol_mid": 0.10, "auction_vol_mid_pts": 5,      # 竞价量比≥10% 加分
        "auction_amount_threshold": 10000000, "auction_amount_pts": 5,  # 竞价金额≥阈值 加分
        # —— 技术形态维度（满分 20）——
        "tech_above_ma5_pts": 8,                                # 收盘站上5日均线
        "tech_ma5_turnup_pts": 5,                               # 5日均线拐头向上
        "tech_vol_breakout_ratio": 1.5, "tech_vol_breakout_pts": 5,  # 量能突破5日均量倍数
        "tech_yang_pts": 2,                                     # 收盘>开盘（阳线）
        # —— 资金流入维度（满分 25）——
        "capital_lookback_days": 3,                             # 资金流向回溯交易日数
        "capital_net_inflow_pts": 10,                           # 主力净流入为正
        "capital_big_ratio_high": 0.20, "capital_big_ratio_high_pts": 10,  # 大单净占比≥20%
        "capital_big_ratio_mid": 0.10, "capital_big_ratio_mid_pts": 5,     # 大单净占比≥10%
        "capital_persist_days": 2, "capital_persist_pts": 5,    # 近N日主力净流入为正≥M天
        # —— 市场情绪维度（满分 20）——
        "sector_limit_up_high": 3, "sector_limit_up_high_pts": 10,  # 板块涨停≥3家
        "sector_limit_up_low": 1, "sector_limit_up_low_pts": 5,     # 板块涨停≥1家
        "sector_change_high": 2.0, "sector_change_high_pts": 10,    # 板块涨幅≥2%
        "sector_change_low": 1.0, "sector_change_low_pts": 5,       # 板块涨幅≥1%
        # —— 总分门槛（按龙头类型动态）——
        "min_total_strong_dragon": 45,   # 连板龙头 且 最高板≥5
        "min_total_dragon": 50,          # 连板龙头
        "min_total_trend": 55,           # 趋势龙头
        # —— 信号级别标签门槛 ——
        "signal_level_strong": 80,       # ≥此分为「强烈信号」
        "signal_level_confirm": 65,      # ≥此分为「确认信号」
        # —— P4 日内反转（当日入池走弱→当日涨停收复）兜底置信度 ——
        # confidence_mode=deduction 时该路径改走统一扣分制；legacy 时用此固定值。
        "intraday_reversal_confidence": 0.65,
        # —— 高开低走/冲高回落排除（仅作用于"非涨停大涨/竞价转强"候选）——
        # "转强"不应把开得高却收盘走弱的接力失败票算进来。涨停票收在板上不受此约束。
        "intraday_fade_guard_enabled": True,
        # 收盘较开盘回落 > 此比例 且为阴线(收<开) → 判冲高回落，淘汰。0.03=回落3%。
        "intraday_fade_max_from_open": 0.03,
        # 收盘较当日最高回落 > 此比例 → 判冲高回落，淘汰（不论阴阳线）。0.06=距高点6%。
        "intraday_fade_max_from_high": 0.06,
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
        # —— 候选准入闸（原为硬编码，现可调；默认值与旧逻辑一致）——
        "min_first_board_score": 60,     # 首板质量分下限
        "candidate_min_gap": 0.02,       # 次日高开下限
        "candidate_min_confidence": 0.70,  # 置信度下限
        # —— 定龙择优（新增：把"逐只过闸"改为"全场横向竞争择优定龙"）——
        "max_dragons_per_day": 3,        # 每日最多输出几只"龙头"
        "max_per_sector": 1,             # 同一板块最多保留几只（去同质化）
        "min_sector_first_board": 0,     # 板块首板助攻硬门槛（0=关闭；≥1 则独票二板不定龙）
        # 横向强度评分权重（用于排序定龙，连续不饱和，可分梯队）
        "rank_w_gap": 1.0,               # ×高开%
        "rank_w_quality": 0.5,           # ×首板质量分
        "rank_w_seal": 0.8,              # ×封单强度%
        "rank_w_fast": 10.0,             # 快速封板加分（固定）
        "rank_w_sector_assist": 6.0,     # ×板块首板助攻家数
        "confidence_mode": "legacy",     # Phase 3/4：legacy=旧加分制(默认)；deduction=满分扣分制
        # 二板定龙原有 0.70 候选准入闸，现统一为可配置最终信号最低置信度。
        # 0.60 或 60 均表示低于60%过滤。
        "min_confidence": 0.70,
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
        "confidence_mode": "legacy",
        # 最终信号最低置信度。0=不过滤；0.60 或 60 均表示低于60%过滤。
        "min_confidence": 0.0,
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
    "weak_to_strong_scoring": "弱转强 · Layer4 多维评分阈值",
    "dragon_lifecycle": "龙头生命周期 · 阶段窗口",
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