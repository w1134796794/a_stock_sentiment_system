"""
数据管理层 - 负责多源数据获取与本地缓存
支持Tushare + AkShare双源备份
"""
import os
import json
import pandas as pd
import tushare as ts
import akshare as ak
from datetime import datetime, timedelta
from pathlib import Path
import loguru
from typing import Optional, Dict, List
import time

# 导入工具类
from core.utils import (
    DateUtils,
    StockCodeUtils,
    TimeUtils,
    CalculationUtils,
    ValidationUtils,
)

logger = loguru.logger


class DataManager:
    """数据管理层 - 负责多源数据获取与本地缓存
    
    缓存目录结构:
        cache/
        ├── market/              # 市场基础数据
        │   ├── daily_basic/     # 每日基础行情
        │   ├── limit_up/        # 涨停池
        │   ├── limit_down/      # 跌停池
        │   └── rt_k/            # 实时行情
        ├── stock/               # 个股数据
        │   ├── daily/           # 个股日线
        │   ├── tick/            # 分时数据
        │   └── auction/         # 集合竞价
        ├── sector/              # 板块/行业数据
        │   ├── ths_index/       # 同花顺板块
        │   ├── ths_member/      # 同花顺成分股
        │   ├── dc_industry/     # 东财行业
        │   └── moneyflow/       # 资金流向
        ├── concept/             # 概念数据
        │   └── members/         # 概念成分股
        └── summary/             # 汇总数据
            └── limit_up_stocks.csv  # 涨停汇总
    """
    
    def __init__(self, tushare_token: str, cache_dir: Path):
        self.ts_pro = ts.pro_api(tushare_token) if tushare_token != "your_tushare_token_here" else None
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化分层缓存目录
        self._init_cache_structure()
        
        # 初始化工具类（单例模式）
        self.date_utils = DateUtils()
        self.stock_code_utils = StockCodeUtils()
        self.time_utils = TimeUtils()
        self.calculation_utils = CalculationUtils()
        self.validation_utils = ValidationUtils()
    
    def _init_cache_structure(self):
        """初始化缓存目录结构"""
        # 市场数据
        self.market_dir = self.cache_dir / "market"
        (self.market_dir / "daily_basic").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_up").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_down").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "rt_k").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_step").mkdir(parents=True, exist_ok=True)
        (self.market_dir / "limit_cpt").mkdir(parents=True, exist_ok=True)

        # 个股数据
        self.stock_dir = self.cache_dir / "stock"
        (self.stock_dir / "daily").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_price").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_data").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "daily_basic").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "tick").mkdir(parents=True, exist_ok=True)
        (self.stock_dir / "auction").mkdir(parents=True, exist_ok=True)

        # 板块数据
        self.sector_dir = self.cache_dir / "sector"
        (self.sector_dir / "ths_index").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "ths_member").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "ths_daily").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "dc_industry").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "industry").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "industry_cons").mkdir(parents=True, exist_ok=True)
        (self.sector_dir / "moneyflow").mkdir(parents=True, exist_ok=True)

        # 概念数据
        self.concept_dir = self.cache_dir / "concept"
        (self.concept_dir / "members").mkdir(parents=True, exist_ok=True)

        # 汇总数据
        self.summary_dir = self.cache_dir / "summary"
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        
    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """获取每日行情基础数据（带缓存）"""
        cache_file = self.market_dir / "daily_basic" / f"{trade_date}.csv"
        if cache_file.exists():
            logger.info(f"从缓存加载 {trade_date} 基础行情")
            return pd.read_csv(cache_file)

        logger.info(f"从API获取 {trade_date} 基础行情")
        try:
            if self.ts_pro:
                df = self.ts_pro.daily_basic(trade_date=trade_date)
                if not df.empty:
                    df.to_csv(cache_file, index=False)
                    return df
        except Exception as e:
            logger.error(f"Tushare获取失败: {e}")
    
    def get_limit_up_pool(self, date: str) -> pd.DataFrame:
        """
        获取涨停池数据

        使用Tushare limit_list_d接口获取涨停数据

        Args:
            date: 日期，格式YYYYMMDD

        Returns:
            涨停数据DataFrame
        """
        cache_file = self.market_dir / "limit_up" / f"{date}.csv"

        if cache_file.exists():
            logger.debug(f"[get_limit_up_pool] 从缓存加载 {date} 涨停池数据")
            df = pd.read_csv(cache_file)
            # 确保缓存数据也经过标准化（处理旧格式缓存）
            df = self._normalize_limit_up_format(df, date)
            self._append_to_limit_up_summary(df, date)
            return df

        if not self.ts_pro:
            logger.warning("[get_limit_up_pool] Tushare未初始化")
            return pd.DataFrame()

        try:
            # 使用limit_list_d接口获取涨跌停数据
            df = self.ts_pro.limit_list_d(trade_date=date)

            if df.empty:
                logger.warning(f"[get_limit_up_pool] {date} 未获取到数据")
                return pd.DataFrame()

            # 筛选涨停数据（limit='U'表示涨停）
            df = df[df['limit'] == 'U'].copy()

            if df.empty:
                logger.info(f"[get_limit_up_pool] {date} 无涨停股票")
                return pd.DataFrame()

            # 标准化格式
            df = self._normalize_limit_up_format(df, date)

            # 缓存结果
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_limit_up_pool] 获取 {date} 涨停池数据: {len(df)}条")
            self._append_to_limit_up_summary(df, date)

            return df

        except Exception as e:
            logger.error(f"[get_limit_up_pool] 获取 {date} 涨停池数据失败: {e}")
            return pd.DataFrame()

    def get_limit_down_pool(self, date: str) -> pd.DataFrame:
        """
        获取跌停池数据
        
        使用Tushare limit_list_d接口获取跌停数据
        
        Args:
            date: 日期，格式YYYYMMDD
            
        Returns:
            跌停数据DataFrame
        """
        # 缓存文件路径
        cache_file = self.market_dir / "limit_down" / f"{date}.csv"
        
        if cache_file.exists():
            logger.debug(f"[get_limit_down_pool] 从缓存加载 {date} 跌停池数据")
            return pd.read_csv(cache_file)
        
        if not self.ts_pro:
            logger.warning("[get_limit_down_pool] Tushare未初始化")
            return pd.DataFrame()
        
        try:
            # 使用limit_list_d接口获取涨跌停数据
            df = self.ts_pro.limit_list_d(trade_date=date)
            
            if df.empty:
                logger.warning(f"[get_limit_down_pool] {date} 未获取到数据")
                return pd.DataFrame()
            
            # 筛选跌停数据（limit='D'表示跌停）
            df = df[df['limit'] == 'D'].copy()
            
            if df.empty:
                logger.info(f"[get_limit_down_pool] {date} 无跌停股票")
                return pd.DataFrame()
            
            # 缓存结果
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_limit_down_pool] 获取 {date} 跌停池数据: {len(df)}条")
            
            return df
            
        except Exception as e:
            logger.error(f"[get_limit_down_pool] 获取 {date} 跌停池数据失败: {e}")
            return pd.DataFrame()

    def get_all_rt_k_data(self, trade_date: str) -> pd.DataFrame:
        """
        批量获取所有股票的实时日线数据（rt_k接口）

        rt_k接口不传入ts_code时返回所有股票的实时数据，每分钟只能调用一次。
        此方法将结果缓存到文件，后续需要实时日线数据时直接从缓存读取。

        Args:
            trade_date: 交易日期（YYYYMMDD），必须是交易日

        Returns:
            pd.DataFrame: 包含所有股票实时数据的DataFrame，失败返回空DataFrame
            列包括: ts_code, trade_date, open, high, low, close, pre_close, change, pct_change, vol, amount
        """
        # 缓存文件路径
        cache_file = self.market_dir / "rt_k" / f"{trade_date}.csv"
        
        # 检查缓存是否存在且有效（缓存文件在当天交易时间内生成）
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                if not df.empty:
                    logger.info(f"[get_all_rt_k_data] 从缓存加载所有股票实时数据: {len(df)}条")
                    return df
            except Exception as e:
                logger.warning(f"[get_all_rt_k_data] 读取缓存失败: {e}")
        
        if not self.ts_pro:
            logger.warning("[get_all_rt_k_data] Tushare未初始化")
            return pd.DataFrame()

        try:
            logger.info(f"[get_all_rt_k_data] 调用rt_k接口获取所有股票实时数据（不传入ts_code）")
            # 不传入ts_code，获取所有股票的实时数据
            df = self.ts_pro.rt_k(ts_code='3*.SZ,6*.SH,0*.SZ,9*.BJ')

            if df is None or df.empty:
                logger.warning("[get_all_rt_k_data] rt_k返回空数据")
                return pd.DataFrame()

            # 缓存结果
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_all_rt_k_data] 成功获取并缓存所有股票实时数据: {len(df)}条")

            return df
            
        except Exception as e:
            logger.error(f"[get_all_rt_k_data] 获取所有股票实时数据失败: {e}")
            return pd.DataFrame()
    
    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取个股历史日线数据

        Args:
            ts_code: 股票代码（已带后缀，如 000001.SZ）
            start_date: 开始日期（YYYYMMDD）
            end_date: 结束日期（YYYYMMDD）

        Returns:
            个股日线数据DataFrame
        """
        code = ts_code

        cache_file = self.stock_dir / "daily" / f"{code}_{start_date}_{end_date}.csv"
        if cache_file.exists():
            # logger.debug(f"[get_stock_daily] 从缓存加载: {cache_file}")
            return pd.read_csv(cache_file, parse_dates=['trade_date'])

        try:
            if self.ts_pro:
                # logger.debug(f"[get_stock_daily] 调用Tushare daily接口: ts_code={code}")
                df = self.ts_pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                # logger.debug(f"[get_stock_daily] Tushare返回 {len(df)} 条数据")
                if not df.empty:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    df.to_csv(cache_file, index=False)
                    # logger.debug(f"[get_stock_daily] 从Tushare获取并缓存数据: {cache_file}")
                    return df
                else:
                    logger.debug(f"[get_stock_daily] Tushare返回空数据")
            else:
                logger.debug(f"[get_stock_daily] Tushare未初始化")
        except Exception as e:
            logger.error(f"[get_stock_daily] 获取个股{code}历史数据失败: {e}")
        return pd.DataFrame()
     
    def get_stock_daily_price(self, ts_code: str, trade_date: str) -> Dict:
        """
        获取个股某日的开盘价、收盘价、昨收价（复用get_stock_daily）

        Args:
            ts_code: 股票代码（已带后缀，如 002218.SZ）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            Dict: 包含 open, close, pre_close 的字典，获取失败返回空字典
        """
        df = self.get_stock_daily(ts_code, trade_date, trade_date)
        if df.empty:
            return {}

        row = df.iloc[0]
        return {
            'open': float(row.get('open', 0)),
            'close': float(row.get('close', 0)),
            'pre_close': float(row.get('pre_close', 0))
        }

    def get_stock_daily_data(self, ts_code: str, trade_date: str) -> Dict:
        """
        获取股票日行情数据（复用get_stock_daily）

        Args:
            ts_code: 股票代码（已带后缀，如 000001.SZ）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            Dict: 包含以下字段的字典，获取失败返回空字典
                - ts_code: 股票代码
                - trade_date: 交易日期
                - open: 开盘价
                - high: 最高价
                - low: 最低价
                - close: 收盘价
                - pre_close: 昨收价
                - change: 涨跌额
                - pct_chg: 涨跌幅
                - vol: 成交量（手）
                - amount: 成交额（千元）
        """
        df = self.get_stock_daily(ts_code, trade_date, trade_date)
        if df.empty:
            return {}

        row = df.iloc[0]
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'open': float(row.get('open', 0)),
            'high': float(row.get('high', 0)),
            'low': float(row.get('low', 0)),
            'close': float(row.get('close', 0)),
            'pre_close': float(row.get('pre_close', 0)),
            'change': float(row.get('change', 0)),
            'pct_chg': float(row.get('pct_chg', 0)),
            'vol': float(row.get('vol', 0)),
            'amount': float(row.get('amount', 0))
        }

    def get_stock_daily_basic(self, ts_code: str, trade_date: str) -> Dict:
        """
        获取股票每日基本面指标（从get_daily_basic筛选）
        包含实际换手率、流通股本等关键指标

        Args:
            ts_code: 股票代码（已带后缀，如 000001.SZ）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            Dict: 包含以下字段的字典，获取失败返回空字典
                - ts_code: 股票代码
                - trade_date: 交易日期
                - close: 收盘价
                - turnover_rate: 换手率（%）
                - turnover_rate_f: 实际换手率（自由流通股，%）
                - float_share: 流通股本（万股）
                - free_share: 自由流通股本（万股）
                - total_share: 总股本（万股）
                - circ_mv: 流通市值（万元）
                - total_mv: 总市值（万元）
        """
        # 从全市场数据筛选
        df = self.get_daily_basic(trade_date)
        if df.empty:
            return {}

        # 筛选指定股票
        stock_data = df[df['ts_code'] == ts_code]
        if stock_data.empty:
            return {}

        row = stock_data.iloc[0]
        return {
            'ts_code': ts_code,
            'trade_date': trade_date,
            'close': float(row.get('close', 0)),
            'turnover_rate': float(row.get('turnover_rate', 0)),
            'turnover_rate_f': float(row.get('turnover_rate_f', 0)),
            'float_share': float(row.get('float_share', 0)),
            'free_share': float(row.get('free_share', 0)),
            'total_share': float(row.get('total_share', 0)),
            'circ_mv': float(row.get('circ_mv', 0)),
            'total_mv': float(row.get('total_mv', 0)),
        }


    def get_limit_cpt_list(self, trade_date: str) -> pd.DataFrame:
        """
        获取最强板块统计数据

        接口：limit_cpt_list
        描述：获取每天涨停股票最多最强的概念板块，可以分析强势板块的轮动，判断资金动向
        限量：单次最大2000行数据，可根据股票代码或者日期循环提取全部
        积分：8000积分以上每分钟500次，每天总量不限制

        Args:
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            最强板块统计DataFrame，包含字段：
            - trade_date: 交易日期
            - concept: 概念板块名称
            - name: 板块名称
            - up_nums: 涨停股票数量
            - cons_nums: 连续涨停数量
            - turnover_rate: 板块平均换手率
            - amount: 板块成交金额（万元）
            - float_mv: 板块流通市值（万元）
        """
        if not self.ts_pro:
            logger.warning("[get_limit_cpt_list] Tushare未初始化")
            return pd.DataFrame()

        # 缓存文件路径
        cache_file = self.market_dir / "limit_cpt" / f"{trade_date}.csv"
        if cache_file.exists():
            logger.debug(f"[get_limit_cpt_list] 从缓存加载 {trade_date} 最强板块数据")
            return pd.read_csv(cache_file)

        try:
            # 调用Tushare接口
            df = self.ts_pro.limit_cpt_list(trade_date=trade_date)

            if df is not None and not df.empty:
                # 过滤ST板块(885699.TI)
                if 'ts_code' in df.columns:
                    df = df[df['ts_code'] != '885699.TI'].copy()
                elif 'code' in df.columns:
                    df = df[df['code'] != '885699.TI'].copy()
                logger.info(f"[get_limit_cpt_list] 过滤ST板块后剩余: {len(df)}条")

                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_cpt_list] {trade_date} 返回空数据")

        except Exception as e:
            logger.error(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据异常: {e}")

        return pd.DataFrame()

    def _normalize_limit_up_format(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """
        标准化涨停数据格式，统一列名以兼容现有代码
        保留Tushare特有字段，添加行业代码映射
        
        Args:
            df: 原始涨停数据DataFrame
            trade_date: 交易日期，用于获取行业代码映射
            
        Returns:
            标准化后的DataFrame
        """
        if df.empty:
            return df
        
        result_df = df.copy()
        
        # 如果已经是标准格式（有'代码'列），直接返回
        if '代码' in result_df.columns:
            return result_df
        
        # Tushare格式 -> 标准格式
        column_mapping = {
            'ts_code': '代码',
            'name': '名称',
            'close': '最新价',
            'pct_chg': '涨跌幅',
            'industry': '所属行业',
            'first_time': '首次封板时间',
            'last_time': '最后封板时间',
            'open_times': '炸板次数',
            'limit_times': '连板数',
            'up_stat': '涨停统计',
            'amount': '成交额',
            'turnover_ratio': '换手率',
            'float_mv': '流通市值',
            'total_mv': '总市值'
        }
        
        # 重命名列
        for old_col, new_col in column_mapping.items():
            if old_col in result_df.columns:
                result_df[new_col] = result_df[old_col]
        
        # 处理Tushare特有字段 - 保留原始字段同时添加中文别名
        if 'limit_amount' in result_df.columns:
            result_df['板上成交金额'] = result_df['limit_amount']
        if 'fd_amount' in result_df.columns:
            result_df['封单金额'] = result_df['fd_amount']
        
        # 设置默认值
        if '连板数' in result_df.columns:
            result_df['连板数'] = result_df['连板数'].fillna(1)
        if '涨停统计' in result_df.columns:
            result_df['涨停统计'] = result_df['涨停统计'].fillna('1/1')
        
        # 添加行业代码映射
        if '所属行业' in result_df.columns and trade_date:
            result_df = self._add_industry_code(result_df, trade_date)
        
        return result_df
    
    def _add_industry_code(self, df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """
        为涨停数据添加行业代码
        
        Args:
            df: 涨停数据DataFrame
            trade_date: 交易日期
            
        Returns:
            添加行业代码后的DataFrame
        """
        result_df = df.copy()
        
        # 获取行业列表（包含代码和名称）
        industry_list = self.get_dc_industry_list(trade_date)
        if industry_list.empty:
            logger.warning(f"无法获取行业列表，无法添加行业代码")
            result_df['行业代码'] = ''
            return result_df
        
        # 创建行业名称到代码的映射
        # Tushare的行业名称和dc_index的可能略有不同，需要处理
        name_to_code = {}
        for _, row in industry_list.iterrows():
            name = row.get('name', '')
            ts_code = row.get('ts_code', '')
            if name and ts_code:
                name_to_code[name] = ts_code
        
        # 为每只股票添加行业代码
        industry_codes = []
        for _, row in result_df.iterrows():
            industry_name = row.get('所属行业', '')
            # 直接匹配
            code = name_to_code.get(industry_name, '')
            
            # 如果直接匹配失败，尝试模糊匹配
            if not code:
                for name, ts_code in name_to_code.items():
                    if industry_name in name or name in industry_name:
                        code = ts_code
                        break
            
            industry_codes.append(code)
        
        result_df['行业代码'] = industry_codes
        
        # 统计匹配情况
        matched = sum(1 for c in industry_codes if c)
        logger.info(f"行业代码映射: {matched}/{len(result_df)}只股票匹配成功")
        
        return result_df

    def _append_to_limit_up_summary(self, df: pd.DataFrame, trade_date: str):
        """
        将涨停数据追加到汇总文件
        
        优化：
        1. 使用交易日历判断是否为交易日，非交易日直接跳过
        2. 通过代码列表比对判断是否是重复数据（备用方案）
        
        Args:
            df: 涨停池数据
            trade_date: 交易日期
        """
        try:
            # 检查是否为交易日
            if not self.date_utils.is_trade_date(trade_date):
                logger.info(f"{trade_date}不是交易日，跳过追加")
                return
            
            # 汇总文件路径
            summary_file = self.summary_dir / "limit_up_stocks.csv"
            
            # 添加交易日期列
            df_copy = df.copy()
            df_copy['trade_date'] = trade_date
            
            # 选择关键字段（如果存在）
            key_columns = ['trade_date', '代码', '名称', '涨跌幅', '最新价', 
                          '成交额', '流通市值', '所属行业', '涨停封单量', 
                          '涨停封单额', '首次封板时间', '最后封板时间', '炸板次数', '连板数']
            
            # 过滤出存在的列
            existing_columns = [col for col in key_columns if col in df_copy.columns]
            df_summary = df_copy[existing_columns].copy()
            
            # 如果汇总文件存在，检查是否已有该日期或相同代码列表的数据（去重）
            if summary_file.exists():
                try:
                    existing_df = pd.read_csv(summary_file)
                    
                    # 1. 检查是否已有该日期的数据
                    if trade_date in existing_df['trade_date'].astype(str).values:
                        return
                    
                    # 2. 检查是否与最近一个交易日的代码列表完全相同（处理非交易日问题）
                    if '代码' in df.columns and not existing_df.empty:
                        # 获取最近一个交易日的数据
                        latest_date = existing_df['trade_date'].astype(str).max()
                        latest_df = existing_df[existing_df['trade_date'].astype(str) == latest_date]
                        
                        if len(latest_df) == len(df):
                            # 比较代码列表是否相同
                            latest_codes = set(latest_df['代码'].astype(str))
                            current_codes = set(df['代码'].astype(str))
                            
                            if latest_codes == current_codes:
                                return
                                
                except Exception as e:
                    logger.warning(f"检查汇总文件去重时出错: {e}")
            
            # 追加到汇总文件（文件不存在时会自动创建）
            header = not summary_file.exists()
            df_summary.to_csv(summary_file, mode='a', header=header, index=False)
            action = "创建汇总文件并写入" if header else "追加"
            logger.info(f"{action}{len(df_summary)}条涨停数据: {summary_file}")
                
        except Exception as e:
            logger.warning(f"追加涨停数据到汇总文件失败: {e}")
    
    def get_dc_industry_list(self, trade_date: str) -> pd.DataFrame:
        """
        获取东财行业板块列表（使用dc_index接口，包含行业代码）

        Args:
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            行业板块DataFrame，包含行业代码(ts_code)和名称(name)
        """
        if not self.ts_pro:
            logger.warning("Tushare未初始化，无法获取东财行业板块")
            return pd.DataFrame()

        cache_file = self.sector_dir / "dc_industry" / f"list_{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            # 使用dc_index接口获取行业板块数据
            df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=trade_date)

            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"获取东财行业板块列表: {len(df)}个行业")
            return df
        except Exception as e:
            logger.warning(f"获取东财行业板块列表失败: {e}")
            return pd.DataFrame()
    
    def get_dc_industry_cons(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        """
        获取东财行业板块成分股（使用行业代码关联）
        
        Args:
            ts_code: 行业代码（如'BK1627.DC'）
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            行业成分股DataFrame
        """
        if not self.ts_pro:
            logger.warning("Tushare未初始化，无法获取东财行业成分")
            return pd.DataFrame()

        cache_file = self.sector_dir / "dc_industry" / f"cons_{ts_code}_{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            # 使用dc_member接口获取行业成分（不是dc_index_cons）
            df = self.ts_pro.dc_member(ts_code=ts_code, trade_date=trade_date)

            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"获取东财行业{ts_code}成分: {len(df)}只股票")
            return df
        except Exception as e:
            logger.warning(f"获取东财行业成分失败: {e}")
            return pd.DataFrame()
    
    def find_industry_by_stock(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """
        根据股票代码查找所属行业（使用dc_index接口）

        Args:
            ts_code: 股票代码（已带后缀，如'000001.SZ'）
            trade_date: 交易日期

        Returns:
            该股票所属的行业列表
        """
        if not self.ts_pro:
            logger.warning("Tushare未初始化")
            return pd.DataFrame()

        stock_code = ts_code

        # 获取所有行业列表
        industry_df = self.get_dc_industry_list(trade_date)
        if industry_df.empty:
            return pd.DataFrame()

        # 遍历所有行业，查找包含该股票的行业
        result = []
        for _, row in industry_df.iterrows():
            industry_ts_code = row['ts_code']
            cons_df = self.get_dc_industry_cons(industry_ts_code, trade_date)
            # dc_member返回的列是con_code而不是ts_code
            if not cons_df.empty and stock_code in cons_df['con_code'].values:
                result.append({
                    'industry_ts_code': row['ts_code'],
                    'industry_name': row['name'],
                    'industry_level': row.get('level', ''),
                    'stock_ts_code': stock_code
                })

        if result:
            return pd.DataFrame(result)
        return pd.DataFrame()
    

        """获取行业成分股"""
        cache_key = f"industry_cons_{industry}"
        cache_file = self.sector_dir / "industry_cons" / f"{industry}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            df = ak.stock_board_industry_cons_em(symbol=industry)
            df.to_csv(cache_file, index=False)
            return df
        except Exception as e:
            logger.warning(f"获取行业{industry}成分股失败: {e}")
            return pd.DataFrame()
    
    def get_stock_concepts(self, ts_code: str) -> str:
        """
        获取个股所属概念（使用tushare kpl_concept_cons接口）

        Args:
            ts_code: 股票代码（已带后缀，如 002218.SZ）

        Returns:
            概念字符串，用逗号分隔
        """
        if not self.ts_pro:
            return ''

        code = ts_code
        cache_file = self.concept_dir / "members" / f"{code.replace('.', '_')}.csv"
        
        if cache_file.exists():
            df = pd.read_csv(cache_file)
            # kpl_concept_cons返回的概念名称在'name'列
            if 'name' in df.columns and not df.empty:
                concepts = df['name'].dropna().unique()
                return ','.join(concepts)
            return ''
        
        try:
            # 使用tushare kpl_concept_cons接口获取概念
            df = self.ts_pro.kpl_concept_cons(con_code=code)
            # kpl_concept_cons返回的概念名称在'name'列
            if 'name' in df.columns and not df.empty:
                df.to_csv(cache_file, index=False)
                concepts = df['name'].dropna().unique()
                # 去重并用逗号分隔
                return ','.join(concepts)
        except Exception as e:
            logger.warning(f"获取股票{code}概念数据失败: {e}")
        
        return ''
       
    def get_concept_members(self, trade_date: str) -> pd.DataFrame:
        """
        获取所有概念板块的成分股数据（使用tushare dc_member接口）

        Args:
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            概念成分股DataFrame，包含股票代码和所属概念
        """
        if not self.ts_pro:
            return pd.DataFrame()

        cache_file = self.concept_dir / "members" / f"all_{trade_date}.csv"

        # 检查缓存是否存在（文件名已包含日期，直接按日期区分）
        if cache_file.exists():
            logger.info(f"使用缓存的概念成分股数据: {cache_file}")
            return pd.read_csv(cache_file)

        try:
            # 1. 获取所有概念板块
            concepts_df = self.ts_pro.dc_index(idx_type='概念板块', trade_date=trade_date)
            
            if concepts_df.empty:
                logger.warning("无法获取概念板块数据")
                return pd.DataFrame()
            
            logger.info(f"获取到{len(concepts_df)}个概念板块，开始获取成分股...")
            
            # 2. 获取每个概念板块的成分股
            all_members = []
            for _, concept_row in concepts_df.iterrows():
                concept_code = concept_row['ts_code']
                concept_name = concept_row['name']
                
                try:
                    members_df = self.ts_pro.dc_member(ts_code=concept_code, trade_date=trade_date)
                    if not members_df.empty:
                        # 添加概念名称列
                        members_df['concept_name'] = concept_name
                        all_members.append(members_df[['con_code', 'name', 'concept_name']])
                except Exception as e:
                    logger.warning(f"获取概念{concept_name}成分股失败: {e}")
                    continue
            
            if not all_members:
                logger.warning("未获取到任何概念成分股数据")
                return pd.DataFrame()
            
            # 3. 合并所有成分股数据
            result_df = pd.concat(all_members, ignore_index=True)

            # 4. 缓存数据
            result_df.to_csv(cache_file, index=False)
            logger.info(f"获取概念成分股完成: {len(result_df)}条记录，涉及{len(concepts_df)}个概念，已缓存至 {cache_file}")
            
            return result_df
            
        except Exception as e:
            logger.error(f"获取概念成分股数据失败: {e}")
            return pd.DataFrame()
    
    def get_stock_concepts_from_members(self, ts_code: str, trade_date: str) -> str:
        """
        从概念成分股数据中获取个股所属概念

        Args:
            ts_code: 股票代码（已带后缀，如 002218.SZ）
            trade_date: 交易日期

        Returns:
            概念字符串，用逗号分隔
        """
        code = ts_code

        # 获取概念成分股数据
        members_df = self.get_concept_members(trade_date)
        if members_df.empty:
            return ''

        # 匹配股票代码
        matched = members_df[members_df['con_code'] == code]

        if matched.empty:
            return ''

        # 获取所有概念名称并去重
        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)


    def get_stock_tick(self, ts_code: str, trade_date: str) -> pd.DataFrame:
        """
        获取个股分时数据（1分钟线）

        使用Tushare rt_min接口获取实时分钟数据，比AkShare更稳定

        Args:
            ts_code: 股票代码（已带后缀，如 002218.SZ）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            分时数据DataFrame，包含time, price, volume等列
        """
        cache_file = self.stock_dir / "tick" / f"{ts_code}_{trade_date}.csv"

        # 检查缓存
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                logger.debug(f"从缓存加载分时数据: {ts_code} {trade_date}")
                return df
            except Exception as e:
                logger.warning(f"读取缓存分时数据失败: {e}")

        # 优先使用Tushare rt_min接口
        if self.ts_pro:
            try:
                code = ts_code
                
                logger.debug(f"[get_stock_tick] 使用Tushare rt_min接口: {ts_code}")
                
                # 使用rt_min接口获取1分钟数据（频率参数必须大写）
                df = self.ts_pro.rt_min(ts_code=ts_code, freq='1MIN')
                
                if df is None:
                    logger.warning(f"[get_stock_tick] Tushare rt_min返回None: {code}")
                    return pd.DataFrame()
                
                if not isinstance(df, pd.DataFrame):
                    logger.warning(f"[get_stock_tick] Tushare rt_min返回类型错误: {type(df)}, {code}")
                    return pd.DataFrame()
                
                if df.empty:
                    logger.warning(f"[get_stock_tick] Tushare rt_min返回空数据: {code}")
                    return pd.DataFrame()
                
                # 检查必要的列是否存在（rt_min返回的列名是time而不是trade_time）
                required_cols = ['time', 'open', 'close', 'high', 'low', 'vol', 'amount']
                missing_cols = [col for col in required_cols if col not in df.columns]
                if missing_cols:
                    logger.warning(f"[get_stock_tick] Tushare rt_min返回数据缺少列: {missing_cols}, 实际列={df.columns.tolist()}")
                    return pd.DataFrame()
                
                # 重命名列以统一格式
                df = df.rename(columns={
                    'vol': 'volume'
                })
                
                # 提取日期和时间（time格式为：YYYY-MM-DD HH:MM:SS）
                df['date'] = df['time'].str[:10]
                df['time'] = df['time'].str[11:19]
                
                # 筛选指定日期的数据
                target_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                df = df[df['date'] == target_date]
                
                if not df.empty:
                    df.to_csv(cache_file, index=False)
                    logger.debug(f"[get_stock_tick] 缓存分时数据: {code} {trade_date}, {len(df)}条")
                
                logger.debug(f"[get_stock_tick] Tushare rt_min成功获取 {len(df)} 条数据: {code}")
                return df
                
            except Exception as e:
                logger.error(f"[get_stock_tick] Tushare rt_min获取失败 {code}: {e}")
                # Tushare失败时，尝试使用AkShare作为备用
                logger.debug(f"[get_stock_tick] 尝试使用AkShare作为备用: {code}")
        else:
            logger.debug(f"[get_stock_tick] Tushare未初始化，使用AkShare: {code}")       
    
    def get_auction_data(self, ts_code: str, trade_date: str) -> Dict:
        """
        获取个股竞价数据（集合竞价）

        优先使用日线数据获取开盘价，更准确可靠

        Args:
            ts_code: 股票代码（已带后缀，如 002218.SZ）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            竞价数据字典，包含开盘价、竞价成交量等
        """
        cache_file = self.stock_dir / "auction" / f"{ts_code}_{trade_date}.json"
        
        # 检查缓存
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取竞价数据缓存失败: {e}")
        
        try:
            # 优先使用日线数据获取开盘价（更准确）
            daily_price = self.get_stock_daily_price(code, trade_date)
            if daily_price and daily_price.get('open', 0) > 0:
                open_price = daily_price['open']
                logger.debug(f"[get_auction_data] 从日线数据获取开盘价: {code} = {open_price}")
                
                result = {
                    '开盘价': float(open_price),
                    '竞价成交量': 0,  # 日线数据不包含竞价成交量
                    '竞价成交额': 0,
                    '价格趋势': []
                }
                
                # 缓存结果
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False)
                
                return result
            
            # 如果日线数据获取失败，尝试从分时数据中提取
            logger.debug(f"[get_auction_data] 日线数据获取失败，尝试分时数据: {code}")
            tick_df = self.get_stock_tick(code, trade_date)
            
            if tick_df.empty:
                logger.debug(f"[get_auction_data] 分时数据为空: {code}")
                return {}
            
            # 获取9:25的竞价数据
            auction_data = tick_df[tick_df['time'] == '09:25:00']
            
            if auction_data.empty:
                # 尝试获取9:30的数据作为开盘价
                first_tick = tick_df[tick_df['time'] >= '09:30:00'].iloc[0] if not tick_df.empty else None
                if first_tick is not None:
                    result = {
                        '开盘价': float(first_tick['open']),
                        '竞价成交量': 0,
                        '竞价成交额': 0,
                        '价格趋势': []
                    }
                    # 缓存结果
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False)
                    return result
                return {}
            
            auction_row = auction_data.iloc[0]
            
            # 计算价格趋势（9:15-9:25的价格变化）
            morning_ticks = tick_df[
                (tick_df['time'] >= '09:15:00') & 
                (tick_df['time'] <= '09:25:00')
            ]
            price_trend = morning_ticks['close'].tolist() if not morning_ticks.empty else []
            
            result = {
                '开盘价': float(auction_row['close']),
                '竞价成交量': float(auction_row['volume']),
                '竞价成交额': float(auction_row['amount']),
                '价格趋势': price_trend
            }
            
            # 缓存结果
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False)
            
            return result
            
        except Exception as e:
            logger.error(f"[get_auction_data] 获取竞价数据失败 {code} {trade_date}: {e}")
            return {}
   
    def _get_concepts_from_preloaded_data(self, ts_code: str, members_df: pd.DataFrame) -> str:
        """
        从预加载的概念成分股数据中获取个股所属概念

        Args:
            ts_code: 股票代码（已带后缀）
            members_df: 概念成分股DataFrame

        Returns:
            概念字符串，用逗号分隔
        """
        code = ts_code

        # 匹配股票代码
        matched = members_df[members_df['con_code'] == code]
        if matched.empty:
            # 尝试用纯数字代码匹配
            matched = members_df[members_df['con_code'].str.contains(code, na=False)]
        
        if matched.empty:
            return ''
        
        # 获取所有概念名称并去重
        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)
    
    def get_industry_sector_data(self, trade_date: str) -> pd.DataFrame:
        """
        获取东财行业板块数据（使用tushare dc_index接口）

        Args:
            trade_date: 交易日期，格式YYYYMMDD

        Returns:
            行业板块数据DataFrame，包含涨跌比、领涨强度等因子
        """
        if not self.ts_pro:
            return pd.DataFrame()

        cache_file = self.sector_dir / "industry" / f"{trade_date}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        if cache_file.exists():
            return pd.read_csv(cache_file)

        try:
            # 使用dc_index接口获取行业板块数据
            df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=trade_date)
            
            if df.empty:
                return df
            
            # 计算衍生因子
            # 1. 涨跌比 = 上涨家数 / (上涨家数 + 下跌家数)
            total_stocks = df['up_num'] + df['down_num']
            df['up_down_ratio'] = df.apply(
                lambda row: row['up_num'] / (row['up_num'] + row['down_num']) 
                if (row['up_num'] + row['down_num']) > 0 else 0, axis=1
            )
            
            # 2. 涨跌差 = 上涨家数 - 下跌家数
            df['up_down_diff'] = df['up_num'] - df['down_num']
            
            # 3. 领涨强度 = 领涨股涨幅 * 涨跌比（综合指标）
            df['leading_strength'] = df['leading_pct'] * df['up_down_ratio']
            
            # 4. 活跃度得分 = 换手率 * 涨跌比
            df['activity_score'] = df['turnover_rate'] * df['up_down_ratio']
            
            # 5. 综合强度 = 涨跌幅 * 0.3 + 涨跌比 * 30 + 领涨强度 * 0.2 + 活跃度 * 0.2
            df['composite_strength'] = (
                df['pct_change'] * 0.3 + 
                df['up_down_ratio'] * 30 + 
                df['leading_strength'] * 0.2 + 
                df['activity_score'] * 0.2
            )
            
            # 缓存数据
            df.to_csv(cache_file, index=False)
            logger.info(f"获取行业板块数据: {len(df)}个板块")
            
            return df
            
        except Exception as e:
            logger.error(f"获取行业板块数据失败: {e}")
            return pd.DataFrame()
    
    def get_sector_factors(self, industry_name: str = None, ts_code: str = None, trade_date: str = None) -> dict:
        """
        获取指定行业的因子数据
        
        Args:
            industry_name: 行业名称（如'火力发电'），推荐使用ts_code
            ts_code: 行业代码（如'BK1627.DC'），优先使用
            trade_date: 交易日期
        
        Returns:
            因子字典
        """
        df = self.get_industry_sector_data(trade_date)
        if df.empty:
            return {}
        
        # 优先使用行业代码精确匹配
        if ts_code:
            matched = df[df['ts_code'] == ts_code]
            if not matched.empty:
                row = matched.iloc[0]
                return {
                    'industry_ts_code': row['ts_code'],
                    'industry_name': row['name'],
                    'pct_change': row['pct_change'],
                    'up_num': row['up_num'],
                    'down_num': row['down_num'],
                    'up_down_ratio': row['up_down_ratio'],
                    'up_down_diff': row['up_down_diff'],
                    'leading': row['leading'],
                    'leading_pct': row['leading_pct'],
                    'leading_strength': row['leading_strength'],
                    'turnover_rate': row['turnover_rate'],
                    'activity_score': row['activity_score'],
                    'composite_strength': row['composite_strength'],
                    'total_mv': row['total_mv']
                }
        
        # 如果没有提供ts_code或精确匹配失败，使用行业名称精确匹配
        if industry_name:
            matched = df[df['name'] == industry_name]
            if not matched.empty:
                row = matched.iloc[0]
                return {
                    'industry_ts_code': row['ts_code'],
                    'industry_name': row['name'],
                    'pct_change': row['pct_change'],
                    'up_num': row['up_num'],
                    'down_num': row['down_num'],
                    'up_down_ratio': row['up_down_ratio'],
                    'up_down_diff': row['up_down_diff'],
                    'leading': row['leading'],
                    'leading_pct': row['leading_pct'],
                    'leading_strength': row['leading_strength'],
                    'turnover_rate': row['turnover_rate'],
                    'activity_score': row['activity_score'],
                    'composite_strength': row['composite_strength'],
                    'total_mv': row['total_mv']
                }
        
        return {}

    def get_sector_moneyflow(self, trade_date: str, sector_type: str = '行业') -> pd.DataFrame:
        """
        获取板块资金流向数据（使用Tushare的moneyflow_ind_dc接口）
        
        Args:
            trade_date: 交易日期，格式YYYYMMDD
            sector_type: 板块类型，'industry'(行业) 或 'concept'(概念)
        
        Returns:
            板块资金流向DataFrame，包含以下关键字段：
            - ts_code: 板块代码
            - name: 板块名称
            - pct_change: 板块涨跌幅
            - net_amount: 小单净流入（万元）
            - net_damount: 大单净流入（万元）
            - net_mamount: 中单净流入（万元）
            - buy_sm_amount: 小单买入金额（万元）
            - sell_sm_amount: 小单卖出金额（万元）
            - buy_md_amount: 中单买入金额（万元）
            - sell_md_amount: 中单卖出金额（万元）
            - buy_lg_amount: 大单买入金额（万元）
            - sell_lg_amount: 大单卖出金额（万元）
        """
        cache_file = self.sector_dir / "moneyflow" / f"{sector_type}_{trade_date}.csv"

        # 检查缓存
        if cache_file.exists():
            logger.info(f"从缓存加载板块资金流向数据: {cache_file}")
            return pd.read_csv(cache_file)

        if not self.ts_pro:
            logger.warning("Tushare未初始化，无法获取板块资金流向数据")
            return pd.DataFrame()

        try:
            logger.info(f"从Tushare获取板块资金流向数据: {trade_date}, 类型: {sector_type}")

            # 使用moneyflow_ind_dc接口获取板块资金流向
            # 该接口支持行业板块和概念板块
            df = self.ts_pro.moneyflow_ind_dc(trade_date=trade_date, type=sector_type)
            
            if not df.empty:
                # moneyflow_ind_dc接口直接返回净流入金额（元）
                # buy_sm_amount: 小单净流入, buy_md_amount: 中单净流入, buy_lg_amount: 大单净流入
                # 转换为万元
                df['net_amount'] = df.get('buy_sm_amount', 0) / 10000  # 小单净流入（万元）
                df['net_mamount'] = df.get('buy_md_amount', 0) / 10000  # 中单净流入（万元）
                df['net_damount'] = df.get('buy_lg_amount', 0) / 10000  # 大单净流入（万元）
                
                # 计算总净流入
                df['total_net_amount'] = df['net_amount'] + df['net_mamount'] + df['net_damount']
                
                # 缓存数据
                df.to_csv(cache_file, index=False)
                logger.info(f"获取板块资金流向数据成功: {len(df)}个板块，已缓存至 {cache_file}")
                return df
            else:
                logger.warning("无法获取板块资金流向数据")
                return pd.DataFrame()
                
        except Exception as e:
            logger.error(f"获取板块资金流向数据失败: {e}")
            return pd.DataFrame()
    
    def get_sector_capital_flow_type(self, sector_name: str = None, ts_code: str = None, trade_date: str = None) -> Dict:
        """
        分析板块资金流向类型
        
        Args:
            sector_name: 板块名称（推荐使用ts_code）
            ts_code: 板块代码（如'BK1627.DC'），优先使用
            trade_date: 交易日期
        
        Returns:
            资金流向分析结果字典
        """
        # 获取行业资金流向数据
        industry_df = self.get_sector_moneyflow(trade_date, 'industry')
        
        # 获取概念资金流向数据
        concept_df = self.get_sector_moneyflow(trade_date, 'concept')
        
        # 合并数据
        all_sectors = pd.concat([industry_df, concept_df], ignore_index=True)
        
        if all_sectors.empty:
            return {
                'capital_flow_type': 'UNKNOWN',
                'large_net': 0,
                'medium_net': 0,
                'small_net': 0,
                'total_net': 0,
                'description': '数据获取失败'
            }
        
        # 优先使用板块代码精确匹配
        matched = pd.DataFrame()
        if ts_code and 'ts_code' in all_sectors.columns:
            matched = all_sectors[all_sectors['ts_code'] == ts_code]
        
        # 如果没有提供ts_code或精确匹配失败，使用板块名称精确匹配
        if matched.empty and sector_name:
            matched = all_sectors[all_sectors['name'] == sector_name]
        
        # 如果精确匹配失败，尝试模糊匹配（作为备选）
        if matched.empty and sector_name:
            matched = all_sectors[all_sectors['name'].str.contains(sector_name, na=False, case=False)]
            if matched.empty:
                # 尝试反向匹配
                matched = all_sectors[all_sectors['name'].apply(lambda x: sector_name in str(x) if pd.notna(x) else False)]
        
        if matched.empty:
            return {
                'capital_flow_type': 'UNKNOWN',
                'large_net': 0,
                'medium_net': 0,
                'small_net': 0,
                'total_net': 0,
                'description': f'未找到板块[{sector_name or ts_code}]的资金流向数据'
            }
        
        # 取第一条匹配记录（最匹配的）
        sector_data = matched.iloc[0]
        
        # 获取资金流向数据
        large_net = sector_data.get('net_damount', 0)  # 大单净流入
        medium_net = sector_data.get('net_mamount', 0)  # 中单净流入
        small_net = sector_data.get('net_amount', 0)   # 小单净流入
        total_net = sector_data.get('total_net_amount', large_net + medium_net + small_net)
        
        # 判断资金流向类型
        if large_net > medium_net + small_net and large_net > 0:
            flow_type = 'INSTITUTION_LEADING'
            description = '机构主导'
        elif small_net > large_net + medium_net and small_net > 0:
            flow_type = 'RETAIL_LEADING'
            description = '散户主导'
        elif total_net > 0:
            flow_type = 'BALANCED'
            description = '均衡流入'
        elif total_net < 0:
            flow_type = 'NET_OUTFLOW'
            description = '净流出'
        else:
            flow_type = 'UNKNOWN'
            description = '数据不足'
        
        return {
            'capital_flow_type': flow_type,
            'large_net': large_net,
            'medium_net': medium_net,
            'small_net': small_net,
            'total_net': total_net,
            'description': description,
            'sector_name': sector_data.get('name', sector_name),
            'pct_change': sector_data.get('pct_change', 0)
        }

    def get_limit_step(self, trade_date: str, ts_code: str = None) -> pd.DataFrame:
        """
        获取连板天梯数据

        接口：limit_step
        描述：获取每天连板个数晋级的股票，可以分析出每天连续涨停进阶个数，判断强势热度
        限量：单次最大2000行数据，可根据股票代码或者日期循环提取全部
        积分：8000积分以上每分钟500次，每天总量不限制

        Args:
            trade_date: 交易日期（YYYYMMDD）
            ts_code: 股票代码（已带后缀，如 000001.SZ），可选，用于查询特定股票

        Returns:
            连板天梯数据DataFrame，包含字段：
            - ts_code: 股票代码
            - name: 股票名称
            - trade_date: 交易日期
            - limit_up_nums: 连板个数
            - up_stat: 涨停统计（如 3/5 表示5天3板）
            - cons_nums: 连续涨停天数
            - turnover_rate: 换手率
            - amount: 成交金额（万元）
            - float_mv: 流通市值（万元）
            - total_mv: 总市值（万元）
        """
        if not self.ts_pro:
            logger.warning("[get_limit_step] Tushare未初始化")
            return pd.DataFrame()

        # 缓存文件路径
        cache_file = self.market_dir / "limit_step" / f"{trade_date}.csv"
        if cache_file.exists():
            logger.debug(f"[get_limit_step] 从缓存加载 {trade_date} 连板天梯数据")
            return pd.read_csv(cache_file)

        try:
            # 构建请求参数
            params = {'trade_date': trade_date}
            if ts_code:
                params['ts_code'] = ts_code

            # 调用Tushare接口
            df = self.ts_pro.limit_step(**params)

            if df is not None and not df.empty:
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_step] 获取 {trade_date} 连板天梯数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_step] {trade_date} 返回空数据")

        except Exception as e:
            logger.error(f"[get_limit_step] 获取 {trade_date} 连板天梯数据异常: {e}")

        return pd.DataFrame()   
    
    def get_ths_index(self, index_type: str = None) -> pd.DataFrame:
        """
        获取同花顺板块指数列表

        接口：ths_index
        描述：获取同花顺板块指数，包括概念、行业、特色指数
        权限：需有6000积分，单次最大返回5000行数据，一次可提取全部数据，请勿循环提取

        Args:
            index_type: 指数类型，可选 '概念指数'/'行业指数'/'特色指数'，默认返回全部

        Returns:
            同花顺板块指数DataFrame，包含字段：
            - ts_code: 指数代码
            - name: 指数名称
            - type: 指数类型（概念/行业/特色）
            - market: 市场分类
        """
        if not self.ts_pro:
            logger.warning("[get_ths_index] Tushare未初始化")
            return pd.DataFrame()

        # 缓存文件路径（按类型缓存）
        type_suffix = f"_{index_type}" if index_type else "_all"
        cache_file = self.sector_dir / "ths_index" / f"index{type_suffix}.csv"

        # 缓存有效期为1天
        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 1:
                # logger.debug(f"[get_ths_index] 从缓存加载同花顺板块指数")
                return pd.read_csv(cache_file)

        try:
            # 调用Tushare接口
            df = self.ts_pro.ths_index(exchange='A', type=index_type).dropna()

            if df is not None and not df.empty:
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_ths_index] 获取同花顺板块指数: {len(df)}条，类型={index_type or '全部'}")
                return df
            else:
                logger.warning(f"[get_ths_index] 返回空数据")

        except Exception as e:
            logger.error(f"[get_ths_index] 获取同花顺板块指数异常: {e}")

        return pd.DataFrame()
    
    def get_ths_daily(self, ts_code: str = None, trade_date: str = None, 
                      start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取同花顺板块指数行情
        
        接口：ths_daily
        描述：获取同花顺板块指数行情数据
        限量：单次最大3000行数据（需6000积分），可根据指数代码、日期参数循环提取
        
        Args:
            ts_code: 指数代码（如'885001.TI'），可选
            trade_date: 交易日期（YYYYMMDD），可选，与start_date/end_date互斥
            start_date: 开始日期（YYYYMMDD），可选
            end_date: 结束日期（YYYYMMDD），可选
            
        Returns:
            同花顺板块指数行情DataFrame，包含字段：
            - ts_code: 指数代码
            - trade_date: 交易日期
            - close: 收盘价
            - open: 开盘价
            - high: 最高价
            - low: 最低价
            - pre_close: 昨收价
            - avg_price: 平均价
            - change: 涨跌额
            - pct_change: 涨跌幅
            - vol: 成交量（手）
            - amount: 成交额（千元）
        """
        if not self.ts_pro:
            logger.warning("[get_ths_daily] Tushare未初始化")
            return pd.DataFrame()

        # 参数校验
        if trade_date and (start_date or end_date):
            logger.warning("[get_ths_daily] trade_date与start_date/end_date互斥，优先使用trade_date")
            start_date = None
            end_date = None

        # 构建缓存文件名
        if trade_date:
            cache_key = f"{ts_code}_{trade_date}" if ts_code else f"all_{trade_date}"
        else:
            cache_key = f"{ts_code}_{start_date}_{end_date}" if ts_code else f"all_{start_date}_{end_date}"
        cache_file = self.sector_dir / "ths_daily" / f"{cache_key}.csv"
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        if cache_file.exists():
            # logger.debug(f"[get_ths_daily] 从缓存加载同花顺板块行情")
            return pd.read_csv(cache_file)

        try:
            # 构建请求参数
            params = {}
            if ts_code:
                params['ts_code'] = ts_code
            if trade_date:
                params['trade_date'] = trade_date
            if start_date:
                params['start_date'] = start_date
            if end_date:
                params['end_date'] = end_date

            # 调用Tushare接口
            df = self.ts_pro.ths_daily(**params)

            if df is not None and not df.empty:
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_ths_daily] 获取同花顺板块行情: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_ths_daily] 返回空数据")

        except Exception as e:
            logger.error(f"[get_ths_daily] 获取同花顺板块行情异常: {e}")

        return pd.DataFrame()
    
    def get_ths_member(self, ts_code: str) -> pd.DataFrame:
        """
        获取同花顺概念板块成分列表

        接口：ths_member
        描述：获取同花顺概念板块成分列表
        限量：用户积累6000积分可调取，每分钟可调取200次，可按概念板块代码循环提取所有成分

        Args:
            ts_code: 同花顺概念板块代码（如'885001.TI'）

        Returns:
            同花顺概念板块成分DataFrame，包含字段：
            - ts_code: 概念板块代码
            - code: 股票代码
            - name: 股票名称
        """
        if not self.ts_pro:
            logger.warning("[get_ths_member] Tushare未初始化")
            return pd.DataFrame()

        if not ts_code:
            logger.warning("[get_ths_member] 必须提供ts_code参数")
            return pd.DataFrame()

        # 缓存文件路径
        cache_file = self.sector_dir / "ths_member" / f"{ts_code}.csv"

        # 缓存有效期为7天（成分相对稳定）
        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 7:
                # logger.debug(f"[get_ths_member] 从缓存加载同花顺板块成分")
                return pd.read_csv(cache_file)

        try:
            # 调用Tushare接口
            df = self.ts_pro.ths_member(ts_code=ts_code)

            if df is not None and not df.empty:
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_ths_member] 获取同花顺板块{ts_code}成分: {len(df)}只股票")
                return df
            else:
                logger.warning(f"[get_ths_member] {ts_code} 返回空数据")

        except Exception as e:
            logger.error(f"[get_ths_member] 获取同花顺板块{ts_code}成分异常: {e}")

        return pd.DataFrame()

if __name__ == "__main__":
    # 测试
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    print("数据管理器初始化成功")
