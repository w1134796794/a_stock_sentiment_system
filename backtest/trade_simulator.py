"""
交易模拟器
模拟交易计划的执行过程
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from datetime import datetime, timedelta
import loguru

logger = loguru.logger


class TradeSimulator:
    """
    交易模拟器
    模拟T+1交易环境下的计划执行
    """

    def __init__(self, data_manager, risk_config=None):
        from risk.risk_config import RiskConfig

        self.dm = data_manager
        self.risk = risk_config or RiskConfig.load()

    def simulate_trade_execution(self,
                                 plan: Dict,
                                 execution_date: str,
                                 market_condition: str = 'normal') -> Dict:
        """
        模拟单笔交易执行

        Args:
            plan: 交易计划
            execution_date: 执行日期
            market_condition: 市场环境 (normal/bull/bear)

        Returns:
            模拟执行结果
        """
        stock_code = plan['代码']
        stock_name = plan['名称']
        pattern_type = plan['模式']

        # 获取执行日价格数据
        price_data = self._get_execution_day_data(stock_code, execution_date)

        if price_data is None:
            return {
                'executed': False,
                'reason': '无法获取价格数据',
                'stock_code': stock_code
            }

        open_gap = self._check_open_gap(price_data)
        if not open_gap['passed']:
            return {
                'executed': False,
                'reason': open_gap['failed_reason'],
                'stock_code': stock_code
            }

        # 检查前置条件
        pre_conditions = plan.get('前置条件', '').split('; ')
        conditions_met = self._check_pre_conditions(pre_conditions, price_data)

        if not conditions_met['passed']:
            return {
                'executed': False,
                'reason': f'前置条件不满足: {conditions_met["failed_reason"]}',
                'stock_code': stock_code
            }

        # 检查取消条件
        cancel_conditions = plan.get('取消条件', '').split('; ')
        should_cancel = self._check_cancel_conditions(cancel_conditions, price_data)

        if should_cancel['should_cancel']:
            return {
                'executed': False,
                'reason': f'触发取消条件: {should_cancel["reason"]}',
                'stock_code': stock_code
            }

        # 计算买入价格（加入滑点）
        entry_price = self._calculate_entry_price(plan, price_data)

        # 模拟次日/后续卖出
        exit_result = self._simulate_exit(stock_code, execution_date, entry_price, plan)

        return {
            'executed': True,
            'stock_code': stock_code,
            'stock_name': stock_name,
            'pattern_type': pattern_type,
            'entry_date': execution_date,
            'entry_price': entry_price,
            'exit_date': exit_result['exit_date'],
            'exit_price': exit_result['exit_price'],
            'holding_days': exit_result['holding_days'],
            'pnl': exit_result['pnl'],
            'pnl_pct': exit_result['pnl_pct'],
            'exit_reason': exit_result['reason'],
            'hot_resonance': plan.get('热点共振', False),
            'resonance_sectors': plan.get('共振板块', '')
        }

    def _get_execution_day_data(self, stock_code: str, date: str) -> Optional[Dict]:
        """获取执行日价格数据"""
        try:
            # 从DataManager获取日线数据
            data = self.dm.get_stock_daily_data(stock_code, date)
            if not data:
                return None
            return {
                'open': data['open'],
                'high': data['high'],
                'low': data['low'],
                'close': data['close'],
                'volume': data.get('vol', data.get('volume', 0)),
                'amount': data.get('amount', 0),
                'pre_close': data.get('pre_close', data['open'])
            }
        except Exception as e:
            logger.error(f"获取价格数据失败 {stock_code} {date}: {e}")
            return None

    def _check_open_gap(self, price_data: Dict) -> Dict:
        """早盘竞价硬规则：只接受 0%-3% 的高开。"""
        open_price = float(price_data.get('open') or 0)
        pre_close = float(price_data.get('pre_close') or 0)
        if open_price <= 0 or pre_close <= 0:
            return {
                'passed': False,
                'failed_reason': '无法确认开盘价/昨收价，放弃买入'
            }
        gap_ratio = (open_price - pre_close) / pre_close
        if gap_ratio <= 0:
            label = '低开' if gap_ratio < 0 else '平开'
            return {
                'passed': False,
                'failed_reason': f'{label}{gap_ratio:.2%}，未高开，放弃竞价买点'
            }
        if gap_ratio > self.risk.max_open_gap:
            return {
                'passed': False,
                'failed_reason': f'高开{gap_ratio:.2%}超过{self.risk.max_open_gap:.2%}，不追高'
            }
        return {'passed': True, 'failed_reason': None}

    def _check_pre_conditions(self, conditions: List[str], price_data: Dict) -> Dict:
        """检查前置条件"""
        # 简化实现，实际应解析条件并检查
        # 这里假设大部分条件都能满足（回测乐观估计）

        if float(price_data.get('pre_close') or 0) <= 0:
            return {
                'passed': False,
                'failed_reason': '昨收价缺失'
            }
        gap_ratio = (price_data['open'] - price_data['pre_close']) / price_data['pre_close']

        for condition in conditions:
            condition = condition.strip()
            if not condition:
                continue

            # 高开范围由统一入口闸门检查，这里只保留“必须高开”的语义。
            if '高开' in condition:
                if gap_ratio <= self.risk.min_open_gap:
                    return {
                        'passed': False,
                        'failed_reason': f'未高开: {gap_ratio:.2%}'
                    }

        return {'passed': True, 'failed_reason': None}

    def _check_cancel_conditions(self, conditions: List[str], price_data: Dict) -> Dict:
        """检查取消条件"""
        if float(price_data.get('pre_close') or 0) <= 0:
            return {'should_cancel': True, 'reason': '昨收价缺失'}
        gap_ratio = (price_data['open'] - price_data['pre_close']) / price_data['pre_close']

        for condition in conditions:
            condition = condition.strip()
            if not condition:
                continue

            # 检查低开
            if '低开' in condition and gap_ratio < 0:
                return {
                    'should_cancel': True,
                    'reason': f'低开: {gap_ratio:.2%}'
                }

            # 检查大幅低开
            if gap_ratio < -0.03:
                return {
                    'should_cancel': True,
                    'reason': f'大幅低开: {gap_ratio:.2%}'
                }

        return {'should_cancel': False, 'reason': None}

    def _calculate_entry_price(self, plan: Dict, price_data: Dict) -> float:
        """计算实际买入价格"""
        # 根据介入时机确定买入价格
        timing = plan.get('介入时机', '')

        if '竞价' in timing:
            # 竞价买入：开盘价
            entry_price = price_data['open']
        elif '开盘' in timing:
            # 开盘买入
            entry_price = price_data['open'] * 1.005  # 加入滑点
        else:
            # 默认买入
            entry_price = price_data['open'] * 1.01

        return entry_price

    def _simulate_exit(self,
                      stock_code: str,
                      entry_date: str,
                      entry_price: float,
                      plan: Dict,
                      max_holding_days: int = 5) -> Dict:
        """
        模拟卖出

        Args:
            stock_code: 股票代码
            entry_date: 买入日期
            entry_price: 买入价格
            plan: 交易计划
            max_holding_days: 最大持仓天数

        Returns:
            卖出结果
        """
        stop_loss_price = plan.get('止损价', entry_price * (1 - self.risk.hard_stop_loss))
        highest_price = entry_price

        # 获取后续N天数据
        exit_date = entry_date
        exit_price = entry_price
        holding_days = 0
        exit_reason = 'holding'

        # 观察窗口结束不强制卖出；只执行硬止损、时间止损和高点回撤止盈。
        for day in range(1, max_holding_days + 1):
            next_date = self._get_next_trade_date(entry_date, day)
            if not next_date:
                break

            price_data = self._get_execution_day_data(stock_code, next_date)
            if not price_data:
                continue

            holding_days = day
            low_price = price_data['low']
            high_price = price_data['high']
            close_price = price_data['close']
            highest_price = max(highest_price, high_price)
            exit_date = next_date
            exit_price = close_price

            # 检查止损
            if low_price <= stop_loss_price:
                exit_date = next_date
                exit_price = stop_loss_price
                exit_reason = 'stop_loss'
                break

            peak_profit = (highest_price - entry_price) / entry_price
            pullback = (highest_price - close_price) / highest_price
            if (self.risk.trailing_stop > 0
                    and peak_profit >= self.risk.trailing_activation
                    and pullback >= self.risk.trailing_stop):
                exit_date = next_date
                exit_price = close_price
                exit_reason = 'trailing_stop'
                break

            current_pnl = (close_price - entry_price) / entry_price
            if (day >= self.risk.time_stop_days
                    and current_pnl < self.risk.time_stop_profit_threshold):
                exit_date = next_date
                exit_price = close_price
                exit_reason = 'time_stop'
                break

        # 计算盈亏
        pnl = (exit_price - entry_price)
        pnl_pct = pnl / entry_price

        return {
            'exit_date': exit_date,
            'exit_price': exit_price,
            'holding_days': holding_days,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'reason': exit_reason
        }

    def _get_next_trade_date(self, date: str, days: int) -> Optional[str]:
        """获取下一个交易日"""
        try:
            current = datetime.strptime(date, "%Y%m%d")
            next_date = current + timedelta(days=days)

            # 跳过周末（简化处理）
            while next_date.weekday() >= 5:
                next_date += timedelta(days=1)

            return next_date.strftime("%Y%m%d")
        except:
            return None

    def batch_simulate(self,
                      trade_plans: pd.DataFrame,
                      start_date: str,
                      end_date: str) -> pd.DataFrame:
        """
        批量模拟交易计划

        Args:
            trade_plans: 交易计划DataFrame
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            模拟结果DataFrame
        """
        results = []

        for _, plan in trade_plans.iterrows():
            execution_date = plan.get('日期', start_date)

            # 检查日期范围
            if execution_date < start_date or execution_date > end_date:
                continue

            result = self.simulate_trade_execution(plan, execution_date)
            results.append(result)

        return pd.DataFrame(results)
