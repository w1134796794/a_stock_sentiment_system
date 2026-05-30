"""
仓位管理器
根据市场状态和策略信号动态调整仓位
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from enum import Enum
import loguru

logger = loguru.logger


class MarketCondition(Enum):
    """市场状态"""
    BULL = "牛市"
    BEAR = "熊市"
    OSCILLATION = "震荡"
    UNKNOWN = "未知"


class PositionSizer:
    """
    仓位管理器
    基于市场情绪和策略质量动态调整仓位
    """

    def __init__(self):
        # 基础仓位配置
        self.base_position = {
            'light': 0.10,
            'medium': 0.15,
            'heavy': 0.20
        }

        # 市场环境调整系数
        self.market_adjustment = {
            MarketCondition.BULL: 1.3,      # 牛市增加30%
            MarketCondition.BEAR: 0.5,      # 熊市减少50%
            MarketCondition.OSCILLATION: 0.8,  # 震荡减少20%
            MarketCondition.UNKNOWN: 0.6    # 未知减少40%
        }

        # 策略质量调整系数
        self.quality_thresholds = {
            'high': 0.85,    # 高置信度阈值
            'medium': 0.70,  # 中等置信度阈值
            'low': 0.50      # 低置信度阈值
        }

        self.quality_multipliers = {
            'high': 1.2,
            'medium': 1.0,
            'low': 0.7
        }

    def calculate_position(self,
                          signal_quality: float,
                          market_condition: MarketCondition,
                          base_size: str = 'medium',
                          hot_resonance: bool = False,
                          sector_heat_score: float = 0) -> Dict:
        """
        计算建议仓位

        Args:
            signal_quality: 信号质量/置信度 (0-1)
            market_condition: 市场环境
            base_size: 基础仓位大小 (light/medium/heavy)
            hot_resonance: 是否与热点共振
            sector_heat_score: 板块热度评分

        Returns:
            {'position_pct': float, 'position_value': float, 'rationale': str}
        """
        # 1. 基础仓位
        base = self.base_position.get(base_size, 0.15)

        # 2. 市场环境调整
        market_adj = self.market_adjustment.get(market_condition, 0.8)

        # 3. 信号质量调整
        if signal_quality >= self.quality_thresholds['high']:
            quality_adj = self.quality_multipliers['high']
            quality_desc = '高置信'
        elif signal_quality >= self.quality_thresholds['medium']:
            quality_adj = self.quality_multipliers['medium']
            quality_desc = '中等置信'
        else:
            quality_adj = self.quality_multipliers['low']
            quality_desc = '低置信'

        # 4. 热点共振调整
        resonance_adj = 1.0
        if hot_resonance:
            # 根据板块热度评分调整
            if sector_heat_score >= 80:
                resonance_adj = 1.25
                resonance_desc = '强共振'
            elif sector_heat_score >= 60:
                resonance_adj = 1.15
                resonance_desc = '中共振'
            else:
                resonance_adj = 1.10
                resonance_desc = '弱共振'
        else:
            resonance_desc = '无共振'

        # 5. 计算最终仓位比例
        position_pct = base * market_adj * quality_adj * resonance_adj

        # 6. 限制在合理范围内
        position_pct = min(max(position_pct, 0.05), 0.25)  # 5%-25%

        rationale = (
            f"基础仓位{base:.0%} × 市场{market_condition.value}{market_adj:.1f} × "
            f"{quality_desc}{quality_adj:.1f} × {resonance_desc}{resonance_adj:.1f} = "
            f"建议仓位{position_pct:.1%}"
        )

        return {
            'position_pct': position_pct,
            'base_position': base,
            'market_adjustment': market_adj,
            'quality_adjustment': quality_adj,
            'resonance_adjustment': resonance_adj,
            'rationale': rationale
        }

    def get_market_condition(self, emotion_result: Dict) -> MarketCondition:
        """
        根据情绪周期判断市场环境

        Args:
            emotion_result: 情绪周期分析结果

        Returns:
            MarketCondition
        """
        cycle_name = emotion_result.get('cycle_name', '震荡期')

        # 情绪周期映射到市场环境
        bull_cycles = ['启动期', '发酵期', '高潮期']
        bear_cycles = ['退潮期', '冰点期']
        oscillation_cycles = ['震荡期', '修复期']

        if cycle_name in bull_cycles:
            return MarketCondition.BULL
        elif cycle_name in bear_cycles:
            return MarketCondition.BEAR
        elif cycle_name in oscillation_cycles:
            return MarketCondition.OSCILLATION
        else:
            return MarketCondition.UNKNOWN

    def get_portfolio_position_limit(self,
                                     market_condition: MarketCondition,
                                     win_rate_recent: float = 0.5) -> float:
        """
        获取组合总仓位限制

        Args:
            market_condition: 市场环境
            win_rate_recent: 近期胜率

        Returns:
            总仓位上限 (0-1)
        """
        base_limits = {
            MarketCondition.BULL: 0.90,
            MarketCondition.BEAR: 0.30,
            MarketCondition.OSCILLATION: 0.60,
            MarketCondition.UNKNOWN: 0.50
        }

        base_limit = base_limits.get(market_condition, 0.50)

        # 根据近期胜率调整
        if win_rate_recent > 0.6:
            win_adj = 1.1
        elif win_rate_recent < 0.4:
            win_adj = 0.8
        else:
            win_adj = 1.0

        final_limit = base_limit * win_adj
        return min(final_limit, 1.0)

    def calculate_position_matrix(self,
                                  market_condition: MarketCondition,
                                  available_signals: int = 5) -> Dict:
        """
        计算仓位配置矩阵

        Returns:
            各类型信号的仓位配置建议
        """
        # 根据市场环境确定总仓位
        total_position = self.get_portfolio_position_limit(market_condition)

        # 根据信号数量分配
        if available_signals >= 5:
            # 信号充足，分散配置
            per_signal = total_position / min(available_signals, 5)
        elif available_signals >= 3:
            # 信号适中
            per_signal = total_position / available_signals
        else:
            # 信号较少，集中配置
            per_signal = total_position / max(available_signals, 1)

        # 限制单票最大仓位
        per_signal = min(per_signal, 0.20)

        return {
            'total_position_limit': total_position,
            'per_signal_position': per_signal,
            'max_signals': min(available_signals, 5),
            'market_condition': market_condition.value,
            'rationale': f"市场环境{market_condition.value}，总仓位{total_position:.0%}，单票{per_signal:.0%}"
        }
