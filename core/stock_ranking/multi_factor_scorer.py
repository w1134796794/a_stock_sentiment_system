"""
多因子综合评分器 - Multi-Factor Scorer

评分公式：
  Score = 模式质量(0.35) × 板块强度(0.30) × 个股地位(0.20) × 情绪适配(0.15)

用于对筛选出的标的进行综合排序，辅助交易决策
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


def action_for_score(total_score: float) -> Tuple[str, float]:
    """综合分 → (建议操作, 建议仓位比例)。

    单一事实来源：``_assign_actions`` 与 Sprint F-7 游资调整后重算建议均复用此函数，
    避免阈值散落多处。
    """
    if total_score >= 80:
        return "重仓买入", 0.40
    if total_score >= 70:
        return "标准仓位", 0.25
    if total_score >= 60:
        return "轻仓试错", 0.15
    if total_score >= 50:
        return "观察", 0.05
    return "放弃", 0.0


@dataclass
class FactorScore:
    """单因子评分"""
    name: str
    raw_value: float
    normalized_score: float     # 0-100
    weight: float


@dataclass
class CompositeScore:
    """综合评分结果"""
    stock_code: str
    stock_name: str
    pattern_type: str

    # 各因子评分
    pattern_quality: FactorScore = None      # 模式质量
    sector_strength: FactorScore = None      # 板块强度
    stock_position: FactorScore = None       # 个股地位
    emotion_fit: FactorScore = None          # 情绪适配

    # 综合
    total_score: float = 0.0
    rank: int = 0

    # 交易建议
    suggested_action: str = ""               # 建议操作
    suggested_position_pct: float = 0.0      # 建议仓位比例

    # Sprint F-7：龙虎榜游资信誉调整
    lhb_adjust_delta: float = 0.0            # 因游资信誉对总分的增减
    lhb_adjust_note: str = ""                # 调整说明（黑名单接盘/优质游资进场等）


class MultiFactorScorer:
    """
    多因子综合评分器

    对筛选出的标的进行多维度评分排序
    """

    def __init__(self):
        # 因子权重
        self.weights = {
            'pattern_quality': 0.35,     # 模式质量
            'sector_strength': 0.30,     # 板块强度
            'stock_position': 0.20,      # 个股地位
            'emotion_fit': 0.15,         # 情绪适配
        }

        # 情绪周期适配映射
        self.emotion_fit_map = {
            '冰点期': {
                '弱转强': 90, '二板定龙': 70, '首板突破': 50, '龙二波': 40,
            },
            '上升期': {
                '弱转强': 85, '二板定龙': 95, '首板突破': 80, '龙二波': 75,
            },
            '高潮期': {
                '弱转强': 60, '二板定龙': 70, '首板突破': 50, '龙二波': 80,
            },
            '退潮期': {
                '弱转强': 70, '二板定龙': 50, '首板突破': 30, '龙二波': 40,
            },
            '震荡期': {
                '弱转强': 75, '二板定龙': 65, '首板突破': 60, '龙二波': 55,
            },
        }

        logger.info("[MultiFactorScorer] 初始化完成")

    def score_signals(self, ranked_signals: List,
                      sector_position_results: Dict = None,
                      emotion_cycle: str = '震荡期',
                      sector_heat_map: Dict[str, float] = None) -> List[CompositeScore]:
        """
        对信号列表进行多因子综合评分

        Args:
            ranked_signals: 优先级排序后的信号列表（RankedSignal）
            sector_position_results: 个股板块地位分析结果
            emotion_cycle: 当前情绪周期
            sector_heat_map: 板块热度映射 {板块名: 热度评分}

        Returns:
            综合评分列表（按总分降序）
        """
        if not ranked_signals:
            return []

        logger.info(f"[MultiFactor] 开始对{len(ranked_signals)}个信号进行综合评分...")

        results = []
        for sig in ranked_signals:
            score = self._score_single(sig, sector_position_results,
                                       emotion_cycle, sector_heat_map)
            results.append(score)

        # 按总分排序
        results.sort(key=lambda x: x.total_score, reverse=True)

        # 分配排名
        for i, r in enumerate(results):
            r.rank = i + 1

        # 分配交易建议
        self._assign_actions(results)

        # 打印评分结果
        for r in results[:10]:
            logger.info(f"[MultiFactor] #{r.rank} {r.stock_name}({r.stock_code}) "
                       f"总分={r.total_score:.1f} "
                       f"模式={r.pattern_quality.normalized_score:.0f} "
                       f"板块={r.sector_strength.normalized_score:.0f} "
                       f"地位={r.stock_position.normalized_score:.0f} "
                       f"情绪={r.emotion_fit.normalized_score:.0f} "
                       f"→ {r.suggested_action} {r.suggested_position_pct:.0%}")

        return results

    def _score_single(self, sig,
                      sector_position_results: Dict,
                      emotion_cycle: str,
                      sector_heat_map: Dict[str, float]) -> CompositeScore:
        """对单个信号进行评分"""
        stock_code = sig.stock_code
        stock_name = sig.stock_name
        pattern_type = sig.pattern_type

        # 1. 模式质量评分（基于置信度）
        pattern_raw = sig.confidence
        pattern_norm = min(100, pattern_raw * 100)
        pattern_factor = FactorScore(
            name='模式质量',
            raw_value=pattern_raw,
            normalized_score=pattern_norm,
            weight=self.weights['pattern_quality'],
        )

        # 2. 板块强度评分
        sector_raw = sig.sector_heat_score
        if sector_heat_map and sig.sector_name in sector_heat_map:
            sector_raw = max(sector_raw, sector_heat_map[sig.sector_name] / 100)
        sector_norm = min(100, sector_raw * 100) if sector_raw > 0 else 50
        sector_factor = FactorScore(
            name='板块强度',
            raw_value=sector_raw,
            normalized_score=sector_norm,
            weight=self.weights['sector_strength'],
        )

        # 3. 个股地位评分
        position_raw = 0.5
        if sector_position_results and stock_code in sector_position_results:
            pos_result = sector_position_results[stock_code]
            position_raw = pos_result.position_score / 100
        position_norm = position_raw * 100
        position_factor = FactorScore(
            name='个股地位',
            raw_value=position_raw,
            normalized_score=position_norm,
            weight=self.weights['stock_position'],
        )

        # 4. 情绪适配评分
        emotion_fit_map = self.emotion_fit_map.get(emotion_cycle, {})
        emotion_raw = emotion_fit_map.get(pattern_type, 50) / 100
        emotion_norm = emotion_raw * 100
        emotion_factor = FactorScore(
            name='情绪适配',
            raw_value=emotion_raw,
            normalized_score=emotion_norm,
            weight=self.weights['emotion_fit'],
        )

        # 综合评分
        total = (
            pattern_norm * self.weights['pattern_quality'] +
            sector_norm * self.weights['sector_strength'] +
            position_norm * self.weights['stock_position'] +
            emotion_norm * self.weights['emotion_fit']
        )

        return CompositeScore(
            stock_code=stock_code,
            stock_name=stock_name,
            pattern_type=pattern_type,
            pattern_quality=pattern_factor,
            sector_strength=sector_factor,
            stock_position=position_factor,
            emotion_fit=emotion_factor,
            total_score=total,
        )

    def _assign_actions(self, results: List[CompositeScore]):
        """根据评分分配交易建议"""
        for r in results:
            r.suggested_action, r.suggested_position_pct = action_for_score(r.total_score)

    def to_dataframe(self, results: List[CompositeScore]) -> pd.DataFrame:
        """将评分结果转换为DataFrame"""
        rows = []
        for r in results:
            rows.append({
                '排名': r.rank,
                '代码': r.stock_code,
                '名称': r.stock_name,
                '模式': r.pattern_type,
                '总分': round(r.total_score, 1),
                '模式质量': round(r.pattern_quality.normalized_score, 1),
                '板块强度': round(r.sector_strength.normalized_score, 1),
                '个股地位': round(r.stock_position.normalized_score, 1),
                '情绪适配': round(r.emotion_fit.normalized_score, 1),
                '建议操作': r.suggested_action,
                '建议仓位': f"{r.suggested_position_pct:.0%}",
            })

        return pd.DataFrame(rows)

    def get_top_picks(self, results: List[CompositeScore],
                      top_n: int = 5) -> List[CompositeScore]:
        """获取Top N推荐"""
        return [r for r in results if r.suggested_action != '放弃'][:top_n]
