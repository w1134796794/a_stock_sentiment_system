"""龙头生命周期注册中心（Phase 1 地基 · L1：弱转强 × 龙二波统一身份）。

把"谁是/曾是龙头、当前处于生命周期哪个阶段"收敛为**单一事实来源**，供弱转强、
龙二波（后续 L2 再纳入首板突破/二板定龙）共同订阅，替代各策略各自重建前龙身份的
现状。

设计要点（与重构方案 docs/refactor_plan_dragon_lifecycle_complementarity.md 对齐）：

  - **纯加性**：本模块只提供"读模型 + 阶段划分"。可由现有 ``WeakToStrongStrategy``
    的 ``dragon_pool``/``weakening_pool`` 构建视图（``from_weak_to_strong``），不改动
    任何既有判定阈值与输出。
  - **阶段划分是纯函数**（``classify_phase``），便于单测与跨策略复用。
  - **配置驱动**：阶段窗口来自 ``config.pattern_params`` 的 ``dragon_lifecycle`` 组，
    支持 webdata 覆盖。

生命周期阶段（L1 覆盖 WEAKENING~SECOND_WAVE；SEED/PROMOTION 预留给 L2）：

    SEED → PROMOTION → LEADER → WEAKENING/WATCHING/FLASH_RECOVERY
                                      └→ ADJUSTING → SECOND_WAVE_READY → EXPIRED
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import loguru

from config.pattern_params import get_params

logger = loguru.logger


class DragonPhase(Enum):
    """龙头生命周期阶段。"""
    SEED = "诞生"               # 热点首板候选（L2：首板突破域）
    PROMOTION = "定位"          # 二板待定龙（L2：二板定龙域）
    LEADER = "龙头"             # 已是龙头，加速/持有（无入场策略）
    WEAKENING = "走弱"          # 刚走弱，进入观察（弱转强域）
    WATCHING = "观察"           # 走弱后短周期观察（弱转强域）
    FLASH_RECOVERY = "日内反转"  # 当日走弱当日强势收复（弱转强域，same_day）
    ADJUSTING = "调整"          # 充分调整中（龙二波域）
    SECOND_WAVE_READY = "二波待发"  # 调整到位、二波启动（龙二波域）
    EXPIRED = "淘汰"            # 超期/A杀/破位


# 各信号域包含的阶段（供 query 与策略订阅）
WEAK_TO_STRONG_PHASES = (DragonPhase.WEAKENING, DragonPhase.WATCHING, DragonPhase.FLASH_RECOVERY)
DRAGON_SECOND_WAVE_PHASES = (DragonPhase.ADJUSTING, DragonPhase.SECOND_WAVE_READY)


@dataclass
class DragonState:
    """一只龙头票在生命周期中的状态快照（读模型）。"""
    stock_code: str
    stock_name: str = ""
    dragon_type: str = ""              # 连板龙头/趋势龙头/空间龙头
    sector_name: str = ""

    peak_height: int = 0               # 最高连板数
    peak_date: str = ""                # 见顶日期 YYYYMMDD
    peak_price: float = 0.0

    weakening_type: str = ""           # 断板/烂板/尾盘板/放量滞涨/趋势回调
    weakening_date: str = ""           # 入池(确认走弱)日期 YYYYMMDD
    max_drawdown: float = 0.0          # 距高点最大回调幅度

    days_since_peak: int = 0           # 距见顶交易/自然日（构建方负责口径）
    days_since_weakening: int = 0      # 距确认走弱天数

    phase: DragonPhase = DragonPhase.LEADER
    both_eligible: bool = False        # 落在交接带：弱转强/龙二波都可命中
    same_day: bool = False             # 当日走弱当日收复

    source: str = ""                   # 构建来源（weak_to_strong/dragon_second_wave/...）
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "代码": self.stock_code,
            "名称": self.stock_name,
            "龙头类型": self.dragon_type,
            "板块": self.sector_name,
            "最高连板": self.peak_height,
            "见顶日": self.peak_date,
            "走弱类型": self.weakening_type,
            "走弱日": self.weakening_date,
            "距高点天数": self.days_since_peak,
            "距走弱天数": self.days_since_weakening,
            "回调": f"{self.max_drawdown * 100:.1f}%",
            "阶段": self.phase.value,
            "交接带": self.both_eligible,
            "同日反转": self.same_day,
        }


def _parse_ymd(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(date_str).strip(), "%Y%m%d")
    except Exception:
        return None


def classify_phase(
    *,
    weakened: bool,
    days_since_weakening: int,
    days_since_peak: int,
    max_drawdown: float,
    same_day_recovered: bool,
    adjust_depth: Optional[float],
    cfg: Dict[str, Any],
) -> DragonState:
    """纯函数：依据窗口/深度把一只票划入生命周期阶段。

    仅返回一个**仅含阶段相关字段**的轻量 ``DragonState``（phase/both_eligible/same_day），
    调用方再合并业务字段。这样便于独立单测。

    判定优先级（L1）：
      1. 当日走弱当日收复 → FLASH_RECOVERY（日内反转是独立信号，优先于一切）
      2. 未走弱 → LEADER
      3. A杀：回调超龙二波最深容忍（``dsw_max_adjust_depth``）→ EXPIRED（任何策略都不做）
      4. 弱转强域优先：距走弱 ≤ ``wts_max_watch_days`` 且 回调 ≤ ``wts_max_drawdown``
         → WEAKENING(0天)/WATCHING（**只看距走弱，不看距高点**，老龙近日走弱仍在窗内）
      5. 龙二波域：距高点 ∈ [dsw_min, dsw_max] 且 深度 ∈ [min_depth, max_depth]
         → SECOND_WAVE_READY（深度达上沿）/ ADJUSTING
      6. 距高点超 ``expire_max_days_since_peak`` 且不在任何信号域 → EXPIRED
      7. 其余（已过弱转强窗、深度未达龙二波）→ ADJUSTING（调整中等待二波）

    设计要点：``expire_max_days_since_peak`` 只用于退役"既不在弱转强窗、也不在龙二波域"
    的老票；**不能**用它淘汰"距高点久但近日才走弱"的票（否则会漏掉趋势龙头反包）。
    回调上限分域：弱转强用 ``wts_max_drawdown``(浅)，龙二波容忍到 ``dsw_max_adjust_depth``(深)。
    """
    st = DragonState(stock_code="")

    expire_days = int(cfg.get("expire_max_days_since_peak", 20))
    wts_watch = int(cfg.get("wts_max_watch_days", 5))
    wts_max_dd = float(cfg.get("wts_max_drawdown", 0.20))
    dsw_min = int(cfg.get("dsw_min_adjust_days", 2))
    dsw_max = int(cfg.get("dsw_max_adjust_days", 15))
    dsw_min_depth = float(cfg.get("dsw_min_adjust_depth", 0.05))
    dsw_max_depth = float(cfg.get("dsw_max_adjust_depth", 0.30))
    handoff_enabled = bool(cfg.get("handoff_overlap_enabled", True))

    # 1) 当日走弱当日收复（日内反转独立信号，优先于淘汰）
    if same_day_recovered:
        st.phase = DragonPhase.FLASH_RECOVERY
        st.same_day = True
        return st

    # 2) 未走弱
    if not weakened:
        st.phase = DragonPhase.LEADER
        return st

    # 3) A杀：回调过深，任何反包/二波都不做
    if max_drawdown > dsw_max_depth:
        st.phase = DragonPhase.EXPIRED
        return st

    # 4) 弱转强域优先（只看距走弱 + 浅回调；不受距高点天数限制）
    wts_ok = (days_since_weakening <= wts_watch) and (max_drawdown <= wts_max_dd)
    depth_ok = (adjust_depth is not None and dsw_min_depth <= adjust_depth <= dsw_max_depth)
    in_dsw = (dsw_min <= days_since_peak <= dsw_max) and depth_ok

    if wts_ok:
        # 交接带标记（弱转强观察窗末端与龙二波调整窗起点重叠区）
        if handoff_enabled and (dsw_min <= days_since_weakening <= wts_watch):
            st.both_eligible = True
        st.phase = DragonPhase.WEAKENING if days_since_weakening <= 0 else DragonPhase.WATCHING
    elif in_dsw:
        # 深度接近上沿视为调整充分、二波待发；否则仍在调整
        if adjust_depth is not None and adjust_depth >= (dsw_min_depth + dsw_max_depth) / 2:
            st.phase = DragonPhase.SECOND_WAVE_READY
        else:
            st.phase = DragonPhase.ADJUSTING
    elif days_since_peak > expire_days:
        st.phase = DragonPhase.EXPIRED
    else:
        # 既过弱转强窗、又不满足龙二波条件 → 仍在调整（等待二波）
        st.phase = DragonPhase.ADJUSTING
    return st


class DragonLifecycleRegistry:
    """龙头生命周期注册中心（读模型 + 阶段划分）。

    Phase 1 用法（非侵入）：用 ``from_weak_to_strong`` 把现有走弱池构建成统一视图，
    供 ``query`` 按阶段订阅。后续阶段再让策略直接消费本注册中心。
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg: Dict[str, Any] = cfg if cfg is not None else get_params("dragon_lifecycle")
        self.states: Dict[str, DragonState] = {}

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def upsert(self, state: DragonState) -> None:
        self.states[state.stock_code] = state

    def from_weak_to_strong(self, strategy: Any, today_date: str) -> "DragonLifecycleRegistry":
        """从 ``WeakToStrongStrategy`` 的 dragon_pool/weakening_pool 构建生命周期视图。

        - dragon_pool（未走弱龙头）→ LEADER
        - weakening_pool（已走弱）→ 经 classify_phase 落 WEAKENING/WATCHING/.../EXPIRED

        天数口径：以自然日近似（与现有 ``_filter_weakening_pool_for_detection`` 一致），
        后续可替换为交易日口径而不改接口。
        """
        today = _parse_ymd(today_date)

        def _days(a: Optional[datetime]) -> int:
            if today is None or a is None:
                return 0
            return (today - a).days

        # 未走弱龙头
        for code, dragon in getattr(strategy, "dragon_pool", {}).items():
            peak_date = getattr(dragon, "peak_date", "")
            st = DragonState(
                stock_code=str(code),
                stock_name=getattr(dragon, "stock_name", ""),
                dragon_type=getattr(getattr(dragon, "dragon_type", None), "value", ""),
                sector_name=getattr(dragon, "sector_name", ""),
                peak_height=getattr(dragon, "peak_board_height", 0),
                peak_date=peak_date,
                peak_price=getattr(dragon, "peak_price", 0.0),
                days_since_peak=_days(_parse_ymd(peak_date)),
                phase=DragonPhase.LEADER,
                source="weak_to_strong",
            )
            self.upsert(st)

        # 已走弱龙头
        for code, wk in getattr(strategy, "weakening_pool", {}).items():
            peak_date = getattr(wk, "peak_date", "")
            weakening_date = getattr(wk, "weakening_date", "")
            days_since_peak = _days(_parse_ymd(peak_date))
            days_since_weakening = _days(_parse_ymd(weakening_date))
            max_dd = float(getattr(wk, "max_drawdown", 0.0) or 0.0)
            same_day = bool(weakening_date and weakening_date == str(today_date))

            phase_only = classify_phase(
                weakened=True,
                days_since_weakening=days_since_weakening,
                days_since_peak=days_since_peak,
                max_drawdown=max_dd,
                same_day_recovered=same_day,
                adjust_depth=max_dd,  # 以回调幅度近似调整深度
                cfg=self.cfg,
            )
            st = DragonState(
                stock_code=str(code),
                stock_name=getattr(wk, "stock_name", ""),
                dragon_type=getattr(getattr(wk, "dragon_type", None), "value", ""),
                sector_name=getattr(wk, "sector_name", ""),
                peak_height=getattr(wk, "peak_board_height", 0),
                peak_date=peak_date,
                peak_price=getattr(wk, "peak_price", 0.0),
                weakening_type=getattr(wk, "weakening_type", ""),
                weakening_date=weakening_date,
                max_drawdown=max_dd,
                days_since_peak=days_since_peak,
                days_since_weakening=days_since_weakening,
                phase=phase_only.phase,
                both_eligible=phase_only.both_eligible,
                same_day=phase_only.same_day,
                source="weak_to_strong",
            )
            self.upsert(st)

        logger.debug(f"[生命周期] 从弱转强池构建 {len(self.states)} 只状态（{today_date}）")
        return self

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query(
        self,
        phases: Optional[tuple] = None,
        *,
        include_handoff: bool = True,
        max_days_since_peak: Optional[int] = None,
    ) -> List[DragonState]:
        """按阶段订阅。

        Args:
            phases: 需要的阶段集合（如 ``WEAK_TO_STRONG_PHASES``）；None=全部。
            include_handoff: 是否额外纳入 ``both_eligible`` 的交接带票（默认是）。
            max_days_since_peak: 额外按距高点天数上限过滤。
        """
        out: List[DragonState] = []
        for st in self.states.values():
            hit = (phases is None) or (st.phase in phases)
            if not hit and include_handoff and st.both_eligible:
                hit = True
            if not hit:
                continue
            if max_days_since_peak is not None and st.days_since_peak > max_days_since_peak:
                continue
            out.append(st)
        return out

    def get(self, code: str) -> Optional[DragonState]:
        return self.states.get(str(code))

    def summary(self) -> Dict[str, int]:
        """各阶段计数（监控/复盘用）。"""
        counts: Dict[str, int] = {}
        for st in self.states.values():
            counts[st.phase.value] = counts.get(st.phase.value, 0) + 1
        return counts