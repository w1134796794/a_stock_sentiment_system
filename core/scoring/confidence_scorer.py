"""
统一置信度「满分扣分制」评分器（Phase 3）

把各策略「基础分 + 阶梯加分 + 封顶」的写法，翻转为「满分起扣」的声明式规则：
每条规则 = 因子 + 分段阈值表 + 启用开关。从 ceiling 起，对每条启用规则按命中的
分段扣分，最终 max(floor, ceiling - Σpenalty)。保留逐项扣分明细 (breakdown)，
便于复盘"为什么不是满分"。

规则模型（落 config/confidence_rules.yaml）：

    second_board_dragon:
      ceiling: 95           # 天花板<100，承认不确定性（坑①）
      floor: 40             # 地板，防扣穿失去区分度（坑③）
      rules:
        - factor: seal_ratio
          enabled: true
          bands: [[0.05, 0], [0.03, 3], [0.02, 5], [0.01, 10], [0.0, 15]]

分段语义：阈值**降序**表达"≥某值扣多少"，取第一个满足 value >= 阈值 的区间 penalty；
分段须连续覆盖到最小值（如 0.0）以避免空洞（坑②）。

坑①（满分虚高）：ceiling < 100。
坑②（区间空洞）：分段覆盖到下界。
坑③（叠加塌陷）：floor 地板。
坑④（跨策略可比）：value 统一归一到 0~1。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
import loguru

logger = loguru.logger


@dataclass
class PenaltyDetail:
    """单因子扣分明细。"""
    factor: str
    value: Any                   # 数值或类别值；缺失为 None
    penalty: float
    band: Optional[list] = None  # 命中的 [阈值, 扣分] 区间（类别型为 None）
    missing: bool = False        # 因子值缺失（按最差扣分）


@dataclass
class ConfidenceResult:
    """置信度评分结果。"""
    value: float                       # 归一化 0~1（下游统一口径）
    raw: float                         # 0~100，已应用 floor/ceiling
    ceiling: float
    floor: float
    total_penalty: float
    breakdown: List[PenaltyDetail] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": round(self.value, 4),
            "raw": round(self.raw, 2),
            "ceiling": self.ceiling,
            "floor": self.floor,
            "total_penalty": round(self.total_penalty, 2),
            "breakdown": [
                {"factor": d.factor, "value": d.value, "penalty": d.penalty,
                 "missing": d.missing}
                for d in self.breakdown
            ],
        }


class ConfidenceScorer:
    """声明式置信度扣分器。无状态、纯函数式，便于单测与复用。"""

    def __init__(self, ruleset: Dict[str, Any]):
        self.ceiling = float(ruleset.get("ceiling", 95))
        self.floor = float(ruleset.get("floor", 40))
        raw_rules = ruleset.get("rules", []) or []
        self.rules: List[Dict[str, Any]] = []
        for r in raw_rules:
            # direction: "higher_better"（默认，value>=阈值）/ "lower_better"（value<=阈值）
            direction = r.get("direction", "higher_better")
            bands = [list(b) for b in (r.get("bands") or [])]
            # higher_better：阈值降序取第一个满足；lower_better：阈值升序取第一个满足
            bands.sort(key=lambda b: b[0], reverse=(direction != "lower_better"))
            self.rules.append({
                "factor": r.get("factor"),
                "enabled": bool(r.get("enabled", True)),
                "bands": bands,
                "direction": direction,
                # 类别型因子（如走弱类型/突破类型）：{类别值: 扣分} + default
                "mapping": dict(r.get("mapping") or {}),
                "default": r.get("default", 0),
            })

    @staticmethod
    def _band_penalty(value: float, bands: List[list], direction: str = "higher_better") -> list:
        """
        取第一个满足的区间：
        - higher_better：bands 已按阈值降序，命中 value >= 阈值；
        - lower_better：bands 已按阈值升序，命中 value <= 阈值。
        都不满足则取最后一个区间（最差）。
        """
        for threshold, penalty in bands:
            if direction == "lower_better":
                if value <= threshold:
                    return [threshold, penalty]
            else:
                if value >= threshold:
                    return [threshold, penalty]
        return bands[-1] if bands else [0, 0]

    def score(self, factors: Dict[str, Any]) -> ConfidenceResult:
        """
        Args:
            factors: {factor_name: value}。布尔值会被当作 1/0 处理。缺失因子按该规则
                     bands 的**最大扣分**计（坑①：未验证项默认重扣）。
        Returns:
            ConfidenceResult，含归一化 value(0~1) 与逐项 breakdown。
        """
        breakdown: List[PenaltyDetail] = []
        total_penalty = 0.0

        for rule in self.rules:
            factor = rule["factor"]
            bands = rule["bands"]
            mapping = rule["mapping"]
            if not rule["enabled"] or (not bands and not mapping):
                continue

            raw_val = factors.get(factor, None)

            # 类别型因子：按 mapping 取扣分；缺失按最差（最大）扣分
            if mapping:
                if raw_val is None:
                    penalty = max(list(mapping.values()) + [rule["default"]])
                    total_penalty += penalty
                    breakdown.append(PenaltyDetail(
                        factor=factor, value=None, penalty=penalty, band=None, missing=True
                    ))
                    continue
                penalty = float(mapping.get(raw_val, rule["default"]))
                total_penalty += penalty
                breakdown.append(PenaltyDetail(
                    factor=factor, value=raw_val, penalty=penalty, band=None, missing=False
                ))
                continue

            if raw_val is None:
                # 缺失：按最差（最大）扣分
                penalty = max((b[1] for b in bands), default=0.0)
                total_penalty += penalty
                breakdown.append(PenaltyDetail(
                    factor=factor, value=None, penalty=penalty, band=None, missing=True
                ))
                continue

            value = float(raw_val) if not isinstance(raw_val, bool) else (1.0 if raw_val else 0.0)
            band = self._band_penalty(value, bands, rule["direction"])
            penalty = float(band[1])
            total_penalty += penalty
            breakdown.append(PenaltyDetail(
                factor=factor, value=value, penalty=penalty, band=band, missing=False
            ))

        raw = max(self.floor, self.ceiling - total_penalty)
        raw = min(raw, self.ceiling)
        return ConfidenceResult(
            value=raw / 100.0,
            raw=raw,
            ceiling=self.ceiling,
            floor=self.floor,
            total_penalty=total_penalty,
            breakdown=breakdown,
        )


# ============================================================
# 规则集加载（带缓存）
# ============================================================

_RULES_CACHE: Optional[Dict[str, Any]] = None


def _default_rules_path() -> Path:
    return Path(__file__).parent.parent.parent / "config" / "confidence_rules.yaml"


def load_confidence_rules(path: Optional[Path] = None, *, use_cache: bool = True) -> Dict[str, Any]:
    """加载 confidence_rules.yaml，返回 {strategy_name: ruleset}。"""
    global _RULES_CACHE
    if use_cache and path is None and _RULES_CACHE is not None:
        return _RULES_CACHE

    p = Path(path) if path else _default_rules_path()
    if not p.exists():
        logger.warning(f"[ConfidenceScorer] 规则文件不存在: {p}")
        rules: Dict[str, Any] = {}
    else:
        try:
            with open(p, "r", encoding="utf-8") as f:
                rules = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"[ConfidenceScorer] 加载规则失败: {e}")
            rules = {}

    if use_cache and path is None:
        _RULES_CACHE = rules
    return rules


def get_scorer(strategy_name: str, path: Optional[Path] = None) -> Optional[ConfidenceScorer]:
    """按策略名取 ConfidenceScorer；规则缺失返回 None（调用方回退旧逻辑）。"""
    rules = load_confidence_rules(path)
    ruleset = rules.get(strategy_name)
    if not ruleset:
        return None
    return ConfidenceScorer(ruleset)


def score_or_none(strategy_name: str, factors: Dict[str, Any]) -> Optional[ConfidenceResult]:
    """
    便捷封装：按策略名打分。规则缺失或异常 → 返回 None，调用方据此回退旧逻辑。
    """
    try:
        scorer = get_scorer(strategy_name)
        if scorer is None:
            return None
        return scorer.score(factors)
    except Exception as e:
        logger.warning(f"[ConfidenceScorer] {strategy_name} 打分失败，回退旧逻辑: {e}")
        return None