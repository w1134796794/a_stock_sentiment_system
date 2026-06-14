"""情绪循环相位模型 —— 系统**唯一**的情绪周期判定来源。

面向超短选手的**有方向循环相位**，取代旧引擎零散的"冰点/退潮/震荡/高潮"五标签：

    冰点 → 修复 → 发酵 → 高潮 → 退潮 →（回到冰点）

设计要点：
  - 输入复用情绪 `metrics`（含 cohorts / promotion），不新增取数。
  - 输出 `legacy_cycle_name`（新相位 → 旧周期名映射），下游 profile / 熔断 /
    仲裁 / 仓位经由统一的中文周期名自动跟随。
  - 阈值外置到 `emotion_cycle_config.yaml::phase_model`，纯函数、可单测。

momentum（动量方向）：有昨日快照 metrics 时取**真·环比一阶导**（晋级率 +
赚钱效应 + 高度变动方向）；无历史时回退到基于当日水平的粗略代理。
"""
from typing import Dict, Optional

# —— 默认阈值（兜底；运行时优先读 emotion_cycle_config.yaml 的 phase_model.thresholds）——
_TH_DEFAULT = {
    "freeze_lu": 25,        # 涨停地量线
    "low_lu": 40,           # 涨停偏少线
    "high_lu": 90,          # 涨停密集线
    "climax_board": 6,      # 高潮高度线
    "ferment_board": 4,     # 发酵高度线
    "freeze_board": 3,      # 高度坍塌线（< 该值）
    "prom_strong": 30.0,    # 晋级率健康线(%)
    "prom_weak": 15.0,      # 晋级率断崖线(%)
    "wr_pos": 50.0,         # 赚钱效应转正线(%)
    "wr_weak": 40.0,        # 亏钱效应线(%)
    "broken_high": 40.0,    # 炸板高位(%)
    "broken_div": 25.0,     # 顶部分歧炸板下限(%)
    "ldr_high": 0.4,        # 跌停/涨停高位比
}
_MOM_DEFAULT = {"prom_delta": 5.0, "wr_delta": 5.0, "ambiguous_min_score": 4}


def _load_thresholds() -> Dict:
    """优先从 YAML 单一真源读取阈值，失败回退默认值。"""
    try:
        from config.config_loader import get_emotion_cycle_config
        pm = (get_emotion_cycle_config() or {}).get("phase_model") or {}
        th = dict(_TH_DEFAULT)
        th.update(pm.get("thresholds") or {})
        mom = dict(_MOM_DEFAULT)
        mom.update(pm.get("momentum") or {})
        return {"th": th, "mom": mom}
    except Exception:
        return {"th": dict(_TH_DEFAULT), "mom": dict(_MOM_DEFAULT)}


# 模块级兼容别名（旧引用/单测可直接读默认阈值）
TH = _TH_DEFAULT

PHASES = ("冰点", "修复", "发酵", "高潮", "退潮")

LEGACY_MAP = {
    "冰点": "冰点期",
    "修复": "上升期",
    "发酵": "上升期",
    "高潮": "高潮期",
    "退潮": "退潮期",
    None:   "震荡期",   # 无明确相位（低主线明确度）→ 旧"震荡期"
}


def _num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _compute_trunk_clarity(metrics: Dict) -> float:
    """主线明确度 0-1：连板梯队 + 连板率 + 中军参与（替代旧'震荡'语义）。"""
    mb = _num(metrics.get("max_board_height"), 1) or 1
    cr = _num(metrics.get("continuous_rate"), 0) or 0
    ladder = min(1.0, mb / 6.0)
    conc = min(1.0, cr / 30.0)
    mid_join = 0.5
    cohorts = metrics.get("cohorts") or {}
    mid = cohorts.get("mid") or {}
    if (_num(mid.get("continuous_count"), 0) or 0) >= 1:
        mid_join = 1.0
    return round(0.5 * ladder + 0.4 * conc + 0.1 * mid_join, 3)


def _score_phases(metrics: Dict, TH: Dict) -> Dict[str, float]:
    lu = _num(metrics.get("limit_up_count"), 0) or 0
    mb = _num(metrics.get("max_board_height"), 1) or 1
    br = _num(metrics.get("broken_rate"), 0) or 0
    cr = _num(metrics.get("continuous_rate"), 0) or 0
    ldr = _num(metrics.get("limit_down_ratio"), 0) or 0
    wr = _num(metrics.get("win_rate"))          # 可能 None
    ap = _num(metrics.get("avg_profit"))        # 可能 None
    premium = _num(metrics.get("prev_limit_up_premium"))
    prom = None
    promo = metrics.get("promotion") or {}
    prom = _num(promo.get("overall"))           # 真·晋级率，可能 None
    prom_eff = prom if prom is not None else cr  # 缺失时用连板率兜底

    s = {p: 0.0 for p in PHASES}

    # 冰点：地量 + 高度坍塌 + 一致跌停/亏钱
    if lu < 20:
        s["冰点"] += 3
    elif lu < TH["freeze_lu"]:
        s["冰点"] += 1
    if mb < TH["freeze_board"]:
        s["冰点"] += 2
    if ldr >= 0.5:
        s["冰点"] += 2
    if wr is not None and wr < 30:
        s["冰点"] += 2

    # 退潮：赚钱效应差 + 核按钮多 + 晋级断崖（仍有涨停，未到地量）
    if wr is not None and wr < TH["wr_weak"]:
        s["退潮"] += 3
    if ldr >= TH["ldr_high"]:
        s["退潮"] += 2
    if prom_eff < TH["prom_weak"] and mb >= TH["freeze_board"]:
        s["退潮"] += 2
    if br > TH["broken_high"]:
        s["退潮"] += 1
    if ap is not None and ap < 0:
        s["退潮"] += 1

    # 高潮：涨停密集 + 高度见顶 + 顶部分歧（炸板抬升）
    if lu >= TH["high_lu"]:
        s["高潮"] += 3
    if mb >= TH["climax_board"]:
        s["高潮"] += 4
    if TH["broken_div"] <= br <= TH["broken_high"]:
        s["高潮"] += 1
    if wr is not None and wr >= 60:
        s["高潮"] += 1
    if prom_eff >= TH["prom_strong"]:
        s["高潮"] += 1

    # 发酵：高度"抬升中"（未见顶）+ 晋级率健康 + 炸板可控
    if TH["ferment_board"] <= mb < TH["climax_board"]:
        s["发酵"] += 3
    if prom_eff >= TH["prom_strong"]:
        s["发酵"] += 3
    elif prom_eff >= 20:
        s["发酵"] += 1
    if cr >= 20:
        s["发酵"] += 2
    if br < TH["broken_div"]:
        s["发酵"] += 2
    if premium is not None and premium > 1:
        s["发酵"] += 1

    # 修复：赚钱效应回正 + 涨停回升但高度未起
    if wr is None or wr >= 45:
        s["修复"] += 2
    if ap is not None and ap > 0:
        s["修复"] += 1
    if TH["freeze_lu"] <= lu < TH["high_lu"]:
        s["修复"] += 2
    if mb <= TH["ferment_board"]:
        s["修复"] += 1
    if br < 30:
        s["修复"] += 1

    return s


