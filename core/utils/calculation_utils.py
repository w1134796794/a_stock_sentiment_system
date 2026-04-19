"""
计算工具模块 - 提供股票相关的通用计算函数
包括涨跌幅、换手率、市值计算等
"""
from typing import Dict, Optional, Tuple
import pandas as pd


class CalculationUtils:
    """计算工具类 - 提供股票相关的通用计算函数"""

    @staticmethod
    def calculate_gap(open_price: float, previous_close: float) -> float:
        """
        计算开盘跳空幅度

        Args:
            open_price: 开盘价
            previous_close: 前收盘价

        Returns:
            跳空幅度（小数，如0.02表示高开2%）
        """
        if previous_close <= 0:
            return 0.0
        return round((open_price - previous_close) / previous_close, 4)

    @staticmethod
    def calculate_drawdown(high: float, low: float, reference_price: float) -> float:
        """
        计算回撤幅度

        Args:
            high: 最高价
            low: 最低价
            reference_price: 参考价格（如涨停价）

        Returns:
            回撤幅度（小数）
        """
        if reference_price <= 0:
            return 0.0
        return round((high - low) / reference_price, 4)

    @staticmethod
    def calculate_volume_ratio(current_vol: float, previous_vol: float) -> float:
        """
        计算量比

        Args:
            current_vol: 当前成交量
            previous_vol: 前一日成交量

        Returns:
            量比（倍数）
        """
        if previous_vol <= 0:
            return 0.0
        return round(current_vol / previous_vol, 2)

    @staticmethod
    def calculate_confidence_score(metrics: Dict[str, float],
                                   weights: Dict[str, float]) -> float:
        """
        计算加权置信度分数

        Args:
            metrics: 指标值字典
            weights: 权重字典

        Returns:
            加权后的置信度（0-1之间）
        """
        if not metrics or not weights:
            return 0.0

        total_score = 0.0
        total_weight = 0.0

        for key, value in metrics.items():
            if key in weights:
                weight = weights[key]
                total_score += value * weight
                total_weight += weight

        if total_weight == 0:
            return 0.0

        # 归一化到0-1
        normalized_score = total_score / total_weight
        return round(min(max(normalized_score, 0.0), 1.0), 2)

    @staticmethod
    def calculate_moving_average(data: pd.Series, window: int) -> pd.Series:
        """
        计算移动平均线

        Args:
            data: 价格序列
            window: 窗口大小

        Returns:
            移动平均序列
        """
        return data.rolling(window=window, min_periods=1).mean()

    @staticmethod
    def calculate_rsi(data: pd.Series, period: int = 14) -> float:
        """
        计算RSI相对强弱指标

        Args:
            data: 价格序列
            period: 计算周期

        Returns:
            RSI值（0-100）
        """
        if len(data) < period:
            return 50.0

        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        return round(rsi.iloc[-1], 2) if not pd.isna(rsi.iloc[-1]) else 50.0

    @staticmethod
    def calculate_score(conditions: list) -> Tuple[int, list]:
        """
        基于多个条件计算评分

        Args:
            conditions: 条件列表，每个元素为 (是否满足, 满足加分, 不满足减分, 描述)

        Returns:
            (总分, 评分详情列表)
        """
        score = 60  # 基础分
        details = []

        for condition, add_score, minus_score, desc in conditions:
            if condition:
                score += add_score
                details.append(f"{desc}: +{add_score}")
            else:
                score -= minus_score
                details.append(f"{desc}: -{minus_score}")

        return score, details

    @staticmethod
    def normalize_value(value: float, min_val: float, max_val: float) -> float:
        """
        归一化数值到0-1范围

        Args:
            value: 原始值
            min_val: 最小值
            max_val: 最大值

        Returns:
            归一化后的值（0-1）
        """
        if max_val <= min_val:
            return 0.5

        normalized = (value - min_val) / (max_val - min_val)
        return round(min(max(normalized, 0.0), 1.0), 4)

    @staticmethod
    def calculate_position_size(confidence: float,
                               risk_level: str = 'medium',
                               max_position: float = 1.0) -> str:
        """
        根据置信度计算建议仓位

        Args:
            confidence: 置信度（0-1）
            risk_level: 风险等级（'low', 'medium', 'high'）
            max_position: 最大仓位比例

        Returns:
            仓位建议字符串
        """
        risk_multipliers = {
            'low': 1.0,
            'medium': 0.7,
            'high': 0.5
        }

        multiplier = risk_multipliers.get(risk_level, 0.7)
        adjusted_confidence = confidence * multiplier

        if adjusted_confidence >= 0.8:
            return "heavy"
        elif adjusted_confidence >= 0.6:
            return "medium"
        elif adjusted_confidence >= 0.4:
            return "light"
        else:
            return "watch"

    @staticmethod
    def calculate_stop_loss(entry_price: float,
                           stop_loss_pct: float = 0.07,
                           min_stop_price: float = None) -> float:
        """
        计算止损价格

        Args:
            entry_price: 买入价格
            stop_loss_pct: 止损比例（默认7%）
            min_stop_price: 最低止损价（如昨日最低价）

        Returns:
            止损价格
        """
        stop_price = entry_price * (1 - stop_loss_pct)

        if min_stop_price and min_stop_price > stop_price:
            stop_price = min_stop_price

        return round(stop_price, 2)

    @staticmethod
    def calculate_take_profit(entry_price: float,
                             take_profit_pct: float = 0.15) -> float:
        """
        计算止盈价格

        Args:
            entry_price: 买入价格
            take_profit_pct: 止盈比例（默认15%）

        Returns:
            止盈价格
        """
        return round(entry_price * (1 + take_profit_pct), 2)

    @staticmethod
    def calculate_risk_reward_ratio(entry: float, stop_loss: float, take_profit: float) -> float:
        """
        计算盈亏比

        Args:
            entry: 买入价
            stop_loss: 止损价
            take_profit: 止盈价

        Returns:
            盈亏比
        """
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)

        if risk <= 0:
            return 0.0

        return round(reward / risk, 2)


