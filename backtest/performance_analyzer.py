"""
性能分析器
计算回测的各项性能指标
"""
import pandas as pd
import numpy as np
from typing import Dict, List
from datetime import datetime
import loguru

logger = loguru.logger


class PerformanceAnalyzer:
    """
    性能分析器
    计算策略回测的各项关键指标
    """

    def __init__(self):
        self.risk_free_rate = 0.03  # 无风险利率3%

    def calculate_all_metrics(self, backtest_result: Dict) -> Dict:
        """
        计算所有性能指标

        Args:
            backtest_result: 回测结果

        Returns:
            完整性能指标报告
        """
        metrics = {
            'returns': self._calculate_return_metrics(backtest_result),
            'risk': self._calculate_risk_metrics(backtest_result),
            'trades': self._calculate_trade_metrics(backtest_result),
            'patterns': self._calculate_pattern_metrics(backtest_result),
            'resonance': self._calculate_resonance_metrics(backtest_result)
        }

        return metrics

    def _calculate_return_metrics(self, result: Dict) -> Dict:
        """计算收益指标"""
        initial = result.get('initial_capital', 100000)
        final = result.get('final_capital', 100000)
        total_return = result.get('total_return', (final - initial) / initial if initial > 0 else 0)

        daily_nav = result.get('daily_nav', [])
        if not daily_nav:
            # 返回基本字段，避免KeyError
            return {
                'total_return': total_return,
                'total_return_pct': f"{total_return:.2%}",
                'annualized_return': 0,
                'annualized_return_pct': "0.00%",
                'volatility': 0,
                'volatility_pct': "0.00%",
                'sharpe_ratio': 0,
                'sortino_ratio': 0,
                'calmar_ratio': 0,
                'initial_capital': initial,
                'final_capital': final,
                'profit_amount': final - initial
            }

        nav_df = pd.DataFrame(daily_nav)
        nav_df['daily_return'] = nav_df['total_value'].pct_change()

        # 年化收益
        days = len(nav_df)
        annualized_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0

        # 波动率
        volatility = nav_df['daily_return'].std() * np.sqrt(252) if len(nav_df) > 1 else 0

        # Sharpe比率
        excess_return = annualized_return - self.risk_free_rate
        sharpe_ratio = excess_return / volatility if volatility > 0 else 0

        # Sortino比率（只考虑下行波动）
        downside_returns = nav_df['daily_return'][nav_df['daily_return'] < 0]
        downside_std = downside_returns.std() * np.sqrt(252) if len(downside_returns) > 0 else 0
        sortino_ratio = excess_return / downside_std if downside_std > 0 else 0

        # Calmar比率（收益/最大回撤）
        max_dd = result.get('max_drawdown', 0)
        calmar_ratio = annualized_return / abs(max_dd) if max_dd != 0 else 0

        return {
            'total_return': total_return,
            'total_return_pct': f"{total_return:.2%}",
            'annualized_return': annualized_return,
            'annualized_return_pct': f"{annualized_return:.2%}",
            'volatility': volatility,
            'volatility_pct': f"{volatility:.2%}",
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': sortino_ratio,
            'calmar_ratio': calmar_ratio,
            'initial_capital': initial,
            'final_capital': final,
            'profit_amount': final - initial
        }

    def _calculate_risk_metrics(self, result: Dict) -> Dict:
        """计算风险指标"""
        daily_nav = result.get('daily_nav', [])
        if not daily_nav:
            # 返回基本字段，避免KeyError
            return {
                'max_drawdown': 0,
                'max_drawdown_pct': "0.00%",
                'max_dd_duration_days': 0,
                'avg_recovery_time_days': 0,
                'var_95_daily': 0,
                'var_95_pct': "0.00%",
                'cvar_95_daily': 0,
                'cvar_95_pct': "0.00%"
            }

        nav_df = pd.DataFrame(daily_nav)

        # 最大回撤
        nav_df['cummax'] = nav_df['total_value'].cummax()
        nav_df['drawdown'] = (nav_df['total_value'] - nav_df['cummax']) / nav_df['cummax']
        max_drawdown = nav_df['drawdown'].min()

        # 最大回撤持续时间
        max_dd_duration = 0
        current_dd_duration = 0
        for i in range(len(nav_df)):
            if nav_df['drawdown'].iloc[i] < 0:
                current_dd_duration += 1
                max_dd_duration = max(max_dd_duration, current_dd_duration)
            else:
                current_dd_duration = 0

        # 回撤恢复时间（平均）
        recovery_times = []
        in_drawdown = False
        dd_start = 0
        for i in range(len(nav_df)):
            if nav_df['drawdown'].iloc[i] < -0.05 and not in_drawdown:
                in_drawdown = True
                dd_start = i
            elif nav_df['drawdown'].iloc[i] >= 0 and in_drawdown:
                in_drawdown = False
                recovery_times.append(i - dd_start)

        avg_recovery_time = np.mean(recovery_times) if recovery_times else 0

        # VaR (Value at Risk) - 95%置信度
        daily_returns = nav_df['total_value'].pct_change().dropna()
        var_95 = np.percentile(daily_returns, 5) if len(daily_returns) > 0 else 0

        # CVaR (Conditional VaR)
        cvar_95 = daily_returns[daily_returns <= var_95].mean() if len(daily_returns) > 0 else 0

        return {
            'max_drawdown': max_drawdown,
            'max_drawdown_pct': f"{max_drawdown:.2%}",
            'max_dd_duration_days': max_dd_duration,
            'avg_recovery_time_days': avg_recovery_time,
            'var_95_daily': var_95,
            'var_95_pct': f"{var_95:.2%}",
            'cvar_95_daily': cvar_95,
            'cvar_95_pct': f"{cvar_95:.2%}"
        }

    def _calculate_trade_metrics(self, result: Dict) -> Dict:
        """计算交易指标"""
        trade_history = result.get('trade_history', [])
        if not trade_history:
            # 返回完整字段，避免KeyError
            return {
                'total_trades': 0,
                'win_trades': 0,
                'loss_trades': 0,
                'win_rate': 0,
                'win_rate_pct': "0.00%",
                'profit_loss_ratio': 0,
                'expectancy': 0,
                'avg_holding_days': 0,
                'stop_loss_rate': 0,
                'stop_loss_rate_pct': "0.00%",
                'take_profit_rate': 0,
                'take_profit_rate_pct': "0.00%",
                'max_single_profit': 0,
                'max_single_loss': 0,
                'max_consecutive_wins': 0,
                'max_consecutive_losses': 0
            }

        # 支持字典和对象两种格式
        def get_attr(obj, attr, default=0):
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        trades_df = pd.DataFrame([{
            'pnl': get_attr(t, 'pnl'),
            'pnl_pct': get_attr(t, 'pnl_pct'),
            'holding_days': get_attr(t, 'holding_days'),
            'stop_loss_triggered': get_attr(t, 'stop_loss_triggered'),
            'take_profit_triggered': get_attr(t, 'take_profit_triggered')
        } for t in trade_history])

        total_trades = len(trades_df)

        # 胜率
        win_trades = trades_df[trades_df['pnl'] > 0]
        loss_trades = trades_df[trades_df['pnl'] < 0]
        win_rate = len(win_trades) / total_trades if total_trades > 0 else 0

        # 盈亏比
        avg_profit = win_trades['pnl'].mean() if len(win_trades) > 0 else 0
        avg_loss = abs(loss_trades['pnl'].mean()) if len(loss_trades) > 0 else 1
        profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

        # 期望值
        expectancy = (win_rate * avg_profit) - ((1 - win_rate) * avg_loss)

        # 平均持仓天数
        avg_holding_days = trades_df['holding_days'].mean()

        # 止损/止盈触发率
        stop_loss_rate = trades_df['stop_loss_triggered'].sum() / total_trades if total_trades > 0 else 0
        take_profit_rate = trades_df['take_profit_triggered'].sum() / total_trades if total_trades > 0 else 0

        # 最大单笔盈亏
        max_profit = trades_df['pnl'].max()
        max_loss = trades_df['pnl'].min()

        # 连续盈亏
        trades_df['win'] = trades_df['pnl'] > 0
        consecutive_wins = self._max_consecutive(trades_df['win'].values)
        consecutive_losses = self._max_consecutive(~trades_df['win'].values)

        return {
            'total_trades': total_trades,
            'win_trades': len(win_trades),
            'loss_trades': len(loss_trades),
            'win_rate': win_rate,
            'win_rate_pct': f"{win_rate:.2%}",
            'profit_loss_ratio': profit_loss_ratio,
            'expectancy': expectancy,
            'avg_holding_days': avg_holding_days,
            'stop_loss_rate': stop_loss_rate,
            'stop_loss_rate_pct': f"{stop_loss_rate:.2%}",
            'take_profit_rate': take_profit_rate,
            'take_profit_rate_pct': f"{take_profit_rate:.2%}",
            'max_single_profit': max_profit,
            'max_single_loss': max_loss,
            'max_consecutive_wins': consecutive_wins,
            'max_consecutive_losses': consecutive_losses
        }

    def _calculate_pattern_metrics(self, result: Dict) -> Dict:
        """计算各模式表现"""
        trade_history = result.get('trade_history', [])
        if not trade_history:
            return {}

        # 支持字典和对象两种格式
        def get_attr(obj, attr, default=0):
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        trades_df = pd.DataFrame([{
            'pattern_type': get_attr(t, 'pattern_type'),
            'pnl': get_attr(t, 'pnl'),
            'pnl_pct': get_attr(t, 'pnl_pct')
        } for t in trade_history])

        pattern_stats = trades_df.groupby('pattern_type').agg({
            'pnl': ['count', 'sum', 'mean', 'std'],
            'pnl_pct': 'mean'
        }).round(4)

        # 胜率按模式
        pattern_win_rates = trades_df.groupby('pattern_type').apply(
            lambda x: (x['pnl'] > 0).sum() / len(x) if len(x) > 0 else 0
        ).to_dict()

        # 最佳/最差模式
        pattern_returns = trades_df.groupby('pattern_type')['pnl'].sum().sort_values(ascending=False)
        best_pattern = pattern_returns.index[0] if len(pattern_returns) > 0 else '无'
        worst_pattern = pattern_returns.index[-1] if len(pattern_returns) > 0 else '无'

        return {
            'pattern_stats': pattern_stats,
            'pattern_win_rates': pattern_win_rates,
            'best_pattern': best_pattern,
            'worst_pattern': worst_pattern,
            'pattern_count': len(pattern_stats)
        }

    def _calculate_resonance_metrics(self, result: Dict) -> Dict:
        """计算热点共振效果"""
        trade_history = result.get('trade_history', [])
        if not trade_history:
            return {}

        # 支持字典和对象两种格式
        def get_attr(obj, attr, default=0):
            if isinstance(obj, dict):
                return obj.get(attr, default)
            return getattr(obj, attr, default)

        trades_df = pd.DataFrame([{
            'hot_resonance': get_attr(t, 'hot_resonance'),
            'pnl': get_attr(t, 'pnl'),
            'pnl_pct': get_attr(t, 'pnl_pct')
        } for t in trade_history])

        # 分组统计
        resonance_stats = trades_df.groupby('hot_resonance').agg({
            'pnl': ['count', 'sum', 'mean'],
            'pnl_pct': 'mean'
        }).round(4)

        # 胜率对比
        with_resonance = trades_df[trades_df['hot_resonance'] == True]
        without_resonance = trades_df[trades_df['hot_resonance'] == False]

        win_rate_with = (with_resonance['pnl'] > 0).mean() if len(with_resonance) > 0 else 0
        win_rate_without = (without_resonance['pnl'] > 0).mean() if len(without_resonance) > 0 else 0

        avg_return_with = with_resonance['pnl'].mean() if len(with_resonance) > 0 else 0
        avg_return_without = without_resonance['pnl'].mean() if len(without_resonance) > 0 else 0

        return {
            'resonance_stats': resonance_stats,
            'win_rate_with_resonance': win_rate_with,
            'win_rate_without_resonance': win_rate_without,
            'avg_return_with_resonance': avg_return_with,
            'avg_return_without_resonance': avg_return_without,
            'resonance_advantage': win_rate_with - win_rate_without,
            'resonance_return_advantage': avg_return_with - avg_return_without
        }

    def _max_consecutive(self, arr: np.ndarray) -> int:
        """计算最大连续True数量"""
        max_count = 0
        current_count = 0
        for val in arr:
            if val:
                current_count += 1
                max_count = max(max_count, current_count)
            else:
                current_count = 0
        return max_count

    def generate_performance_report(self, backtest_result: Dict) -> str:
        """生成性能报告文本"""
        metrics = self.calculate_all_metrics(backtest_result)

        report = []
        report.append("=" * 70)
        report.append("策略回测性能报告")
        report.append("=" * 70)

        # 收益指标
        returns = metrics['returns']
        report.append("\n【收益指标】")
        report.append(f"  总收益率:     {returns['total_return_pct']}")
        report.append(f"  年化收益率:   {returns['annualized_return_pct']}")
        report.append(f"  收益金额:     ¥{returns['profit_amount']:,.2f}")
        report.append(f"  波动率:       {returns['volatility_pct']}")
        report.append(f"  Sharpe比率:   {returns['sharpe_ratio']:.2f}")
        report.append(f"  Sortino比率:  {returns['sortino_ratio']:.2f}")
        report.append(f"  Calmar比率:   {returns['calmar_ratio']:.2f}")

        # 风险指标
        risk = metrics['risk']
        report.append("\n【风险指标】")
        report.append(f"  最大回撤:           {risk.get('max_drawdown_pct', 'N/A')}")
        report.append(f"  最大回撤天数:       {risk.get('max_dd_duration_days', 0)}天")
        report.append(f"  平均恢复时间:       {risk.get('avg_recovery_time_days', 0):.1f}天")
        report.append(f"  VaR(95%):           {risk.get('var_95_pct', 'N/A')}")
        report.append(f"  CVaR(95%):          {risk.get('cvar_95_pct', 'N/A')}")

        # 交易指标
        trades = metrics['trades']
        report.append("\n【交易指标】")
        report.append(f"  总交易次数:         {trades['total_trades']}次")
        report.append(f"  盈利次数:           {trades['win_trades']}次")
        report.append(f"  亏损次数:           {trades['loss_trades']}次")
        report.append(f"  胜率:               {trades['win_rate_pct']}")
        report.append(f"  盈亏比:             {trades['profit_loss_ratio']:.2f}")
        report.append(f"  期望值:             ¥{trades['expectancy']:.2f}")
        report.append(f"  平均持仓天数:       {trades['avg_holding_days']:.1f}天")
        report.append(f"  止损触发率:         {trades['stop_loss_rate_pct']}")
        report.append(f"  止盈触发率:         {trades['take_profit_rate_pct']}")
        report.append(f"  最大单笔盈利:       ¥{trades['max_single_profit']:,.2f}")
        report.append(f"  最大单笔亏损:       ¥{trades['max_single_loss']:,.2f}")
        report.append(f"  最大连续盈利:       {trades['max_consecutive_wins']}次")
        report.append(f"  最大连续亏损:       {trades['max_consecutive_losses']}次")

        # 共振效果
        resonance = metrics.get('resonance', {})
        if resonance:
            report.append("\n【热点共振效果】")
            report.append(f"  有共振胜率:         {resonance.get('win_rate_with_resonance', 0):.2%}")
            report.append(f"  无共振胜率:         {resonance.get('win_rate_without_resonance', 0):.2%}")
            report.append(f"  胜率提升:           {resonance.get('resonance_advantage', 0):.2%}")
            report.append(f"  有共振平均收益:     ¥{resonance.get('avg_return_with_resonance', 0):.2f}")
            report.append(f"  无共振平均收益:     ¥{resonance.get('avg_return_without_resonance', 0):.2f}")

        report.append("\n" + "=" * 70)

        return "\n".join(report)