def _promotion_overall(metrics: Dict) -> Optional[float]:
    return _num((metrics.get("promotion") or {}).get("overall"))


def _derive_momentum(phase: Optional[str], metrics: Dict,
                     prev_metrics: Optional[Dict], TH: Dict, MOM: Dict) -> str:
    """动量方向（升温/见顶/降温）。

    有昨日快照 metrics 时用**真·环比一阶导**（晋级率 + 赚钱效应 + 高度的变动方向）；
    无历史时回退到基于当日水平的粗略代理。
    """
    mb = _num(metrics.get("max_board_height"), 1) or 1
    br = _num(metrics.get("broken_rate"), 0) or 0
    wr = _num(metrics.get("win_rate"))
    prom = _promotion_overall(metrics)

    # 见顶优先：高潮相位或高度见顶且分歧抬升
    if phase == "高潮" or (mb >= TH["climax_board"] and br >= TH["broken_div"]):
        return "见顶"

    if prev_metrics:
        p_wr = _num(prev_metrics.get("win_rate"))
        p_prom = _promotion_overall(prev_metrics)
        p_mb = _num(prev_metrics.get("max_board_height"))
        # 仅用领先指标（晋级率 + 赚钱效应）定方向；高度仅作平手时的弱裁决，
        # 避免高度 +1 这种滞后/弱信号单独把方向翻成升温/降温。
        up = down = 0
        if prom is not None and p_prom is not None:
            if prom - p_prom >= MOM["prom_delta"]:
                up += 1
            elif p_prom - prom >= MOM["prom_delta"]:
                down += 1
        if wr is not None and p_wr is not None:
            if wr - p_wr >= MOM["wr_delta"]:
                up += 1
            elif p_wr - wr >= MOM["wr_delta"]:
                down += 1
        if up > down:
            return "升温"
        if down > up:
            return "降温"
        # 领先指标走平：高度仅作平手裁决（且需 up/down 至少各有一次才参考）
        if up == down and up >= 1 and p_mb is not None:
            if mb > p_mb:
                return "升温"
            if mb < p_mb:
                return "降温"
        return "—"

    # 无历史：粗略代理
    if phase in ("发酵", "修复") and (wr is None or wr >= TH["wr_pos"]) and (prom is None or prom >= 20):
        return "升温"
    if phase in ("退潮", "冰点") or (wr is not None and wr < TH["wr_weak"]):
        return "降温"
    return "—"


def compute_phase_model(metrics: Dict, prev_metrics: Optional[Dict] = None) -> Optional[Dict]:
    """从 metrics 计算循环相位模型。

    Args:
        metrics: 当日情绪 metrics（含 cohorts / promotion）。
        prev_metrics: 昨日快照的 metrics，用于真·环比动量（可选）。

    失败返回 None，绝不影响主流程。
    """
    try:
        if not metrics or not metrics.get("limit_up_count"):
            return None
        cfg = _load_thresholds()
        TH, MOM = cfg["th"], cfg["mom"]

        scores = _score_phases(metrics, TH)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_phase, top_score = ordered[0]
        second_score = ordered[1][1] if len(ordered) > 1 else 0.0
        gap = round(top_score - second_score, 1)

        trunk_clarity = _compute_trunk_clarity(metrics)

        # 主线模糊且相位胶着 → 判为"无明确相位"（旧语义的震荡）
        ambiguous = (top_score < MOM["ambiguous_min_score"]) or (gap < 1 and trunk_clarity < 0.3)
        phase = None if ambiguous else top_phase

        momentum = _derive_momentum(phase, metrics, prev_metrics, TH, MOM)
        legacy = LEGACY_MAP.get(phase, "震荡期")

        return {
            "phase": phase if phase is not None else "无主线",
            "momentum": momentum,
            "trunk_clarity": trunk_clarity,
            "legacy_cycle_name": legacy,
            "scores": {k: round(v, 1) for k, v in scores.items()},
            "score_gap": gap,
        }
    except Exception:
        return None
