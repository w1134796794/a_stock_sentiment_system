"""
DataManager扩展模块

提供额外的数据接口：
1. 个股资金流向 (moneyflow)
2. 龙虎榜数据 (top_list/top_inst)
3. 北向资金流向 (moneyflow_hsgt)
4. 筹码结构数据 (cyq_perf)

设计原则：
- 通过DataManager实例调用
- 统一缓存机制
- 支持多数据源回退
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from datetime import datetime
import loguru

logger = loguru.logger


class DataManagerExtensions:
    """DataManager扩展功能"""

    def __init__(self, data_manager):
        """
        初始化
        
        Args:
            data_manager: DataManager实例
        """
        self.dm = data_manager
        self.ts_pro = data_manager.ts_pro
        self._ensure_cache_dirs()

    def _ensure_cache_dirs(self):
        """确保缓存目录存在"""
        cache_root = self.dm.cache_dir
        
        # 创建资金流向缓存目录
        (cache_root / "moneyflow").mkdir(parents=True, exist_ok=True)
        (cache_root / "moneyflow" / "stock").mkdir(parents=True, exist_ok=True)
        (cache_root / "moneyflow" / "hsgt").mkdir(parents=True, exist_ok=True)
        
        # 创建龙虎榜缓存目录
        (cache_root / "top_list").mkdir(parents=True, exist_ok=True)
        
        # 创建筹码结构缓存目录
        (cache_root / "cyq_perf").mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # 1. 个股资金流向 (moneyflow)
    # =========================================================================

    def get_stock_moneyflow(self, ts_code: str, trade_date: str = None,
                            start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取个股资金流向数据
        
        接口: moneyflow (Tushare)
        数据说明: 每日单只股票的资金流入流出情况
        
        Args:
            ts_code: 股票代码 (如 000001.SZ)
            trade_date: 交易日期 (YYYYMMDD)，获取单日数据
            start_date: 开始日期，获取区间数据
            end_date: 结束日期
            
        Returns:
            DataFrame: 资金流向数据
            核心字段:
                - buy_sm_vol: 小单买入量（手）
                - buy_sm_amount: 小单买入金额（元）
                - sell_sm_vol: 小单卖出量（手）
                - sell_sm_amount: 小单卖出金额（元）
                - buy_md_vol: 中单买入量（手）
                - buy_md_amount: 中单买入金额（元）
                - sell_md_vol: 中单卖出量（手）
                - sell_md_amount: 中单卖出金额（元）
                - buy_lg_vol: 大单买入量（手）
                - buy_lg_amount: 大单买入金额（元）
                - sell_lg_vol: 大单卖出量（手）
                - sell_lg_amount: 大单卖出金额（元）
                - buy_elg_vol: 特大单买入量（手）
                - buy_elg_amount: 特大单买入金额（元）
                - sell_elg_vol: 特大单卖出量（手）
                - sell_elg_amount: 特大单卖出金额（元）
                - net_mf_vol: 净流入量（手）
                - net_mf_amount: 净流入额（元）
        """
        cache_file = self.dm.cache_dir / "moneyflow" / "stock" / f"{ts_code}_{trade_date or start_date}_{end_date}.csv"
        
        # 尝试读取缓存
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 调用Tushare接口
            if trade_date:
                df = self.ts_pro.moneyflow(ts_code=ts_code, trade_date=trade_date)
            else:
                df = self.ts_pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_stock_moneyflow] 获取 {ts_code} 资金流向数据: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_stock_moneyflow] 获取失败 {ts_code}: {e}")
            return pd.DataFrame()

    def get_moneyflow_summary(self, trade_date: str) -> pd.DataFrame:
        """
        获取当日全市场资金流向汇总
        
        Args:
            trade_date: 交易日期 (YYYYMMDD)
            
        Returns:
            DataFrame: 全市场资金流向汇总
        """
        cache_file = self.dm.cache_dir / "moneyflow" / f"summary_{trade_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 获取当日所有股票的资金流向
            df = self.ts_pro.moneyflow(trade_date=trade_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_moneyflow_summary] 获取 {trade_date} 全市场资金流向: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_moneyflow_summary] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    # =========================================================================
    # 2. 龙虎榜数据 (top_list/top_inst)
    # =========================================================================

    def get_top_list(self, trade_date: str) -> pd.DataFrame:
        """
        获取龙虎榜每日明细
        
        接口: top_list (Tushare)
        数据说明: 每日龙虎榜上榜股票明细
        
        Args:
            trade_date: 交易日期 (YYYYMMDD)
            
        Returns:
            DataFrame: 龙虎榜明细
            核心字段:
                - ts_code: 股票代码
                - name: 股票名称
                - close: 收盘价
                - pct_change: 涨跌幅
                - turnover_rate: 换手率
                - amount: 总成交额（元）
                - l_buy: 龙虎榜买入额（元）
                - l_sell: 龙虎榜卖出额（元）
                - net_amount: 龙虎榜净买入额（元）
                - reason: 上榜原因
        """
        cache_file = self.dm.cache_dir / "top_list" / f"top_list_{trade_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            df = self.ts_pro.top_list(trade_date=trade_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_top_list] 获取 {trade_date} 龙虎榜: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_top_list] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    def get_top_inst(self, trade_date: str, ts_code: str = None) -> pd.DataFrame:
        """
        获取龙虎榜机构交易明细
        
        接口: top_inst (Tushare)
        数据说明: 龙虎榜中机构专用席位的交易明细
        
        Args:
            trade_date: 交易日期 (YYYYMMDD)
            ts_code: 股票代码（可选，不填则获取全部）
            
        Returns:
            DataFrame: 机构交易明细
            核心字段:
                - ts_code: 股票代码
                - name: 股票名称
                - exalter: 营业部名称
                - buy: 买入金额（元）
                - buy_rate: 买入占总成交比例
                - sell: 卖出金额（元）
                - sell_rate: 卖出占总成交比例
                - net_buy: 净买入金额（元）
        """
        cache_file = self.dm.cache_dir / "top_list" / f"top_inst_{trade_date}_{ts_code or 'all'}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            if ts_code:
                df = self.ts_pro.top_inst(trade_date=trade_date, ts_code=ts_code)
            else:
                df = self.ts_pro.top_inst(trade_date=trade_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_top_inst] 获取 {trade_date} 机构交易: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_top_inst] 获取失败 {trade_date}: {e}")
            return pd.DataFrame()

    def analyze_top_list_summary(self, trade_date: str) -> Dict:
        """
        分析龙虎榜汇总数据
        
        Args:
            trade_date: 交易日期
            
        Returns:
            Dict: 龙虎榜分析汇总
                - total_stocks: 上榜股票数
                - total_buy: 总买入额
                - total_sell: 总卖出额
                - net_amount: 净买入额
                - top_buy_stocks: 买入最多的股票
                - top_sell_stocks: 卖出最多的股票
                - institution_activity: 机构活跃度
        """
        top_list = self.get_top_list(trade_date)
        top_inst = self.get_top_inst(trade_date)
        
        if top_list.empty:
            return {}
        
        summary = {
            'total_stocks': len(top_list),
            'total_buy': top_list['l_buy'].sum() if 'l_buy' in top_list.columns else 0,
            'total_sell': top_list['l_sell'].sum() if 'l_sell' in top_list.columns else 0,
            'net_amount': top_list['net_amount'].sum() if 'net_amount' in top_list.columns else 0,
        }
        
        # 买入最多的股票
        if 'l_buy' in top_list.columns:
            top_buy = top_list.nlargest(5, 'l_buy')[['ts_code', 'name', 'l_buy', 'net_amount']]
            summary['top_buy_stocks'] = top_buy.to_dict('records')
        
        # 卖出最多的股票
        if 'l_sell' in top_list.columns:
            top_sell = top_list.nlargest(5, 'l_sell')[['ts_code', 'name', 'l_sell', 'net_amount']]
            summary['top_sell_stocks'] = top_sell.to_dict('records')
        
        # 机构活跃度
        if not top_inst.empty and 'net_buy' in top_inst.columns:
            inst_net_buy = top_inst['net_buy'].sum()
            summary['institution_net_buy'] = inst_net_buy
            summary['institution_activity'] = '活跃' if inst_net_buy > 100000000 else '一般'
        
        return summary

    # =========================================================================
    # 3. 北向资金流向 (moneyflow_hsgt)
    # =========================================================================

    def get_hsgt_moneyflow(self, trade_date: str = None,
                           start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取沪深港通资金流向
        
        接口: moneyflow_hsgt (Tushare)
        数据说明: 每日北向资金（沪股通+深股通）流向数据
        
        Args:
            trade_date: 交易日期，获取单日数据
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame: 北向资金流向
            核心字段:
                - trade_date: 交易日期
                - ggt_ss: 港股通（上海）买入金额（亿元）
                - ggt_sz: 港股通（深圳）买入金额（亿元）
                - hgt: 沪股通买入金额（亿元）
                - sgt: 深股通买入金额（亿元）
                - north_money: 北向资金净流入（亿元）
                - south_money: 南向资金净流入（亿元）
        """
        cache_file = self.dm.cache_dir / "moneyflow" / "hsgt" / f"hsgt_{trade_date or start_date}_{end_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            if trade_date:
                df = self.ts_pro.moneyflow_hsgt(trade_date=trade_date)
            else:
                df = self.ts_pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hsgt_moneyflow] 获取北向资金: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_hsgt_moneyflow] 获取失败: {e}")
            return pd.DataFrame()

    def get_hsgt_top10(self, trade_date: str, market_type: str = '1') -> pd.DataFrame:
        """
        获取沪深股通十大成交股
        
        接口: hsgt_top10 (Tushare)
        数据说明: 每日沪深股通成交最多的10只股票
        
        Args:
            trade_date: 交易日期
            market_type: 市场类型 1=沪股通，3=深股通
            
        Returns:
            DataFrame: 十大成交股
            核心字段:
                - ts_code: 股票代码
                - name: 股票名称
                - close: 收盘价
                - change: 涨跌额
                - rank: 资金排名
                - market_type: 市场类型
                - amount: 成交金额（元）
                - net_amount: 净成交金额（元）
                - buy: 买入金额（元）
                - sell: 卖出金额（元）
        """
        cache_file = self.dm.cache_dir / "moneyflow" / "hsgt" / f"top10_{market_type}_{trade_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            df = self.ts_pro.hsgt_top10(trade_date=trade_date, market_type=market_type)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_hsgt_top10] 获取十大成交股: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_hsgt_top10] 获取失败: {e}")
            return pd.DataFrame()

    def analyze_hsgt_trend(self, trade_date: str, days: int = 5) -> Dict:
        """
        分析北向资金趋势
        
        Args:
            trade_date: 当前日期
            days: 回溯天数
            
        Returns:
            Dict: 北向资金趋势分析
        """
        from core.utils import DateUtils
        
        date_utils = DateUtils()
        date_list = date_utils.get_last_n_trade_dates(days, trade_date)
        
        if len(date_list) < days:
            logger.warning(f"[analyze_hsgt_trend] 历史数据不足 {len(date_list)}/{days}")
        
        # 获取历史数据
        flows = []
        for date in date_list:
            df = self.get_hsgt_moneyflow(trade_date=date)
            if not df.empty and 'north_money' in df.columns:
                flows.append({
                    'date': date,
                    'north_money': df['north_money'].iloc[0]
                })
        
        if not flows:
            return {}
        
        # 计算趋势
        north_moneys = [f['north_money'] for f in flows]
        
        return {
            'current': north_moneys[0] if north_moneys else 0,
            'avg_5d': np.mean(north_moneys) if len(north_moneys) >= 5 else np.mean(north_moneys),
            'trend': '流入' if north_moneys[0] > 0 else '流出',
            'continuous_days': self._count_continuous_days(north_moneys),
            'total_flow_5d': sum(north_moneys),
        }

    def _count_continuous_days(self, values: List[float]) -> int:
        """计算连续流入/流出天数"""
        if not values:
            return 0
        
        sign = 1 if values[0] > 0 else -1
        count = 0
        
        for v in values:
            if (sign > 0 and v > 0) or (sign < 0 and v < 0):
                count += 1
            else:
                break
        
        return count

    # =========================================================================
    # 4. 筹码结构数据 (cyq_perf)
    # =========================================================================

    def get_cyq_perf(self, ts_code: str, trade_date: str = None,
                     start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取股票筹码分布数据
        
        接口: cyq_perf (Tushare)
        数据说明: 每日股票的筹码分布和获利情况
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期，获取单日数据
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame: 筹码分布数据
            核心字段:
                - trade_date: 交易日期
                - close: 收盘价
                - avg_cost: 平均成本
                - pct_chg: 涨跌幅
                - turnover: 换手率
                - profit_pct: 获利盘比例（%）
                - avg_profit: 平均获利比例（%）
                - avg_loss: 平均亏损比例（%）
                - max_profit: 最大获利比例（%）
                - max_loss: 最大亏损比例（%）
                - concentration: 筹码集中度（%）
        """
        cache_file = self.dm.cache_dir / "cyq_perf" / f"{ts_code}_{trade_date or start_date}_{end_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            if trade_date:
                df = self.ts_pro.cyq_perf(ts_code=ts_code, trade_date=trade_date)
            else:
                df = self.ts_pro.cyq_perf(ts_code=ts_code, start_date=start_date, end_date=end_date)
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_cyq_perf] 获取 {ts_code} 筹码数据: {len(df)}条")
            
            return df
        except Exception as e:
            logger.error(f"[get_cyq_perf] 获取失败 {ts_code}: {e}")
            return pd.DataFrame()

    def analyze_chips_structure(self, ts_code: str, trade_date: str) -> Dict:
        """
        分析个股筹码结构
        
        Args:
            ts_code: 股票代码
            trade_date: 交易日期
            
        Returns:
            Dict: 筹码结构分析
                - profit_pct: 获利盘比例
                - concentration: 筹码集中度
                - avg_cost: 平均成本
                - cost_vs_price: 成本与现价对比
                - structure_type: 筹码结构类型
        """
        df = self.get_cyq_perf(ts_code=ts_code, trade_date=trade_date)
        
        if df.empty:
            return {}
        
        row = df.iloc[0]
        
        profit_pct = row.get('profit_pct', 0)
        concentration = row.get('concentration', 0)
        avg_cost = row.get('avg_cost', 0)
        close = row.get('close', 0)
        
        # 判断筹码结构类型
        if profit_pct >= 80:
            structure_type = '高位密集' if concentration > 30 else '高位分散'
        elif profit_pct <= 20:
            structure_type = '低位密集' if concentration > 30 else '低位分散'
        else:
            structure_type = '震荡整理'
        
        return {
            'profit_pct': profit_pct,
            'concentration': concentration,
            'avg_cost': avg_cost,
            'close': close,
            'cost_vs_price': ((close - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0,
            'structure_type': structure_type,
        }

    def get_batch_cyq_perf(self, ts_codes: List[str], trade_date: str) -> pd.DataFrame:
        """
        批量获取筹码数据
        
        Args:
            ts_codes: 股票代码列表
            trade_date: 交易日期
            
        Returns:
            DataFrame: 批量筹码数据
        """
        results = []
        
        for ts_code in ts_codes:
            df = self.get_cyq_perf(ts_code=ts_code, trade_date=trade_date)
            if not df.empty:
                results.append(df)
        
        if results:
            return pd.concat(results, ignore_index=True)
        
        return pd.DataFrame()


# 便捷函数
def create_extensions(data_manager) -> DataManagerExtensions:
    """创建DataManagerExtensions实例"""
    return DataManagerExtensions(data_manager)
