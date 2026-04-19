"""
风险管理器
实现硬风控规则：
1. 单票仓位限制
2. 总仓位限制
3. 止损线强制执行
4. 板块集中度限制
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
import loguru

logger = loguru.logger


@dataclass
class RiskLimits:
    """风控限制配置"""
    # 仓位限制
    max_position_per_stock: float = 0.20  # 单票最大20%
    max_total_position: float = 0.80  # 总仓位最大80%
    min_cash_ratio: float = 0.20  # 最小现金比例20%

    # 板块集中度限制
    max_sector_concentration: float = 0.40  # 单一板块最大40%
    max_correlated_positions: int = 3  # 相关板块最多持仓数

    # 止损限制
    hard_stop_loss: float = 0.05  # 硬止损5%
    trailing_stop: float = 0.08  # 移动止损8%
    max_daily_loss: float = 0.03  # 单日最大亏损3%

    # 流动性限制
    min_daily_volume: float = 10000000  # 最小日成交额1000万
    max_position_in_small_cap: float = 0.10  # 小盘股最大仓位10%


class RiskManager:
    """
    风险管理器
    严格执行风控规则，不可绕过
    """

    def __init__(self, risk_limits: RiskLimits = None):
        self.limits = risk_limits or RiskLimits()
        self.violation_history: List[Dict] = []

    def check_position_limits(self,
                              new_position_value: float,
                              current_positions: Dict,
                              total_capital: float) -> Dict:
        """
        检查仓位限制

        Returns:
            {'allowed': bool, 'reason': str, 'max_allowed': float}
        """
        current_position_value = sum(pos.get('market_value', 0) for pos in current_positions.values())

        # 检查单票限制
        if new_position_value > total_capital * self.limits.max_position_per_stock:
            max_allowed = total_capital * self.limits.max_position_per_stock
            return {
                'allowed': False,
                'reason': f'单票仓位超限: 最大允许{self.limits.max_position_per_stock:.0%}',
                'max_allowed': max_allowed
            }

        # 检查总仓位限制
        new_total = current_position_value + new_position_value
        if new_total > total_capital * self.limits.max_total_position:
            max_allowed = total_capital * self.limits.max_total_position - current_position_value
            return {
                'allowed': False,
                'reason': f'总仓位超限: 当前{current_position_value/total_capital:.1%}, 最大{self.limits.max_total_position:.0%}',
                'max_allowed': max(max_allowed, 0)
            }

        # 检查现金比例
        cash = total_capital - current_position_value
        remaining_cash = cash - new_position_value
        if remaining_cash < total_capital * self.limits.min_cash_ratio:
            max_allowed = cash - total_capital * self.limits.min_cash_ratio
            return {
                'allowed': False,
                'reason': f'现金不足: 需保留{self.limits.min_cash_ratio:.0%}现金',
                'max_allowed': max(max_allowed, 0)
            }

        return {'allowed': True, 'reason': '通过', 'max_allowed': new_position_value}

    def check_sector_concentration(self,
                                   sector: str,
                                   new_position_value: float,
                                   current_positions: Dict,
                                   total_capital: float) -> Dict:
        """检查板块集中度"""
        # 计算当前板块持仓
        sector_value = 0
        for pos in current_positions.values():
            if pos.get('sector') == sector:
                sector_value += pos.get('market_value', 0)

        new_sector_value = sector_value + new_position_value
        sector_ratio = new_sector_value / total_capital

        if sector_ratio > self.limits.max_sector_concentration:
            max_allowed = total_capital * self.limits.max_sector_concentration - sector_value
            return {
                'allowed': False,
                'reason': f'板块{sector}集中度超限: {sector_ratio:.1%} > {self.limits.max_sector_concentration:.0%}',
                'max_allowed': max(max_allowed, 0)
            }

        return {'allowed': True, 'reason': '通过', 'max_allowed': new_position_value}

    def check_stop_loss(self,
                        entry_price: float,
                        current_price: float,
                        highest_price: float) -> Dict:
        """
        检查止损条件

        Returns:
            {'should_stop': bool, 'stop_type': str, 'stop_price': float}
        """
        # 硬止损
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= self.limits.hard_stop_loss:
            return {
                'should_stop': True,
                'stop_type': 'HARD_STOP',
                'stop_price': current_price,
                'reason': f'触发硬止损: 亏损{loss_pct:.2%} >= {self.limits.hard_stop_loss:.2%}'
            }

        # 移动止损
        if highest_price > entry_price:
            pullback_pct = (highest_price - current_price) / highest_price
            if pullback_pct >= self.limits.trailing_stop:
                return {
                    'should_stop': True,
                    'stop_type': 'TRAILING_STOP',
                    'stop_price': current_price,
                    'reason': f'触发移动止损: 回撤{pullback_pct:.2%} >= {self.limits.trailing_stop:.2%}'
                }

        return {'should_stop': False, 'stop_type': None, 'stop_price': None, 'reason': '未触发'}

    def check_liquidity(self,
                        stock_code: str,
                        position_value: float,
                        avg_daily_volume: float) -> Dict:
        """检查流动性风险"""
        if avg_daily_volume < self.limits.min_daily_volume:
            return {
                'allowed': False,
                'reason': f'流动性不足: 日均成交额{avg_daily_volume/1e8:.2f}亿 < {self.limits.min_daily_volume/1e8:.2f}亿',
                'max_allowed': 0
            }

        # 检查持仓是否超过日成交额的10%（影响退出）
        max_position = avg_daily_volume * 0.10
        if position_value > max_position:
            return {
                'allowed': False,
                'reason': f'持仓超限: 建议最大持仓{max_position/1e4:.0f}万（基于流动性）',
                'max_allowed': max_position
            }

        return {'allowed': True, 'reason': '通过', 'max_allowed': position_value}

    def validate_trade_plan(self,
                           plan: Dict,
                           current_positions: Dict,
                           total_capital: float,
                           market_data: Dict) -> Dict:
        """
        全面验证交易计划

        Returns:
            {'valid': bool, 'violations': List[str], 'adjusted_plan': Dict}
        """
        violations = []
        adjusted_plan = plan.copy()

        # 1. 检查仓位限制
        position_value = plan.get('position_value', 0)
        position_check = self.check_position_limits(position_value, current_positions, total_capital)
        if not position_check['allowed']:
            violations.append(position_check['reason'])
            adjusted_plan['position_value'] = position_check['max_allowed']

        # 2. 检查板块集中度
        sector = plan.get('sector', '未知')
        sector_check = self.check_sector_concentration(
            sector, adjusted_plan['position_value'], current_positions, total_capital
        )
        if not sector_check['allowed']:
            violations.append(sector_check['reason'])
            adjusted_plan['position_value'] = min(adjusted_plan['position_value'], sector_check['max_allowed'])

        # 3. 检查流动性
        stock_code = plan.get('stock_code', '')
        avg_volume = market_data.get('avg_daily_volume', 0)
        liquidity_check = self.check_liquidity(stock_code, adjusted_plan['position_value'], avg_volume)
        if not liquidity_check['allowed']:
            violations.append(liquidity_check['reason'])
            adjusted_plan['position_value'] = min(adjusted_plan['position_value'], liquidity_check['max_allowed'])

        # 记录违规
        if violations:
            self.violation_history.append({
                'timestamp': datetime.now(),
                'stock_code': stock_code,
                'violations': violations,
                'original_value': position_value,
                'adjusted_value': adjusted_plan['position_value']
            })
            logger.warning(f"交易计划风控调整: {stock_code} - {', '.join(violations)}")

        return {
            'valid': len(violations) == 0,
            'violations': violations,
            'adjusted_plan': adjusted_plan
        }

    def get_risk_report(self) -> Dict:
        """生成风险报告"""
        return {
            'violation_count': len(self.violation_history),
            'recent_violations': self.violation_history[-10:] if self.violation_history else [],
            'risk_limits': {
                'max_position_per_stock': self.limits.max_position_per_stock,
                'max_total_position': self.limits.max_total_position,
                'hard_stop_loss': self.limits.hard_stop_loss,
                'max_sector_concentration': self.limits.max_sector_concentration
            }
        }