# 保持向后兼容的函数接口
calculate_gap = CalculationUtils.calculate_gap
calculate_drawdown = CalculationUtils.calculate_drawdown
calculate_volume_ratio = CalculationUtils.calculate_volume_ratio
calculate_confidence_score = CalculationUtils.calculate_confidence_score
calculate_moving_average = CalculationUtils.calculate_moving_average
calculate_rsi = CalculationUtils.calculate_rsi
calculate_score = CalculationUtils.calculate_score
normalize_value = CalculationUtils.normalize_value
calculate_position_size = CalculationUtils.calculate_position_size
calculate_stop_loss = CalculationUtils.calculate_stop_loss
calculate_take_profit = CalculationUtils.calculate_take_profit
calculate_risk_reward_ratio = CalculationUtils.calculate_risk_reward_ratio


if __name__ == "__main__":
    # 测试
    print("计算工具测试:")
    print(f"  跳空: {CalculationUtils.calculate_gap(10.5, 10.0)*100:.2f}%")
    print(f"  回撤: {CalculationUtils.calculate_drawdown(11.0, 10.5, 11.0)*100:.2f}%")
    print(f"  量比: {CalculationUtils.calculate_volume_ratio(1500000, 1000000)}")
    print(f"  止损价: {CalculationUtils.calculate_stop_loss(10.0)}")
    print(f"  止盈价: {CalculationUtils.calculate_take_profit(10.0)}")
    print(f"  盈亏比: {CalculationUtils.calculate_risk_reward_ratio(10.0, 9.3, 11.5)}")

    # 评分测试
    conditions = [
        (True, 20, 0, "换手达标"),
        (False, 10, 15, "烂板次数适中"),
        (True, 10, 0, "尾盘回封"),
    ]
    score, details = CalculationUtils.calculate_score(conditions)
    print(f"\n评分测试: {score}分")
    for d in details:
        print(f"  {d}")
