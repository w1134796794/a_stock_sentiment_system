"""
回测引擎 - 基于交易计划的历史回测
核心功能：
1. 加载历史交易计划
2. 模拟T+1交易执行
3. 计算收益和回撤
4. 生成回测报告
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from pathlib import Path
import loguru

from backtest.matching_rules import open_gap_pct
from backtest.trade_calendar import TradeCalendar

logger = loguru.logger


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 100000.0  # 初始资金
    max_position_per_stock: float = 0.2  # 单票最大仓位20%
    max_total_position: float = 0.8  # 总仓位上限80%
    max_positions: int = 8  # 最多同时持仓只数（仅风控开启时生效）
    max_sector_concentration: float = 0.4  # 单一板块最大仓位（仅风控开启时生效）

    # 风控闸门总开关：关闭后不施加组合层约束（单票/总仓/持仓数/板块集中度），
    # 仅保留现金/价格有效性等基础校验，用于对比"无风控"模拟结果。个股止损止盈
    # 属于交易计划的退出策略，始终生效，不受此开关影响。
    risk_control: bool = True

    # 基础止损止盈
    stop_loss_pct: float = 0.05  # 硬止损线5%
    take_profit_pct: float = 0.10  # 基础止盈线10%

    # 移动止盈（跟踪止损）
    trailing_stop: bool = True  # 启用跟踪止损
    trailing_stop_pct: float = 0.08  # 从最高点回撤8%触发
    trailing_activation_pct: float = 0.05  # 盈利5%后启动跟踪止损

    # 时间止损
    time_stop_days: int = 5  # 持仓超过5天强制卖出
    time_stop_profit_threshold: float = 0.02  # 盈利低于2%时触发时间止损

    # 分批止盈
    partial_take_profit: bool = True  # 启用分批止盈
    partial_profit_first_pct: float = 0.08  # 第一次止盈8%
    partial_profit_first_ratio: float = 0.5  # 卖出50%仓位
    partial_profit_second_pct: float = 0.15  # 第二次止盈15%
    partial_profit_second_ratio: float = 0.5  # 再卖出剩余50%（即总仓位的25%）

    # 费用设置
    commission_rate: float = 0.0003  # 佣金率0.03%
    stamp_duty_rate: float = 0.001  # 印花税0.1%（卖出）
    slippage: float = 0.002  # 滑点0.2%
    min_holding_days: int = 1  # 最小持仓天数（T+1）

    # 数据缺失时是否用随机价格兜底（B-1：默认关闭，缺数据则跳过该票，避免回测失真）
    use_simulated_prices: bool = False


@dataclass
class TradeRecord:
    """交易记录"""
    date: str
    stock_code: str
    stock_name: str
    pattern_type: str
    action: str  # BUY/SELL
    entry_price: float
    exit_price: Optional[float]
    shares: int
    position_size: float
    pnl: float
    pnl_pct: float
    holding_days: int
    hot_resonance: bool
    resonance_sectors: str
    stop_loss_triggered: bool = False
    take_profit_triggered: bool = False


class BacktestEngine:
    """
    回测引擎
    基于每日交易计划进行历史回测
    """

    def __init__(self, data_manager, config: BacktestConfig = None):
        self.dm = data_manager
        self.config = config or BacktestConfig()
        self.calendar = TradeCalendar()
        self.trade_history: List[TradeRecord] = []
        self.daily_nav: List[Dict] = []  # 每日净值
        self.current_positions: Dict[str, Dict] = {}  # 当前持仓
        self.cash: float = self.config.initial_capital
        self.total_capital: float = self.config.initial_capital

    def run_backtest(self,
                     start_date: str,
                     end_date: str,
                     trade_plans_dir: str) -> Dict:
        """
        执行回测

        Args:
            start_date: 回测开始日期 YYYYMMDD
            end_date: 回测结束日期 YYYYMMDD
            trade_plans_dir: 交易计划文件目录

        Returns:
            回测结果报告
        """
        logger.info(f"开始回测: {start_date} 至 {end_date}")
        logger.info(f"初始资金: {self.config.initial_capital:,.2f}")

        # 生成交易日历
        trade_dates = self._get_trade_dates(start_date, end_date)

        for date in trade_dates:
            self._process_date(date, trade_plans_dir)

        # 生成回测报告
        report = self._generate_backtest_report()

        logger.info(f"回测完成: 最终资金 {self.total_capital:,.2f}")
        logger.info(f"总收益率: {report['total_return']:.2%}")

        return report

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[str]:
        """获取交易日列表（B-1：使用真实交易日历，自动剔除节假日）"""
        return self.calendar.get_trade_dates(start_date, end_date)

    def _process_date(self, date: str, trade_plans_dir: str):
        """处理单日回测
        
        注意：交易计划是前一天收盘后制定的，所以当天执行的是前一天的计划
        例如：20260413 收盘后制定 20260414 的计划，20260414 开盘时执行
        """
        # 1. 检查并执行止损止盈（对已有持仓）
        self._check_stop_loss_take_profit(date)

        # 2. 加载前一日的交易计划（T日执行T-1日的计划）
        prev_date = self._get_prev_trade_date(date)
        plans_df = self._load_trade_plans(prev_date, trade_plans_dir)

        if not plans_df.empty:
            logger.info(f"[{date}] 加载 {prev_date} 制定的 {len(plans_df)} 条交易计划")

            # 3. 根据计划执行买入（检查开盘情况和介入时机）
            for _, plan in plans_df.iterrows():
                self._execute_buy(plan, date)

        # 4. 计算当日净值
        self._calculate_daily_nav(date)

    def _get_prev_trade_date(self, date: str) -> str:
        """获取前一个交易日（B-1：真实交易日历）"""
        return self.calendar.prev(date)

    def _load_trade_plans(self, date: str, trade_plans_dir: str) -> pd.DataFrame:
        """加载交易计划"""
        # 尝试两种文件名格式
        plan_file = Path(trade_plans_dir) / f"交易计划_{date}.csv"

        if not plan_file.exists():
            # 尝试旧格式
            plan_file = Path(trade_plans_dir) / f"trade_plans_{date}.csv"
            if not plan_file.exists():
                return pd.DataFrame()

        try:
            df = pd.read_csv(plan_file)
            # 只保留买入计划
            if '动作' in df.columns:
                df = df[df['动作'] == '买入']
            return df
        except Exception as e:
            logger.error(f"加载交易计划失败 {date}: {e}")
            return pd.DataFrame()

    def _execute_buy(self, plan: pd.Series, date: str):
        """执行买入
        
        根据交易计划中的介入时机和当日开盘情况决定是否买入
        """
        stock_code = str(plan['代码']).zfill(6)  # 标准化为6位代码
        stock_name = plan['名称']

        # 检查是否已有持仓
        if stock_code in self.current_positions:
            logger.debug(f"{stock_name} 已有持仓，跳过")
            return

        position_size = self._calculate_position_size(plan)
        current_position_value = sum(pos['market_value'] for pos in self.current_positions.values())

        # 组合层风控闸门（仅风控开启时施加：持仓数 / 单票 / 总仓 / 板块集中度）
        if self.config.risk_control:
            # a) 持仓数上限
            if len(self.current_positions) >= self.config.max_positions:
                logger.warning(f"持仓数已达上限{self.config.max_positions}只，跳过买入 {stock_name}")
                return

            # b) 单票上限
            max_position_value = self.total_capital * self.config.max_position_per_stock
            if position_size > max_position_value:
                position_size = max_position_value

            # c) 总仓位上限
            max_total = self.total_capital * self.config.max_total_position
            if current_position_value + position_size > max_total:
                logger.warning(f"总仓位超限，跳过买入 {stock_name}")
                return

            # d) 板块集中度
            sector = str(plan.get('共振板块', '') or '').split(',')[0].strip()
            if sector:
                sector_value = sum(
                    pos['market_value'] for pos in self.current_positions.values()
                    if str(pos.get('resonance_sectors', '') or '').split(',')[0].strip() == sector
                )
                max_sector = self.total_capital * self.config.max_sector_concentration
                if sector_value + position_size > max_sector:
                    allowed = max(max_sector - sector_value, 0.0)
                    if allowed < self.total_capital * 0.005:
                        logger.warning(f"板块[{sector}]集中度超限，跳过买入 {stock_name}")
                        return
                    position_size = allowed

        # 检查现金
        if position_size > self.cash:
            logger.warning(f"现金不足，跳过买入 {stock_name}")
            return

        # 检查开盘情况是否满足买入条件
        can_buy, entry_price = self._check_buy_conditions(plan, date, stock_code, stock_name)
        if not can_buy:
            return
        
        if entry_price <= 0:
            logger.warning(f"{stock_name} 买入价格无效，跳过")
            return

        # 确保买入价格不超过涨停价（A股涨停为10%，ST为5%）
        # 从DataManager获取昨日收盘价计算涨停价
        try:
            prev_close = self._get_prev_close(stock_code, date)
            if prev_close and prev_close > 0:
                limit_up_price = prev_close * 1.1  # 10%涨停限制
                if entry_price > limit_up_price:
                    logger.warning(f"{stock_name} 买入价{entry_price:.2f}超过涨停价{limit_up_price:.2f}，调整为涨停价")
                    entry_price = limit_up_price
        except Exception as e:
            logger.debug(f"获取昨日收盘价失败 {stock_code}: {e}")

        if entry_price <= 0:
            logger.warning(f"{stock_name} 买入价格无效，跳过")
            return
        shares = int(position_size / entry_price / 100) * 100  # 整手

        if shares < 100:
            logger.warning(f"{stock_name} 计算股数不足1手，跳过")
            return

        actual_cost = shares * entry_price
        commission = actual_cost * self.config.commission_rate

        # 执行买入
        self.cash -= (actual_cost + commission)

        self.current_positions[stock_code] = {
            'stock_name': stock_name,
            'entry_date': date,
            'entry_price': entry_price,
            'shares': shares,
            'cost_basis': actual_cost + commission,
            'market_value': actual_cost,
            'pattern_type': plan['模式'],
            'hot_resonance': plan.get('热点共振', False),
            'resonance_sectors': plan.get('共振板块', ''),
            'stop_loss_price': entry_price * (1 - self.config.stop_loss_pct),
            'take_profit_price': entry_price * (1 + self.config.take_profit_pct),
            'highest_price': entry_price  # 用于跟踪回撤
        }

        logger.info(f"[{date}] 买入 {stock_name}({stock_code}): {shares}股 @ {entry_price:.2f}, 成本:{actual_cost+commission:.2f}")

        # 记录买入交易
        trade_record = TradeRecord(
            date=date,
            stock_code=stock_code,
            stock_name=stock_name,
            pattern_type=plan['模式'],
            action='BUY',
            entry_price=entry_price,
            exit_price=0,
            shares=shares,
            position_size=actual_cost + commission,
            pnl=0,
            pnl_pct=0,
            holding_days=0,
            hot_resonance=plan.get('热点共振', False),
            resonance_sectors=plan.get('共振板块', ''),
            stop_loss_triggered=False,
            take_profit_triggered=False
        )
        self.trade_history.append(trade_record)

    def _check_buy_conditions(self, plan: pd.Series, date: str, stock_code: str, stock_name: str) -> Tuple[bool, float]:
        """
        检查买入条件
        
        Returns:
            (是否可以买入, 买入价格)
        """
        target_price = plan['目标价']
        entry_timing = plan.get('介入时机', '09:31-10:00')
        
        # 获取当日开盘数据
        try:
            standardized_code = self._standardize_stock_code(stock_code)
            daily_data = self.dm.get_stock_daily_data(standardized_code, date)
            
            if not daily_data:
                logger.info(f"{stock_name} 无法获取当日开盘数据，不能确认高开，放弃买入")
                return False, 0
            
            open_price = daily_data.get('open', 0)
            high_price = daily_data.get('high', 0)
            low_price = daily_data.get('low', 0)
            
            if open_price <= 0:
                logger.info(f"{stock_name} 开盘价无效，不能确认高开，放弃买入")
                return False, 0
                
        except Exception as e:
            logger.debug(f"{stock_name} 获取开盘数据失败: {e}，不能确认高开，放弃买入")
            return False, 0
        
        # 早盘竞价硬规则：必须高开才允许执行；低开/平开/缺昨收直接放弃。
        prev_close = float(daily_data.get('pre_close') or 0)
        if prev_close <= 0:
            prev_close = self._get_prev_close(stock_code, date) or 0
        gap = open_gap_pct({"open": open_price, "pre_close": prev_close}, prev_close)
        if gap is None:
            logger.info(f"{stock_name} 昨收价缺失，不能确认高开，放弃买入")
            return False, 0
        if gap <= 0:
            label = "低开" if gap < 0 else "平开"
            logger.info(f"{stock_name} {label}{gap:.2%}，未高开，放弃竞价买点")
            return False, 0

        # 检查是否涨停开盘（无法买入）
        if prev_close and prev_close > 0:
            limit_up_price = prev_close * 1.1
            if open_price >= limit_up_price * 0.998:  # 考虑浮点误差
                logger.info(f"{stock_name} 涨停开盘，无法买入")
                return False, 0
        
        # 根据介入时机判断买入价格
        # 竞价时段 (09:24:30-09:25:00)
        if '09:24' in entry_timing or '竞价' in entry_timing:
            # 集合竞价买入，使用开盘价
            entry_price = open_price * (1 + self.config.slippage)
            logger.debug(f"{stock_name} 集合竞价买入，开盘价: {open_price:.2f}")
            return True, entry_price
        
        # 开盘后时段 (09:31-10:00 等)
        # 检查目标价是否在当日价格范围内
        if target_price > 0:
            if low_price <= target_price <= high_price:
                # 目标价在范围内，可以成交
                entry_price = target_price * (1 + self.config.slippage)
                logger.debug(f"{stock_name} 目标价{target_price:.2f}在当日价格范围[{low_price:.2f}, {high_price:.2f}]内")
                return True, entry_price
            elif target_price < low_price:
                # 目标价低于最低价，以最低价成交
                entry_price = low_price * (1 + self.config.slippage)
                logger.debug(f"{stock_name} 目标价{target_price:.2f}低于最低价{low_price:.2f}，以最低价买入")
                return True, entry_price
            else:
                # 目标价高于最高价，无法成交
                logger.info(f"{stock_name} 目标价{target_price:.2f}高于最高价{high_price:.2f}，无法买入")
                return False, 0
        else:
            # 目标价为0，使用开盘价
            entry_price = open_price * (1 + self.config.slippage)
            return True, entry_price

    def _calculate_position_size(self, plan: pd.Series) -> float:
        """计算仓位大小"""
        position_map = {
            'light': 0.1,
            'medium': 0.15,
            'heavy': 0.2
        }

        position_str = plan.get('仓位', 'medium')
        position_pct = position_map.get(position_str, 0.1)

        # 热点共振增加仓位
        if plan.get('热点共振', False):
            position_pct *= 1.2

        return self.total_capital * position_pct

    def _check_stop_loss_take_profit(self, date: str):
        """检查止损止盈 - 综合卖出策略"""
        stocks_to_sell = []
        stocks_to_partial_sell = []

        for stock_code, position in self.current_positions.items():
            # 获取当日价格数据
            current_price = self._get_stock_price(stock_code, date)

            if current_price is None:
                continue

            # 更新最高价（用于回撤计算）
            if current_price > position['highest_price']:
                position['highest_price'] = current_price

            # 计算当前盈亏比例
            current_pnl_pct = (current_price - position['entry_price']) / position['entry_price']

            # 更新市值
            position['market_value'] = position['shares'] * current_price

            # ========== 1. 硬止损（必须执行）==========
            if current_price <= position['stop_loss_price']:
                stocks_to_sell.append((stock_code, current_price, 'stop_loss'))
                logger.info(f"[{date}] {position['stock_name']} 触发硬止损: {current_price:.2f} (亏损{current_pnl_pct:.2%})")
                continue

            # ========== 2. 跟踪止损（移动止盈）==========
            if self.config.trailing_stop and position['highest_price'] > position['entry_price']:
                # 计算从最高点的回撤
                drawdown_from_high = (position['highest_price'] - current_price) / position['highest_price']

                # 只有当盈利超过激活阈值后才启动跟踪止损
                profit_pct = (position['highest_price'] - position['entry_price']) / position['entry_price']

                if profit_pct >= self.config.trailing_activation_pct and drawdown_from_high >= self.config.trailing_stop_pct:
                    stocks_to_sell.append((stock_code, current_price, 'trailing_stop'))
                    logger.info(f"[{date}] {position['stock_name']} 触发跟踪止损: {current_price:.2f} "
                               f"(最高点{position['highest_price']:.2f}, 回撤{drawdown_from_high:.2%})")
                    continue

            # ========== 3. 时间止损 ==========
            holding_days = self._calculate_holding_days(position['entry_date'], date)
            if holding_days >= self.config.time_stop_days:
                # 持仓时间过长且盈利未达到预期，强制卖出
                if current_pnl_pct < self.config.time_stop_profit_threshold:
                    stocks_to_sell.append((stock_code, current_price, 'time_stop'))
                    logger.info(f"[{date}] {position['stock_name']} 触发时间止损: {current_price:.2f} "
                               f"(持仓{holding_days}天, 盈利{current_pnl_pct:.2%})")
                    continue

            # ========== 4. 分批止盈 ==========
            if self.config.partial_take_profit:
                # 第一次止盈
                if (current_pnl_pct >= self.config.partial_profit_first_pct and
                    not position.get('first_partial_sold', False)):
                    shares_to_sell = int(position['shares'] * self.config.partial_profit_first_ratio / 100) * 100
                    if shares_to_sell >= 100:
                        stocks_to_partial_sell.append((stock_code, current_price, 'partial_first', shares_to_sell))
                        position['first_partial_sold'] = True
                        logger.info(f"[{date}] {position['stock_name']} 触发第一次分批止盈: {current_price:.2f} "
                                   f"(盈利{current_pnl_pct:.2%}, 卖出{shares_to_sell}股)")
                        continue

                # 第二次止盈
                if (current_pnl_pct >= self.config.partial_profit_second_pct and
                    position.get('first_partial_sold', False) and
                    not position.get('second_partial_sold', False)):
                    remaining_shares = position['shares']
                    shares_to_sell = int(remaining_shares * self.config.partial_profit_second_ratio / 100) * 100
                    if shares_to_sell >= 100:
                        stocks_to_partial_sell.append((stock_code, current_price, 'partial_second', shares_to_sell))
                        position['second_partial_sold'] = True
                        logger.info(f"[{date}] {position['stock_name']} 触发第二次分批止盈: {current_price:.2f} "
                                   f"(盈利{current_pnl_pct:.2%}, 卖出{shares_to_sell}股)")
                        continue

            # ========== 5. 基础止盈 ==========
            if current_price >= position['take_profit_price']:
                stocks_to_sell.append((stock_code, current_price, 'take_profit'))
                logger.info(f"[{date}] {position['stock_name']} 触发基础止盈: {current_price:.2f} (盈利{current_pnl_pct:.2%})")
                continue

        # 执行分批卖出
        for stock_code, sell_price, reason, shares in stocks_to_partial_sell:
            self._execute_partial_sell(stock_code, sell_price, date, reason, shares)

        # 执行全部卖出
        for stock_code, sell_price, reason in stocks_to_sell:
            self._execute_sell(stock_code, sell_price, date, reason)

    def _execute_sell(self, stock_code: str, sell_price: float, date: str, reason: str):
        """执行卖出"""
        if stock_code not in self.current_positions:
            return

        position = self.current_positions[stock_code]
        shares = position['shares']

        # 计算卖出金额（减去滑点）
        actual_sell_price = sell_price * (1 - self.config.slippage)
        sell_value = shares * actual_sell_price

        # 计算费用
        commission = sell_value * self.config.commission_rate
        stamp_duty = sell_value * self.config.stamp_duty_rate

        # 计算盈亏
        total_cost = position['cost_basis']
        total_revenue = sell_value - commission - stamp_duty
        pnl = total_revenue - total_cost
        pnl_pct = pnl / total_cost if total_cost > 0 else 0

        # 更新现金
        self.cash += total_revenue

        # 记录交易
        holding_days = self._calculate_holding_days(position['entry_date'], date)

        trade_record = TradeRecord(
            date=date,
            stock_code=stock_code,
            stock_name=position['stock_name'],
            pattern_type=position['pattern_type'],
            action='SELL',
            entry_price=position['entry_price'],
            exit_price=actual_sell_price,
            shares=shares,
            position_size=total_cost,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            hot_resonance=position['hot_resonance'],
            resonance_sectors=position['resonance_sectors'],
            stop_loss_triggered=(reason == 'stop_loss'),
            take_profit_triggered=(reason == 'take_profit')
        )

        self.trade_history.append(trade_record)

        logger.info(f"[{date}] 卖出 {position['stock_name']}({stock_code}): {shares}股 @ {actual_sell_price:.2f}, 盈亏:{pnl:.2f}({pnl_pct:.2%})")

        # 移除持仓
        del self.current_positions[stock_code]

    def _execute_partial_sell(self, stock_code: str, sell_price: float, date: str, reason: str, shares: int):
        """执行分批卖出"""
        if stock_code not in self.current_positions:
            return

        position = self.current_positions[stock_code]

        # 确保卖出股数不超过持仓
        shares = min(shares, position['shares'])
        if shares < 100:
            return

        # 计算卖出金额（减去滑点）
        actual_sell_price = sell_price * (1 - self.config.slippage)
        sell_value = shares * actual_sell_price

        # 计算费用
        commission = sell_value * self.config.commission_rate
        stamp_duty = sell_value * self.config.stamp_duty_rate

        # 计算盈亏（按比例分摊成本）
        cost_ratio = shares / position['shares']
        total_cost = position['cost_basis'] * cost_ratio
        total_revenue = sell_value - commission - stamp_duty
        pnl = total_revenue - total_cost
        pnl_pct = pnl / total_cost if total_cost > 0 else 0

        # 更新现金
        self.cash += total_revenue

        # 更新持仓
        position['shares'] -= shares
        position['cost_basis'] -= total_cost
        position['market_value'] = position['shares'] * sell_price

        # 记录交易
        holding_days = self._calculate_holding_days(position['entry_date'], date)

        trade_record = TradeRecord(
            date=date,
            stock_code=stock_code,
            stock_name=position['stock_name'],
            pattern_type=position['pattern_type'],
            action='SELL_PARTIAL',
            entry_price=position['entry_price'],
            exit_price=actual_sell_price,
            shares=shares,
            position_size=total_cost,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_days=holding_days,
            hot_resonance=position['hot_resonance'],
            resonance_sectors=position['resonance_sectors'],
            stop_loss_triggered=False,
            take_profit_triggered=(reason.startswith('partial'))
        )

        self.trade_history.append(trade_record)

        logger.info(f"[{date}] 分批卖出 {position['stock_name']}({stock_code}): {shares}股 @ {actual_sell_price:.2f}, "
                   f"盈亏:{pnl:.2f}({pnl_pct:.2%}), 剩余:{position['shares']}股")

        # 如果全部卖完，移除持仓
        if position['shares'] <= 0:
            del self.current_positions[stock_code]

    def _get_stock_price(self, stock_code: str, date: str) -> Optional[float]:
        """获取股票当日价格"""
        # 标准化股票代码为6位数字
        normalized_code = str(stock_code).zfill(6)
        # 标准化股票代码（添加交易所后缀）
        standardized_code = self._standardize_stock_code(normalized_code)

        # 首先尝试从DataManager获取真实数据
        try:
            result = self.dm.get_stock_daily_data(standardized_code, date)
            if result and 'close' in result:
                price = result['close']
                if price > 0:
                    return float(price)
        except Exception as e:
            logger.debug(f"获取真实价格失败 {stock_code}({standardized_code}) {date}: {e}")

        # B-1：默认不再用随机价格兜底（回测失真之源）。仅当显式开启
        # use_simulated_prices 时才回退模拟价格，否则返回 None 由调用方跳过。
        if not self.config.use_simulated_prices:
            return None

        # 如果没有真实数据，使用模拟价格（基于前一日收盘价或持仓成本）
        if normalized_code in self.current_positions:
            position = self.current_positions[normalized_code]

            # 使用当前市值计算昨日收盘价
            prev_close = position['market_value'] / position['shares'] if position['shares'] > 0 else position['entry_price']

            # 使用日期作为种子，确保同一天价格一致
            date_seed = int(date)
            np.random.seed(date_seed + hash(stock_code) % 10000)

            # 模拟价格波动 (-8% 到 +12%)，增加波动性以测试各种卖出条件
            daily_change = np.random.uniform(-0.08, 0.12)
            simulated_price = prev_close * (1 + daily_change)

            # 确保价格不会低于0.1元
            simulated_price = max(simulated_price, 0.1)

            logger.debug(f"[{date}] {normalized_code} 使用模拟价格: {simulated_price:.2f} (基于昨收{prev_close:.2f}, 变动{daily_change:.2%})")
            return simulated_price

        return None

    def _standardize_stock_code(self, stock_code) -> str:
        """
        标准化股票代码，添加交易所后缀
        例如: 000001 -> 000001.SZ, 600000 -> 600000.SH
        """
        # 先转换为字符串
        code_str = str(stock_code)

        # 如果已经有后缀，直接返回
        if '.' in code_str:
            return code_str

        # 根据代码规则判断交易所
        code = code_str.zfill(6)  # 补齐6位

        if code.startswith(('60', '68', '88', '89')):
            # 上海主板、科创板、B股
            return f"{code}.SH"
        elif code.startswith(('00', '30', '20', '44', '83', '87', '43')):
            # 深圳主板、创业板、中小板、北交所等
            return f"{code}.SZ"
        elif code.startswith(('8', '4')) and len(code) >= 6:
            # 新三板/北交所
            return f"{code}.BJ"
        else:
            # 默认上海
            return f"{code}.SH"

    def _get_prev_close(self, stock_code: str, date: str) -> Optional[float]:
        """获取昨日收盘价（B-1：真实交易日历取前一交易日）"""
        try:
            prev_date_str = self.calendar.prev(date)
            standardized_code = self._standardize_stock_code(stock_code)

            result = self.dm.get_stock_daily_data(standardized_code, prev_date_str)
            if result and 'close' in result:
                return float(result['close'])
        except Exception as e:
            logger.debug(f"获取昨日收盘价失败 {stock_code} {date}: {e}")
        return None

    def _calculate_holding_days(self, entry_date: str, exit_date: str) -> int:
        """计算持仓天数（B-1：按交易日计，时间止损更准确）"""
        return self.calendar.holding_days(entry_date, exit_date)

    def _calculate_daily_nav(self, date: str):
        """计算每日净值"""
        position_value = sum(pos['market_value'] for pos in self.current_positions.values())
        total_value = self.cash + position_value

        self.daily_nav.append({
            'date': date,
            'cash': self.cash,
            'position_value': position_value,
            'total_value': total_value,
            'position_count': len(self.current_positions)
        })

        self.total_capital = total_value

    def _generate_backtest_report(self) -> Dict:
        """生成回测报告"""
        if not self.trade_history:
            return {
                'total_return': 0,
                'annualized_return': 0,
                'sharpe_ratio': 0,
                'max_drawdown': 0,
                'win_rate': 0,
                'profit_loss_ratio': 0,
                'total_trades': 0
            }

        # 计算收益率
        total_return = (self.total_capital - self.config.initial_capital) / self.config.initial_capital

        # 计算年化收益
        days = len(self.daily_nav)
        annualized_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0

        # 计算最大回撤
        nav_series = pd.DataFrame(self.daily_nav)
        nav_series['cummax'] = nav_series['total_value'].cummax()
        nav_series['drawdown'] = (nav_series['total_value'] - nav_series['cummax']) / nav_series['cummax']
        max_drawdown = nav_series['drawdown'].min()

        # 计算胜率
        trades_df = pd.DataFrame([{
            'pnl': t.pnl,
            'pnl_pct': t.pnl_pct,
            'pattern_type': t.pattern_type,
            'hot_resonance': t.hot_resonance
        } for t in self.trade_history])

        win_rate = (trades_df['pnl'] > 0).mean()

        # 计算盈亏比
        avg_profit = trades_df[trades_df['pnl'] > 0]['pnl'].mean() if len(trades_df[trades_df['pnl'] > 0]) > 0 else 0
        avg_loss = abs(trades_df[trades_df['pnl'] < 0]['pnl'].mean()) if len(trades_df[trades_df['pnl'] < 0]) > 0 else 1
        profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

        # 计算Sharpe比率（简化）
        if len(nav_series) > 1:
            daily_returns = nav_series['total_value'].pct_change().dropna()
            sharpe_ratio = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252) if daily_returns.std() > 0 else 0
        else:
            sharpe_ratio = 0

        # 按模式统计
        pattern_stats = trades_df.groupby('pattern_type').agg({
            'pnl': ['count', 'sum', 'mean'],
            'pnl_pct': 'mean'
        }).round(4)

        # 热点共振 vs 非共振统计
        resonance_stats = trades_df.groupby('hot_resonance').agg({
            'pnl': ['count', 'sum', 'mean'],
            'pnl_pct': 'mean'
        }).round(4)

        report = {
            'total_return': total_return,
            'annualized_return': annualized_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'win_rate': win_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'total_trades': len(self.trade_history),
            'initial_capital': self.config.initial_capital,
            'final_capital': self.total_capital,
            'pattern_stats': pattern_stats,
            'resonance_stats': resonance_stats,
            'daily_nav': self.daily_nav,
            'trade_history': self.trade_history
        }

        return report
