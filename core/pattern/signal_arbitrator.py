"""跨策略信号仲裁层（Phase 4 · L1 收尾）。

四个模式策略（弱转强/二板定龙/龙二波/首板突破）可能对**同一只票**同时出信号。
此前各策略各放各的 key、全程无协调，导致：
  - 同票被重复输出（去重缺失）；
  - 同票被两策略边界争议（如弱转强∩龙二波）无人裁决；
  - 多策略共振未被识别/加权。

本模块把"同票多策略"收敛为一次**仲裁**：
  - **择主**：按策略优先级（可配）+ 置信度，选一条主信号；
  - **去重**：其余标为被抑制（默认保留、仅标注；strict 模式才剔除）；
  - **协同**：识别共振并可对主信号轻微加权（reweight 模式）。

设计原则（与重构方案对齐）：
  - **纯函数 + 注解优先**：`arbitrate` 只算决策；`apply` 把决策写回信号的 key_metrics。
  - **默认零 diff**：默认 `mode=annotate`，只加标注、不改信号集合/置信度。
  - 被抑制信号保留到 `key_metrics['仲裁']`，可完整复盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import loguru

logger = loguru.logger

# 择主优先级：数值越大越权威（同票多策略时优先作为主信号）
DEFAULT_PRIORITY: Dict[str, int] = {
    "二板定龙": 4,
    "龙二波": 3,
    "弱转强": 2,
    "首板突破": 1,
}

PATTERN_KEYS = ("弱转强", "二板定龙", "龙二波", "首板突破")


def _norm_code6(code: str) -> str:
    s = str(code).strip().upper()
    return s.split('.')[0].zfill(6) if s else s


@dataclass
class ArbitrationDecision:
    """单只票的仲裁结论。"""
    code: str
    name: str = ""
    primary_pattern: str = ""
    all_patterns: List[str] = field(default_factory=list)
    suppressed_patterns: List[str] = field(default_factory=list)
    is_resonance: bool = False
    resonance_bonus: float = 0.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "主信号": self.primary_pattern,
            "全部命中": self.all_patterns,
            "被抑制": self.suppressed_patterns,
            "共振": self.is_resonance,
            "共振加权": round(self.resonance_bonus, 4),
            "择主理由": self.reason,
        }


@dataclass
class ArbitrationResult:
    decisions: Dict[str, ArbitrationDecision] = field(default_factory=dict)  # by code6

    @property
    def overlaps(self) -> Dict[str, ArbitrationDecision]:
        """仅含多策略命中的票。"""
        return {c: d for c, d in self.decisions.items() if len(d.all_patterns) > 1}


def _effective_priority(
    base: Dict[str, int], emotion: str, cfg: Dict[str, Any]
) -> Dict[str, int]:
    """按情绪周期对择主优先级做增量调整（Phase 5 路由）。

    ``cfg['emotion_routing']`` 形如 ``{"退潮期": {"弱转强": -2, "龙二波": +1}}``：
    在该情绪下对应策略的择主优先级偏移。默认空表 → 不调整（零 diff）。
    """
    routing = (cfg.get("emotion_routing") or {}).get(str(emotion), {}) if emotion else {}
    if not routing:
        return dict(base)
    eff = dict(base)
    for pt, delta in routing.items():
        eff[pt] = eff.get(pt, 0) + int(delta)
    return eff


def arbitrate(
    results: Dict[str, List[Any]],
    *,
    priority: Optional[Dict[str, int]] = None,
    cfg: Optional[Dict[str, Any]] = None,
    emotion: str = "",
) -> ArbitrationResult:
    """计算仲裁决策（纯函数，不改入参）。

    Args:
        results: {pattern_type: [signal,...]}（signal 鸭子类型，需 stock_code/confidence）。
        priority: 策略择主优先级；默认 ``DEFAULT_PRIORITY``。
        cfg: 仲裁配置（resonance_bonus / resonance_max_confidence / emotion_routing）。
        emotion: 当前市场情绪周期（用于 Phase 5 择主路由；空=不路由）。
    """
    cfg = cfg or {}
    priority = _effective_priority(priority or DEFAULT_PRIORITY, emotion, cfg)
    resonance_bonus = float(cfg.get("resonance_bonus", 0.05))
    resonance_max = float(cfg.get("resonance_max_confidence", 0.98))

    # 聚合：code6 → {pattern: signal}
    by_code: Dict[str, Dict[str, Any]] = {}
    name_by_code: Dict[str, str] = {}
    for ptype in PATTERN_KEYS:
        for s in (results.get(ptype, []) or []):
            c6 = _norm_code6(getattr(s, "stock_code", ""))
            if not c6:
                continue
            by_code.setdefault(c6, {})[ptype] = s
            name_by_code.setdefault(c6, getattr(s, "stock_name", ""))

    res = ArbitrationResult()
    for c6, pmap in by_code.items():
        patterns = list(pmap.keys())
        # 择主：优先级降序，其次置信度降序
        def _key(pt: str):
            return (priority.get(pt, 0), float(getattr(pmap[pt], "confidence", 0) or 0))
        ranked = sorted(patterns, key=_key, reverse=True)
        primary = ranked[0]
        suppressed = ranked[1:]
        is_resonance = len(patterns) > 1
        bonus = 0.0
        if is_resonance:
            # 共振：主信号置信度越接近上限加权越小（凸性），上限封顶
            base_conf = float(getattr(pmap[primary], "confidence", 0) or 0)
            bonus = min(resonance_bonus, max(0.0, resonance_max - base_conf))
        reason = (
            f"多策略命中{patterns}→取优先级最高[{primary}]"
            if is_resonance else f"仅[{primary}]命中"
        )
        res.decisions[c6] = ArbitrationDecision(
            code=c6, name=name_by_code.get(c6, ""),
            primary_pattern=primary, all_patterns=patterns,
            suppressed_patterns=suppressed, is_resonance=is_resonance,
            resonance_bonus=bonus, reason=reason,
        )
    return res


def apply(
    results: Dict[str, List[Any]],
    arb: ArbitrationResult,
    *,
    mode: str = "annotate",
) -> Dict[str, List[Any]]:
    """把仲裁决策作用到信号集合，返回新的 results。

    mode：
      - ``annotate``（默认，零 diff）：仅把仲裁结论写入每条信号的 ``key_metrics['仲裁']``；
        不改信号集合，不改置信度。
      - ``reweight``：在 annotate 基础上，对**共振主信号**置信度加 ``resonance_bonus``
        （保留所有信号，仅主信号小幅提升）。
      - ``dedup``：在 reweight 基础上，从各策略列表中**剔除被抑制信号**（只保留主信号），
        被抑制信号的快照仍记录在主信号的 ``key_metrics['仲裁']['被抑制明细']``。
    """
    if mode not in ("annotate", "reweight", "dedup"):
        mode = "annotate"

    # 收集每只票被抑制的信号快照（供 dedup 复盘）
    suppressed_snapshot: Dict[str, List[Dict[str, Any]]] = {}
    out: Dict[str, List[Any]] = {}

    for ptype in PATTERN_KEYS:
        kept: List[Any] = []
        for s in (results.get(ptype, []) or []):
            c6 = _norm_code6(getattr(s, "stock_code", ""))
            d = arb.decisions.get(c6)
            # 仅对"多策略命中"的票介入；单策略命中不标注、不改动（保持零 diff）
            if d is None or len(d.all_patterns) <= 1:
                kept.append(s)
                continue
            is_primary = (ptype == d.primary_pattern)
            # 注解写回
            km = getattr(s, "key_metrics", None)
            if isinstance(km, dict):
                km["仲裁"] = {**d.to_dict(), "本条为主信号": is_primary}
            if is_primary:
                if mode in ("reweight", "dedup") and d.is_resonance and d.resonance_bonus > 0:
                    try:
                        s.confidence = float(getattr(s, "confidence", 0) or 0) + d.resonance_bonus
                    except Exception:
                        pass
                kept.append(s)
            else:
                # 被抑制信号
                suppressed_snapshot.setdefault(c6, []).append({
                    "策略": ptype,
                    "代码": getattr(s, "stock_code", ""),
                    "置信度": round(float(getattr(s, "confidence", 0) or 0), 4),
                })
                if mode == "dedup":
                    continue  # 剔除
                kept.append(s)  # annotate/reweight 保留
        out[ptype] = kept

    # dedup 模式：把被抑制明细补进主信号
    if mode == "dedup" and suppressed_snapshot:
        for ptype in PATTERN_KEYS:
            for s in out.get(ptype, []):
                c6 = _norm_code6(getattr(s, "stock_code", ""))
                d = arb.decisions.get(c6)
                km = getattr(s, "key_metrics", None)
                if d and ptype == d.primary_pattern and isinstance(km, dict) and c6 in suppressed_snapshot:
                    km.setdefault("仲裁", {})["被抑制明细"] = suppressed_snapshot[c6]
    return out


def gate_by_emotion(
    results: Dict[str, List[Any]],
    emotion: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Any]]:
    """情绪周期闸门（Phase 5）：在指定情绪下整体抑制某些策略的信号。

    ``cfg['emotion_gate']`` 形如 ``{"退潮期": ["首板突破", "弱转强"]}``：
    该情绪下被列出的策略本日不出信号。默认空表 → 不抑制（零 diff）。
    被抑制的策略列表会被清空（保留 key），其余原样返回。
    """
    cfg = cfg or {}
    gate = (cfg.get("emotion_gate") or {}).get(str(emotion), []) if emotion else []
    if not gate:
        return results
    gate_set = set(gate)
    out: Dict[str, List[Any]] = {}
    for ptype, sigs in results.items():
        if ptype in gate_set:
            if sigs:
                logger.warning(f"[仲裁-情绪闸门] {emotion} 抑制策略[{ptype}] {len(sigs)} 条信号")
            out[ptype] = []
        else:
            out[ptype] = sigs
    return out


__all__ = [
    "DEFAULT_PRIORITY",
    "ArbitrationDecision",
    "ArbitrationResult",
    "arbitrate",
    "apply",
    "gate_by_emotion",
]