"""
资金流向分析器

功能：
1. 个股资金流向分析（主力/散户资金流向）
2. 龙虎榜游资动向分析
3. 北向资金趋势分析
4. 板块资金流向共振分析
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class MoneyFlowType(Enum):
    """资金流向类型"""
    SUPER_LARGE = "特大单"   # >100万
    LARGE = "大单"         # 20-100万
    MEDIUM = "中单"        # 4-20万
    SMALL = "小单"         # <4万


@dataclass
class StockMoneyFlow:
    """个股资金流向数据"""
    ts_code: str
    name: str
    trade_date: str
    
    # 净流入
    net_mf_amount: float = 0      # 净流入金额（元）
    net_mf_vol: float = 0         # 净流入量（手）
    
    # 大单净流入（主力）
    main_net_amount: float = 0    # 主力净流入（大单+特大单）
    
    # 散户净流入（小单）
    retail_net_amount: float = 0  # 散户净流入（小单）
    
    # 买卖比例
    buy_ratio: float = 0          # 买入占比
    sell_ratio: float = 0         # 卖出占比


@dataclass
class InstitutionActivity:
    """机构/游资活动数据"""
    ts_code: str
    name: str
    trade_date: str
    
    # 龙虎榜数据
    top_list_buy: float = 0       # 龙虎榜买入额
    top_list_sell: float = 0      # 龙虎榜卖出额
    top_list_net: float = 0       # 龙虎榜净买入
    
    # 机构数据
    inst_buy: float = 0           # 机构买入额
    inst_sell: float = 0          # 机构卖出额
    inst_net: float = 0           # 机构净买入
    
    # 活跃度评级
    activity_level: str = ""      # 活跃/一般/低迷


@dataclass
class HSGTFlow:
    """北向资金流向数据"""
    trade_date: str
    north_money: float = 0        # 北向净流入（亿元）
    south_money: float = 0        # 南向净流入（亿元）
    hgt: float = 0                # 沪股通（亿元）
    sgt: float = 0                # 深股通（亿元）
    trend: str = ""               # 流入/流出


class MoneyFlowAnalyzer:
    """资金流向分析器"""

    def __init__(self, data_manager):
        self.dm = data_manager
        # 懒加载扩展模块
        self._extensions = None

    @property
    def extensions(self):
        """懒加载DataManagerExtensions"""
        if self._extensions is None:
            from core.data.data_manager_extensions import DataManagerExtensions
            self._extensions = DataManagerExtensions(self.dm)
        return self._extensions

    # =========================================================================
    # 个股资金流向分析
    # =========================================================================

    def analyze_stock_moneyflow(self, ts_code: str, trade_date: str) -> StockMoneyFlow:
        """
        分析个股资金流向
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            StockMoneyFlow: 资金流向分析结果
        """
        df = self.extensions.get_stock_moneyflow(ts_code, trade_date=trade_date)
        
        if df.empty:
            return StockMoneyFlow(ts_code=ts_code, name="", trade_date=trade_date)
        
        row = df.iloc[0]
        
        # 计算主力净流入（大单+特大单）
        main_buy = row.get('buy_lg_amount', 0) + row.get('buy_elg_amount', 0)
        main_sell = row.get('sell_lg_amount', 0) + row.get('sell_elg_amount', 0)
        main_net = main_buy - main_sell
        
        # 散户净流入（小单）
        retail_buy = row.get('buy_sm_amount', 0)
        retail_sell = row.get('sell_sm_amount', 0)
        retail_net = retail_buy - retail_sell
        
        # 总买入卖出
        total_buy = main_buy + row.get('buy_md_amount', 0) + retail_buy
        total_sell = main_sell + row.get('sell_md_amount', 0) + retail_sell
        
        return StockMoneyFlow(
            ts_code=ts_code,
            name=row.get('name', ''),
            trade_date=trade_date,
            net_mf_amount=row.get('net_mf_amount', 0),
            net_mf_vol=row.get('net_mf_vol', 0),
            main_net_amount=main_net,
            retail_net_amount=retail_net,
            buy_ratio=total_buy / (total_buy + total_sell) * 100 if (total_buy + total_sell) > 0 else 0,
            sell_ratio=total_sell / (total_buy + total_sell) * 100 if (total_buy + total_sell) > 0 else 0,
        )

    def analyze_main_force_direction(self, ts_code: str, trade_date: str,
                                      days: int = 3) -> Dict:
        """
        分析主力资金方向（连续N天）
        
        Args:
            ts_code: 股票代码
            trade_date: 结束日期
            days: 回溯天数
            
        Returns:
            Dict: 主力资金方向分析
        """
        from core.utils import DateUtils
        
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(days, trade_date)
        
        flows = []
        for date in date_list:
            flow = self.analyze_stock_moneyflow(ts_code, date)
            if flow.net_mf_amount != 0:
                flows.append({
                    'date': date,
                    'main_net': flow.main_net_amount,
                    'retail_net': flow.retail_net_amount,
                    'total_net': flow.net_mf_amount,
                })
        
        if not flows:
            return {}
        
        main_nets = [f['main_net'] for f in flows]
        
        return {
            'ts_code': ts_code,
            'avg_main_net': np.mean(main_nets),
            'total_main_net': sum(main_nets),
            'main_inflow_days': sum(1 for x in main_nets if x > 0),
            'main_outflow_days': sum(1 for x in main_nets if x < 0),
            'direction': '流入' if sum(main_nets) > 0 else '流出',
            'strength': self._calculate_flow_strength(main_nets),
            'retail_opposite': self._check_retail_opposite(flows),
        }

    def _calculate_flow_strength(self, values: List[float]) -> str:
        """计算资金流向强度"""
        total = sum(abs(x) for x in values)
        if total == 0:
            return "无数据"
        
        avg = np.mean(values)
        if abs(avg) > 10000000:  # 1000万
            return "强"
        elif abs(avg) > 1000000:  # 100万
            return "中等"
        else:
            return "弱"

    def _check_retail_opposite(self, flows: List[Dict]) -> bool:
        """检查散户是否反向操作"""
        if len(flows) < 2:
            return False
        
        opposite_count = 0
        for f in flows:
            main = f['main_net']
            retail = f['retail_net']
            # 主力流入且散户流出，或主力流出且散户流入
            if (main > 0 and retail < 0) or (main < 0 and retail > 0):
                opposite_count += 1
        
        return opposite_count >= len(flows) * 0.6  # 60%以上天数反向

    # =========================================================================
    # 龙虎榜分析
    # =========================================================================

    def analyze_institution_activity(self, ts_code: str, trade_date: str) -> InstitutionActivity:
        """
        分析机构/游资活动
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            InstitutionActivity: 机构活动分析
        """
        # 获取龙虎榜数据
        top_list = self.extensions.get_top_list(trade_date)
        top_inst = self.extensions.get_top_inst(trade_date, ts_code)
        
        result = InstitutionActivity(
            ts_code=ts_code,
            name="",
            trade_date=trade_date
        )
        
        # 查找该股票的龙虎榜数据
        if not top_list.empty:
            stock_top = top_list[top_list['ts_code'] == ts_code]
            if not stock_top.empty:
                row = stock_top.iloc[0]
                result.name = row.get('name', '')
                result.top_list_buy = row.get('l_buy', 0)
                result.top_list_sell = row.get('l_sell', 0)
                result.top_list_net = row.get('net_amount', 0)
        
        # 机构交易数据
        if not top_inst.empty:
            result.inst_buy = top_inst['buy'].sum() if 'buy' in top_inst.columns else 0
            result.inst_sell = top_inst['sell'].sum() if 'sell' in top_inst.columns else 0
            result.inst_net = top_inst['net_buy'].sum() if 'net_buy' in top_inst.columns else 0
        
        # 活跃度评级
        total_amount = result.top_list_buy + result.top_list_sell
        if total_amount > 500000000:  # 5亿
            result.activity_level = "极度活跃"
        elif total_amount > 100000000:  # 1亿
            result.activity_level = "活跃"
        elif total_amount > 50000000:  # 5000万
            result.activity_level = "一般"
        else:
            result.activity_level = "低迷"
        
        return result

    def get_hot_money_stocks(self, trade_date: str, top_n: int = 10) -> pd.DataFrame:
        """
        获取游资重点参与的股票
        
        Args:
            trade_date: 交易日期
            top_n: 返回前N个
            
        Returns:
            DataFrame: 游资活跃股票列表
        """
        top_list = self.extensions.get_top_list(trade_date)
        
        if top_list.empty:
            return pd.DataFrame()
        
        # 按净买入排序
        if 'net_amount' in top_list.columns:
            return top_list.nlargest(top_n, 'net_amount')
        
        return top_list.head(top_n)

    # =========================================================================
    # 北向资金分析
    # =========================================================================

    def analyze_hsgt_flow(self, trade_date: str) -> HSGTFlow:
        """
        分析北向资金流向
        
        Args:
            trade_date: 交易日期
            
        Returns:
            HSGTFlow: 北向资金分析
        """
        df = self.extensions.get_hsgt_moneyflow(trade_date=trade_date)
        
        if df.empty:
            return HSGTFlow(trade_date=trade_date)
        
        row = df.iloc[0]
        
        north_money = row.get('north_money', 0)
        
        return HSGTFlow(
            trade_date=trade_date,
            north_money=north_money,
            south_money=row.get('south_money', 0),
            hgt=row.get('hgt', 0),
            sgt=row.get('sgt', 0),
            trend='流入' if north_money > 0 else '流出'
        )

    def get_hsgt_top_stocks(self, trade_date: str, market_type: str = '1',
                            top_n: int = 10) -> pd.DataFrame:
        """
        获取北向资金重点买入的股票
        
        Args:
            trade_date: 交易日期
            market_type: 1=沪股通, 3=深股通
            top_n: 返回前N个
            
        Returns:
            DataFrame: 北向资金重点股票
        """
        df = self.extensions.get_hsgt_top10(trade_date, market_type)
        
        if df.empty:
            return pd.DataFrame()
        
        # 按净买入排序
        if 'net_amount' in df.columns:
            return df.nlargest(top_n, 'net_amount')
        
        return df.head(top_n)

    # =========================================================================
    # 板块资金流向共振分析
    # =========================================================================

    def analyze_sector_moneyflow_resonance(self, ts_code: str, trade_date: str) -> Dict:
        """
        分析板块资金流向共振
        
        判断个股、板块、北向资金是否同向
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            Dict: 资金流向共振分析
        """
        # 1. 个股资金流向
        stock_flow = self.analyze_stock_moneyflow(ts_code, trade_date)
        stock_direction = 1 if stock_flow.main_net_amount > 0 else -1
        
        # 2. 北向资金流向
        hsgt_flow = self.analyze_hsgt_flow(trade_date)
        hsgt_direction = 1 if hsgt_flow.north_money > 0 else -1
        
        # 3. 龙虎榜活跃度
        inst_activity = self.analyze_institution_activity(ts_code, trade_date)
        top_list_direction = 1 if inst_activity.top_list_net > 0 else -1
        
        # 计算共振度
        directions = [stock_direction, hsgt_direction, top_list_direction]
        positive_count = sum(1 for d in directions if d > 0)
        negative_count = sum(1 for d in directions if d < 0)
        
        if positive_count >= 2:
            resonance = "正向共振"
            resonance_score = positive_count * 33
        elif negative_count >= 2:
            resonance = "负向共振"
            resonance_score = -negative_count * 33
        else:
            resonance = "无共振"
            resonance_score = 0
        
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'stock_main_direction': '流入' if stock_direction > 0 else '流出',
            'hsgt_direction': '流入' if hsgt_direction > 0 else '流出',
            'top_list_direction': '流入' if top_list_direction > 0 else '流出',
            'resonance': resonance,
            'resonance_score': resonance_score,
            'stock_main_net': stock_flow.main_net_amount,
            'hsgt_north_money': hsgt_flow.north_money,
            'top_list_net': inst_activity.top_list_net,
        }

    def find_moneyflow_resonance_stocks(self, trade_date: str,
                                        stock_list: List[str] = None) -> pd.DataFrame:
        """
        查找资金流向共振的股票
        
        Args:
            trade_date: 交易日期
            stock_list: 股票列表（可选）
            
        Returns:
            DataFrame: 共振股票列表
        """
        if stock_list is None:
            # 获取当日涨停股票
            limit_up_df = self.dm.get_limit_up_pool(trade_date)
            if not limit_up_df.empty:
                stock_list = limit_up_df['code'].tolist() if 'code' in limit_up_df.columns else []
        
        if not stock_list:
            return pd.DataFrame()
        
        results = []
        for ts_code in stock_list:
            try:
                resonance = self.analyze_sector_moneyflow_resonance(ts_code, trade_date)
                if resonance.get('resonance_score', 0) > 50:  # 强共振
                    results.append(resonance)
            except Exception as e:
                logger.warning(f"[find_moneyflow_resonance_stocks] 分析失败 {ts_code}: {e}")
        
        if results:
            return pd.DataFrame(results)
        
        return pd.DataFrame()


# 便捷函数
def create_moneyflow_analyzer(data_manager) -> MoneyFlowAnalyzer:
    """创建资金流向分析器"""
    return MoneyFlowAnalyzer(data_manager)
