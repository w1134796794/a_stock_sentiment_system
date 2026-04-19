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
    def __init__(self, tushare_token: str, cache_dir: Path):
        self.ts_pro = ts.pro_api(tushare_token) if tushare_token != "your_tushare_token_here" else None
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.today_str = datetime.now().strftime("%Y%m%d")
        self.today_dir = self.cache_dir / self.today_str
        self.today_dir.mkdir(exist_ok=True)
        
        # 初始化工具类（单例模式）
        self.date_utils = DateUtils()
        self.stock_code_utils = StockCodeUtils()
        self.time_utils = TimeUtils()
        self.calculation_utils = CalculationUtils()
        self.validation_utils = ValidationUtils()
        
    def get_daily_basic(self, trade_date: str) -> pd.DataFrame:
        """获取每日行情基础数据（带缓存）"""
        cache_file = self.today_dir / f"daily_basic_{trade_date}.csv"
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
        
        # 备用：使用AkShare
        try:
            df = ak.stock_zh_a_spot_em()
            df.to_csv(cache_file, index=False)
            return df
        except Exception as e:
            logger.error(f"AkShare获取失败: {e}")
            return pd.DataFrame()
    
    def get_limit_up_pool(self, date: str) -> pd.DataFrame:
        """获取涨停池数据（优先使用Tushare，包含板上成交金额和行业代码）"""
        # 检查是否为交易日，如果不是则获取最近的交易日
        actual_date = date
        if not self.date_utils.is_trade_date(date):
            actual_date = self.date_utils.get_nearest_trade_date(date)
            if actual_date != date:
                logger.debug(f"[get_limit_up_pool] {date} 非交易日，使用最近交易日 {actual_date}")
                date = actual_date

        # 使用基于查询日期的缓存目录，确保历史数据可以被复用
        query_date_dir = self.cache_dir / date
        query_date_dir.mkdir(exist_ok=True)
        cache_file = query_date_dir / f"zt_pool_{date}.csv"
        if cache_file.exists():
            df = pd.read_csv(cache_file)
            # 检查是否有行业代码列，如果没有需要重新获取
            if '行业代码' not in df.columns:
                logger.info(f"缓存数据缺少行业代码，重新获取 {date} 的涨停数据")
            else:
                # 即使从缓存读取，也要确保追加到汇总文件
                self._append_to_limit_up_summary(df, actual_date)
                return df

        # 优先使用Tushare（包含板上成交金额等更详细数据）
        if self.ts_pro:
            try:
                df = self._get_limit_up_from_tushare(date)
                if not df.empty:
                    # 标准化格式以兼容现有代码，同时保留Tushare特有字段，添加行业代码
                    df = self._normalize_limit_up_format(df, date)
                    df.to_csv(cache_file, index=False)
                    logger.info(f"从Tushare获取涨停池数据: {len(df)}条")
                    self._append_to_limit_up_summary(df, actual_date)
                    return df
            except Exception as e:
                logger.warning(f"Tushare获取涨停池失败: {e}")

    def _get_limit_up_from_tushare(self, date: str) -> pd.DataFrame:
        """
        使用Tushare limit_list_d接口获取涨停数据

        Args:
            date: 日期，格式YYYYMMDD

        Returns:
            涨停数据DataFrame
        """
        if not self.ts_pro:
            return pd.DataFrame()

        try:
            # limit_list_d接口获取涨跌停数据
            df = self.ts_pro.limit_list_d(trade_date=date)
            if not df.empty:
                # 只保留涨停数据（limit='U'表示涨停）
                df = df[df['limit'] == 'U'].copy()
                logger.info(f"Tushare limit_list_d获取到{len(df)}条涨停数据")
            return df
        except Exception as e:
            logger.warning(f"Tushare limit_list_d接口调用失败: {e}")
            return pd.DataFrame()

    def _normalize_limit_up_format(self, df: pd.DataFrame, trade_date: str = None) -> pd.DataFrame:
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
        
        # 处理时间格式（去掉冒号）
        for col in ['首次封板时间', '最后封板时间']:
            if col in result_df.columns:
                result_df[col] = result_df[col].astype(str).str.replace(':', '')
        
        # 确保代码格式统一（6位数字）
        if '代码' in result_df.columns:
            result_df['代码'] = result_df['代码'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
        
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
            # 0. 首先使用交易日历判断是否为交易日
            if not self.date_utils.is_trade_date(trade_date):
                actual_trade_date = self.date_utils.get_nearest_trade_date(trade_date)
                logger.info(f"{trade_date}不是交易日，实际对应交易日为{actual_trade_date}，跳过追加")
                return
            
            # 汇总文件路径
            summary_file = self.cache_dir / "all_limit_up_stocks.csv"
            
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
    
    def get_dc_industry_list(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取东财行业板块列表（使用dc_index接口，包含行业代码）
        
        Args:
            trade_date: 交易日期，格式YYYYMMDD，默认使用当前日期
            
        Returns:
            行业板块DataFrame，包含行业代码(ts_code)和名称(name)
        """
        if not self.ts_pro:
            logger.warning("Tushare未初始化，无法获取东财行业板块")
            return pd.DataFrame()
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y%m%d")
        
        cache_file = self.cache_dir / f"dc_industry_list_{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 使用dc_index接口获取行业板块数据
            df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=trade_date)
            
            # 如果当日数据为空，尝试获取上一交易日数据
            if df.empty:
                prev_date = self.date_utils.get_nearest_trade_date(trade_date)
                if prev_date != trade_date:
                    logger.warning(f"{trade_date}行业板块数据为空，尝试获取上一交易日{prev_date}数据")
                    df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=prev_date)
                    if not df.empty:
                        trade_date = prev_date
                        cache_file = self.cache_dir / f"dc_industry_list_{trade_date}.csv"
            
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
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y%m%d")
        
        cache_file = self.cache_dir / f"dc_industry_cons_{ts_code}_{trade_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 使用dc_member接口获取行业成分（不是dc_index_cons）
            df = self.ts_pro.dc_member(ts_code=ts_code, trade_date=trade_date)
            
            # 如果当日数据为空，尝试获取上一交易日数据
            if df.empty:
                prev_date = self.date_utils.get_nearest_trade_date(trade_date)
                if prev_date != trade_date:
                    logger.warning(f"{trade_date}行业成分数据为空，尝试获取上一交易日{prev_date}数据")
                    df = self.ts_pro.dc_member(ts_code=ts_code, trade_date=prev_date)
                    if not df.empty:
                        cache_file = self.cache_dir / f"dc_industry_cons_{ts_code}_{prev_date}.csv"
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"获取东财行业{ts_code}成分: {len(df)}只股票")
            return df
        except Exception as e:
            logger.warning(f"获取东财行业成分失败: {e}")
            return pd.DataFrame()
    
    def find_industry_by_stock(self, stock_code: str, trade_date: str = None) -> pd.DataFrame:
        """
        根据股票代码查找所属行业（使用dc_index接口）
        
        Args:
            stock_code: 股票代码（如'000001.SZ'或'000001'）
            trade_date: 交易日期
            
        Returns:
            该股票所属的行业列表
        """
        if not self.ts_pro:
            logger.warning("Tushare未初始化")
            return pd.DataFrame()
        
        # 标准化股票代码
        stock_code = self.stock_code_utils.standardize_code(stock_code)
        
        # 获取所有行业列表
        industry_df = self.get_dc_industry_list(trade_date)
        if industry_df.empty:
            return pd.DataFrame()
        
        # 遍历所有行业，查找包含该股票的行业
        result = []
        for _, row in industry_df.iterrows():
            ts_code = row['ts_code']
            cons_df = self.get_dc_industry_cons(ts_code, trade_date)
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
    
    def get_industry_cons(self, industry: str) -> pd.DataFrame:
        """获取行业成分股"""
        cache_key = f"industry_cons_{industry}"
        cache_file = self.today_dir / f"{cache_key}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            df = ak.stock_board_industry_cons_em(symbol=industry)
            df.to_csv(cache_file, index=False)
            return df
        except Exception as e:
            logger.warning(f"获取行业{industry}成分股失败: {e}")
            return pd.DataFrame()
    
    def get_stock_daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取个股历史日线数据"""
        # 确保代码格式正确（添加后缀）
        code = self.stock_code_utils.standardize_code(ts_code)

        logger.debug(f"[get_stock_daily] 获取 {code} 从 {start_date} 到 {end_date} 的日线数据")

        cache_file = self.cache_dir / f"{code}_{start_date}_{end_date}.csv"
        if cache_file.exists():
            logger.debug(f"[get_stock_daily] 从缓存加载: {cache_file}")
            return pd.read_csv(cache_file, parse_dates=['trade_date'])

        try:
            if self.ts_pro:
                logger.debug(f"[get_stock_daily] 调用Tushare daily接口: ts_code={code}")
                df = self.ts_pro.daily(ts_code=code, start_date=start_date, end_date=end_date)
                logger.debug(f"[get_stock_daily] Tushare返回 {len(df)} 条数据")
                if not df.empty:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    df.to_csv(cache_file, index=False)
                    logger.debug(f"[get_stock_daily] 从Tushare获取并缓存数据: {cache_file}")
                    return df
                else:
                    logger.debug(f"[get_stock_daily] Tushare返回空数据")
            else:
                logger.debug(f"[get_stock_daily] Tushare未初始化")
        except Exception as e:
            logger.error(f"[get_stock_daily] 获取个股{code}历史数据失败: {e}")
        return pd.DataFrame()

    def get_all_rt_k_data(self, trade_date: str = None) -> pd.DataFrame:
        """
        批量获取所有股票的实时日线数据（rt_k接口）
        
        rt_k接口不传入ts_code时返回所有股票的实时数据，每分钟只能调用一次。
        此方法将结果缓存到文件，后续需要实时日线数据时直接从缓存读取。
        
        Args:
            trade_date: 交易日期（YYYYMMDD），默认使用当前日期
            
        Returns:
            pd.DataFrame: 包含所有股票实时数据的DataFrame，失败返回空DataFrame
            列包括: ts_code, trade_date, open, high, low, close, pre_close, change, pct_change, vol, amount
        """
        if trade_date is None:
            trade_date = self.today_str
            
        # 检查是否为交易日
        if not self.date_utils.is_trade_date(trade_date):
            actual_date = self.date_utils.get_nearest_trade_date(trade_date)
            if actual_date != trade_date:
                logger.debug(f"[get_all_rt_k_data] {trade_date} 非交易日，使用最近交易日 {actual_date}")
                trade_date = actual_date
        
        # 缓存文件路径
        cache_file = self.cache_dir / f"rt_k_all_{trade_date}.csv"
        
        # 检查缓存是否存在且有效（缓存文件在当天交易时间内生成）
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                if not df.empty:
                    logger.info(f"[get_all_rt_k_data] 从缓存加载所有股票实时数据: {len(df)}条")
                    return df
            except Exception as e:
                logger.warning(f"[get_all_rt_k_data] 读取缓存失败: {e}")
        
        # 判断当前时间是否在交易时间内（9:30-17:00）
        now = datetime.now()
        current_time = now.strftime("%H%M")
        
        if not ("0930" <= current_time <= "1700"):
            logger.debug(f"[get_all_rt_k_data] 当前时间 {current_time} 不在交易时间内，跳过获取实时数据")
            return pd.DataFrame()
        
        if not self.ts_pro:
            logger.warning("[get_all_rt_k_data] Tushare未初始化")
            return pd.DataFrame()
        
        try:
            logger.info(f"[get_all_rt_k_data] 调用rt_k接口获取所有股票实时数据（不传入ts_code）")
            # 不传入ts_code，获取所有股票的实时数据
            df = self.ts_pro.rt_k()
            
            if df is None or df.empty:
                logger.warning("[get_all_rt_k_data] rt_k返回空数据")
                return pd.DataFrame()
            
            # 确保ts_code格式统一（6位数字）
            if 'ts_code' in df.columns:
                df['code'] = df['ts_code'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
            
            # 缓存结果
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_all_rt_k_data] 成功获取并缓存所有股票实时数据: {len(df)}条")
            
            return df
            
        except Exception as e:
            logger.error(f"[get_all_rt_k_data] 获取所有股票实时数据失败: {e}")
            return pd.DataFrame()
    
    def get_stock_daily_price(self, ts_code: str, trade_date: str) -> Dict:
        """
        获取个股某日的开盘价、收盘价、昨收价

        根据时间判断使用接口:
        - 交易日盘中(9:30-17:00): 优先从批量缓存的rt_k数据中获取，如果没有则使用 daily 接口
        - 17点之后: 使用 daily 历史接口

        Args:
            ts_code: 股票代码（如 002218.SZ 或 002218）
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            Dict: 包含 open, close, pre_close 的字典，获取失败返回空字典
        """
        from datetime import datetime

        # 检查是否为交易日，如果不是则获取最近的交易日
        if not self.date_utils.is_trade_date(trade_date):
            actual_date = self.date_utils.get_nearest_trade_date(trade_date)
            if actual_date != trade_date:
                logger.debug(f"[get_stock_daily_price] {trade_date} 非交易日，使用最近交易日 {actual_date}")
                trade_date = actual_date

        # 确保代码格式正确
        code = self.stock_code_utils.standardize_code(ts_code)

        # 检查缓存
        cache_file = self.cache_dir / f"daily_price_{code}_{trade_date}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取日线价格缓存失败: {e}")

        try:
            if not self.ts_pro:
                logger.debug(f"[get_stock_daily_price] Tushare未初始化")
                return {}

            # 判断当前时间
            now = datetime.now()
            current_time = now.strftime("%H%M")

            # 交易日盘中(9:30-17:00)优先从批量缓存的rt_k数据中获取
            if "0930" <= current_time <= "1700":
                # 尝试从批量缓存的rt_k数据中获取
                rt_k_cache_file = self.cache_dir / f"rt_k_all_{trade_date}.csv"
                if rt_k_cache_file.exists():
                    try:
                        rt_k_df = pd.read_csv(rt_k_cache_file)
                        # 查找指定股票的数据
                        stock_data = rt_k_df[rt_k_df['code'] == code_6digit]
                        if not stock_data.empty:
                            row = stock_data.iloc[0]
                            result = {
                                'open': float(row.get('open', 0)),
                                'close': float(row.get('close', 0)),
                                'pre_close': float(row.get('pre_close', 0))
                            }
                            logger.debug(f"[get_stock_daily_price] 从批量rt_k缓存获取 {code}: {result}")

                            # 缓存结果
                            with open(cache_file, 'w', encoding='utf-8') as f:
                                json.dump(result, f)
                            return result
                        else:
                            logger.debug(f"[get_stock_daily_price] 批量rt_k缓存中未找到 {code}，尝试使用 daily 接口")
                    except Exception as e:
                        logger.debug(f"[get_stock_daily_price] 读取批量rt_k缓存失败: {e}")
                else:
                    logger.debug(f"[get_stock_daily_price] 批量rt_k缓存不存在，尝试使用 daily 接口")

            # 17点之后或 rt_k 缓存中没有，使用 daily 接口
            logger.debug(f"[get_stock_daily_price] 使用 daily 接口: {code}, {trade_date}")
            df = self.ts_pro.daily(ts_code=code, start_date=trade_date, end_date=trade_date)
            
            if df is not None and not df.empty:
                row = df.iloc[0]
                result = {
                    'open': float(row.get('open', 0)),
                    'close': float(row.get('close', 0)),
                    'pre_close': float(row.get('pre_close', 0))
                }
                logger.debug(f"[get_stock_daily_price] daily 返回: {result}")
                
                # 缓存结果
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f)
                return result
            else:
                logger.warning(f"[get_stock_daily_price] daily 返回空数据")
                
        except Exception as e:
            logger.error(f"[get_stock_daily_price] 获取 {code} {trade_date} 日线数据失败: {e}")
        
        return {}
  
    def get_stock_tick(self, code: str, trade_date: str) -> pd.DataFrame:
        """
        获取个股分时数据（1分钟线）
        
        使用Tushare rt_min接口获取实时分钟数据，比AkShare更稳定
        
        Args:
            code: 股票代码（如 002218）
            trade_date: 交易日期（YYYYMMDD）
            
        Returns:
            分时数据DataFrame，包含time, price, volume等列
        """
        cache_file = self.cache_dir / f"tick_{code}_{trade_date}.csv"
        
        # 检查缓存
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file)
                logger.debug(f"从缓存加载分时数据: {code} {trade_date}")
                return df
            except Exception as e:
                logger.warning(f"读取缓存分时数据失败: {e}")
        
        # 优先使用Tushare rt_min接口
        if self.ts_pro:
            try:
                # 确保代码格式正确（添加后缀）
                ts_code = self.stock_code_utils.standardize_code(code)
                
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
        
    
    def get_auction_data(self, code: str, trade_date: str) -> Dict:
        """
        获取个股竞价数据（集合竞价）
        
        优先使用日线数据获取开盘价，更准确可靠
        
        Args:
            code: 股票代码（如 002218）
            trade_date: 交易日期（YYYYMMDD）
            
        Returns:
            竞价数据字典，包含开盘价、竞价成交量等
        """
        cache_file = self.cache_dir / f"auction_{code}_{trade_date}.json"
        
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
    
    def get_stock_concepts(self, stock_code: str) -> str:
        """
        获取个股所属概念（使用tushare kpl_concept_cons接口）
        
        Args:
            stock_code: 股票代码（如 002218.SZ）
        
        Returns:
            概念字符串，用逗号分隔
        """
        if not self.ts_pro:
            return ''
        
        # 转换代码格式为tushare格式（添加后缀）
        code = self.stock_code_utils.standardize_code(stock_code)
        
        cache_file = self.today_dir / f"stock_concepts_{code.replace('.', '_')}.csv"
        
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
    
    def enrich_core_stocks_concepts(self, core_stocks_df: pd.DataFrame, trade_date: str = None) -> pd.DataFrame:
        """
        为核心标的DataFrame添加概念数据（使用dc_member接口获取）
        
        Args:
            core_stocks_df: 核心标的DataFrame，包含'Code'或'代码'列
            trade_date: 交易日期，格式YYYYMMDD，默认使用当前日期
        
        Returns:
            添加了概念列的DataFrame
        """
        if core_stocks_df.empty:
            return core_stocks_df
        
        # 确定代码列名
        code_col = 'Code' if 'Code' in core_stocks_df.columns else ('代码' if '代码' in core_stocks_df.columns else None)
        if not code_col:
            logger.warning("DataFrame中没有找到代码列(Code或代码)")
            return core_stocks_df
        
        logger.info(f"正在获取{len(core_stocks_df)}只核心标的的概念数据...")
        
        # 预加载概念成分股数据（避免重复调用API）
        members_df = self.get_concept_members(trade_date)
        if members_df.empty:
            logger.warning("概念成分股数据为空，尝试使用kpl_concept_cons接口")
            # 回退到旧方法
            concepts_list = []
            for _, row in core_stocks_df.iterrows():
                code = row[code_col]
                concept = self.get_stock_concepts(code)
                concepts_list.append(concept)
            core_stocks_df['Concept'] = concepts_list
            return core_stocks_df
        
        logger.info(f"使用概念成分股数据为核心标的匹配概念...")
        
        concepts_list = []
        for _, row in core_stocks_df.iterrows():
            code = row[code_col]
            concept = self._get_concepts_from_preloaded_data(code, members_df)
            concepts_list.append(concept)
        
        core_stocks_df['Concept'] = concepts_list
        logger.info(f"概念数据获取完成")
        
        return core_stocks_df
    
    def _get_concepts_from_preloaded_data(self, stock_code: str, members_df: pd.DataFrame) -> str:
        """
        从预加载的概念成分股数据中获取个股所属概念
        
        Args:
            stock_code: 股票代码
            members_df: 概念成分股DataFrame
        
        Returns:
            概念字符串，用逗号分隔
        """
        # 标准化股票代码
        code = self.stock_code_utils.standardize_code(stock_code)
        
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
    
    def get_industry_sector_data(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取东财行业板块数据（使用tushare dc_index接口）
        
        Args:
            trade_date: 交易日期，格式YYYYMMDD，默认使用当前日期
        
        Returns:
            行业板块数据DataFrame，包含涨跌比、领涨强度等因子
        """
        if not self.ts_pro:
            return pd.DataFrame()
        
        if not trade_date:
            trade_date = self.date_utils.get_nearest_trade_date(self.date_utils.get_today_str())
        
        cache_file = self.today_dir / f"industry_sector_{trade_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 使用dc_index接口获取行业板块数据
            df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=trade_date)
            
            # 如果当日数据为空，尝试获取上一交易日数据
            if df.empty:
                prev_date = self.date_utils.get_prev_trade_date(trade_date)
                if prev_date and prev_date != trade_date:
                    logger.warning(f"{trade_date}行业板块数据为空，尝试获取上一交易日{prev_date}数据")
                    df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=prev_date)
                    if not df.empty:
                        trade_date = prev_date
                        cache_file = self.today_dir / f"industry_sector_{trade_date}.csv"
            
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
    
    def get_limit_down_pool(self, date: str) -> pd.DataFrame:
        """
        获取跌停池数据
        
        使用Tushare limit_list_d接口获取跌停数据
        
        Args:
            date: 日期，格式YYYYMMDD
            
        Returns:
            跌停数据DataFrame
        """
        # 检查是否为交易日
        actual_date = date
        if not self.date_utils.is_trade_date(date):
            actual_date = self.date_utils.get_nearest_trade_date(date)
            if actual_date != date:
                logger.debug(f"[get_limit_down_pool] {date} 非交易日，使用最近交易日 {actual_date}")
                date = actual_date
        
        # 缓存文件路径
        query_date_dir = self.cache_dir / date
        query_date_dir.mkdir(exist_ok=True)
        cache_file = query_date_dir / f"dt_pool_{date}.csv"
        
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
            
            # 标准化格式
            df['代码'] = df['ts_code'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
            df['名称'] = df['name']
            df['涨跌幅'] = df['pct_chg']
            df['最新价'] = df['close']
            
            # 缓存结果
            df.to_csv(cache_file, index=False)
            logger.info(f"[get_limit_down_pool] 获取 {date} 跌停池数据: {len(df)}条")
            
            return df
            
        except Exception as e:
            logger.error(f"[get_limit_down_pool] 获取 {date} 跌停池数据失败: {e}")
            return pd.DataFrame()
    
    def get_concept_members(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取所有概念板块的成分股数据（使用tushare dc_member接口）
        
        优化：使用固定缓存目录，避免每日重复调用API
        
        Args:
            trade_date: 交易日期，格式YYYYMMDD，默认使用当前日期
        
        Returns:
            概念成分股DataFrame，包含股票代码和所属概念
        """
        if not self.ts_pro:
            return pd.DataFrame()
        
        if not trade_date:
            trade_date = datetime.now().strftime("%Y%m%d")
        
        # 使用固定缓存目录，而不是today_dir
        concept_cache_dir = self.cache_dir / "concept_members"
        concept_cache_dir.mkdir(exist_ok=True)
        
        cache_file = concept_cache_dir / f"concept_members_{trade_date}.csv"
        cache_meta_file = concept_cache_dir / f"concept_members_{trade_date}.meta"
        
        # 检查缓存是否存在（文件名已包含日期，直接按日期区分）
        if cache_file.exists():
            logger.info(f"使用缓存的概念成分股数据: {cache_file}")
            return pd.read_csv(cache_file)
        
        try:
            # 1. 获取所有概念板块
            concepts_df = self.ts_pro.dc_index(idx_type='概念板块', trade_date=trade_date)
            
            if concepts_df.empty:
                # 尝试获取上一交易日数据（使用交易日历）
                prev_date = self.date_utils.get_prev_trade_date(trade_date)
                if prev_date and prev_date != trade_date:
                    logger.warning(f"{trade_date}概念板块数据为空，尝试获取上一交易日{prev_date}数据")
                    concepts_df = self.ts_pro.dc_index(idx_type='概念板块', trade_date=prev_date)
                    if not concepts_df.empty:
                        trade_date = prev_date
                        cache_file = concept_cache_dir / f"concept_members_{trade_date}.csv"
            
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
            
            # 4. 缓存数据（包括meta文件记录缓存时间）
            result_df.to_csv(cache_file, index=False)
            # 创建meta文件记录缓存时间
            cache_meta_file.touch()
            logger.info(f"获取概念成分股完成: {len(result_df)}条记录，涉及{len(concepts_df)}个概念，已缓存至 {cache_file}")
            
            return result_df
            
        except Exception as e:
            logger.error(f"获取概念成分股数据失败: {e}")
            return pd.DataFrame()
    
    def get_stock_concepts_from_members(self, stock_code: str, trade_date: str = None) -> str:
        """
        从概念成分股数据中获取个股所属概念
        
        Args:
            stock_code: 股票代码（如 002218.SZ 或 002218）
            trade_date: 交易日期
        
        Returns:
            概念字符串，用逗号分隔
        """
        # 标准化股票代码
        code = self.stock_code_utils.standardize_code(stock_code)
        
        # 获取概念成分股数据
        members_df = self.get_concept_members(trade_date)
        if members_df.empty:
            return ''
        
        # 匹配股票代码（支持带后缀和不带后缀的匹配）
        matched = members_df[members_df['con_code'] == code]
        if matched.empty:
            # 尝试用纯数字代码匹配
            matched = members_df[members_df['con_code'].str.contains(code, na=False)]
        
        if matched.empty:
            return ''
        
        # 获取所有概念名称并去重
        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)

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
        cache_file = self.today_dir / f"sector_moneyflow_{sector_type}_{trade_date}.csv"
        
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

            if df.empty:
                logger.warning(f"{trade_date}板块资金流向数据为空")
                # 尝试获取上一交易日数据
                prev_date = self.date_utils.get_prev_trade_date(trade_date)
                if prev_date and prev_date != trade_date:
                    logger.info(f"尝试获取上一交易日{prev_date}数据")
                    df = self.ts_pro.moneyflow_ind_dc(trade_date=prev_date, type=sector_type)
                    if not df.empty:
                        trade_date = prev_date
                        cache_file = self.today_dir / f"sector_moneyflow_{sector_type}_{trade_date}.csv"
            
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

    def get_stock_daily_data(self, symbol: str, trade_date: str) -> Dict:
        """
        获取股票日行情数据（使用tushare daily接口）

        Args:
            symbol: 股票代码（如 000001 或 000001.SZ）
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
        # 检查是否为交易日，如果不是则获取最近的交易日
        if not self.date_utils.is_trade_date(trade_date):
            actual_date = self.date_utils.get_nearest_trade_date(trade_date)
            if actual_date != trade_date:
                logger.debug(f"[get_stock_daily_data] {trade_date} 非交易日，使用最近交易日 {actual_date}")
                trade_date = actual_date

        # 标准化代码格式
        code = self.stock_code_utils.standardize_code(symbol)

        # 检查缓存
        cache_file = self.cache_dir / f"daily_data_{code}_{trade_date}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    logger.debug(f"[get_stock_daily_data] 从缓存获取 {code} {trade_date} 日行情数据")
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[get_stock_daily_data] 读取缓存失败: {e}")

        if not self.ts_pro:
            logger.warning("[get_stock_daily_data] Tushare未初始化")
            return {}

        try:
            df = self.ts_pro.daily(ts_code=code, start_date=trade_date, end_date=trade_date)
            if df is not None and not df.empty:
                row = df.iloc[0]
                result = {
                    'ts_code': row.get('ts_code', code),
                    'trade_date': trade_date,
                    'open': float(row.get('open', 0)),
                    'high': float(row.get('high', 0)),
                    'low': float(row.get('low', 0)),
                    'close': float(row.get('close', 0)),
                    'pre_close': float(row.get('pre_close', 0)),
                    'change': float(row.get('change', 0)),
                    'pct_chg': float(row.get('pct_chg', 0)),
                    'vol': float(row.get('vol', 0)),  # 成交量（手）
                    'amount': float(row.get('amount', 0))  # 成交额（千元）
                }

                # 缓存结果
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f)

                logger.debug(f"[get_stock_daily_data] 获取 {code} {trade_date} 日行情: 成交量={result['vol']}手, 成交额={result['amount']}千元")
                return result
            else:
                logger.warning(f"[get_stock_daily_data] {code} {trade_date} 返回空数据")
        except Exception as e:
            logger.error(f"[get_stock_daily_data] 获取 {code} {trade_date} 日行情异常: {e}")

        return {}

    def get_stock_daily_basic(self, symbol: str, trade_date: str) -> Dict:
        """
        获取股票每日基本面指标（使用tushare daily_basic接口）
        包含实际换手率、流通股本等关键指标

        Args:
            symbol: 股票代码（如 000001 或 000001.SZ）
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
        # 检查是否为交易日，如果不是则获取最近的交易日
        if not self.date_utils.is_trade_date(trade_date):
            actual_date = self.date_utils.get_nearest_trade_date(trade_date)
            if actual_date != trade_date:
                logger.debug(f"[get_stock_daily_basic] {trade_date} 非交易日，使用最近交易日 {actual_date}")
                trade_date = actual_date

        # 标准化代码格式
        code = self.stock_code_utils.standardize_code(symbol)

        # 检查缓存
        cache_file = self.cache_dir / f"daily_basic_{code}_{trade_date}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    logger.debug(f"[get_stock_daily_basic] 从缓存获取 {code} {trade_date} 基本面数据")
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[get_stock_daily_basic] 读取缓存失败: {e}")

        if not self.ts_pro:
            logger.warning("[get_stock_daily_basic] Tushare未初始化")
            return {}

        try:
            df = self.ts_pro.daily_basic(ts_code=code, trade_date=trade_date)
            if df is not None and not df.empty:
                row = df.iloc[0]
                result = {
                    'ts_code': row.get('ts_code', code),
                    'trade_date': trade_date,
                    'close': float(row.get('close', 0)),
                    'turnover_rate': float(row.get('turnover_rate', 0)),  # 换手率（%）
                    'turnover_rate_f': float(row.get('turnover_rate_f', 0)),  # 实际换手率（%）
                    'float_share': float(row.get('float_share', 0)),  # 流通股本（万股）
                    'free_share': float(row.get('free_share', 0)),  # 自由流通股本（万股）
                    'total_share': float(row.get('total_share', 0)),  # 总股本（万股）
                    'circ_mv': float(row.get('circ_mv', 0)),  # 流通市值（万元）
                    'total_mv': float(row.get('total_mv', 0)),  # 总市值（万元）
                }

                # 缓存结果
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(result, f)

                logger.debug(f"[get_stock_daily_basic] 获取 {code} {trade_date} 基本面数据: "
                            f"实际换手率={result['turnover_rate_f']:.2f}%, "
                            f"流通股本={result['float_share']:.2f}万股")
                return result
            else:
                logger.warning(f"[get_stock_daily_basic] {code} {trade_date} 返回空数据")
        except Exception as e:
            logger.error(f"[get_stock_daily_basic] 获取 {code} {trade_date} 基本面数据异常: {e}")

        return {}

    def get_limit_step(self, trade_date: str = None, ts_code: str = None) -> pd.DataFrame:
        """
        获取连板天梯数据
        
        接口：limit_step
        描述：获取每天连板个数晋级的股票，可以分析出每天连续涨停进阶个数，判断强势热度
        限量：单次最大2000行数据，可根据股票代码或者日期循环提取全部
        积分：8000积分以上每分钟500次，每天总量不限制
        
        Args:
            trade_date: 交易日期（YYYYMMDD），默认使用最近交易日
            ts_code: 股票代码（如 000001.SZ），可选，用于查询特定股票
            
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
        
        # 使用最近交易日
        if not trade_date:
            trade_date = self.date_utils.get_nearest_trade_date(self.today_str)
        
        # 确保是交易日
        if not self.date_utils.is_trade_date(trade_date):
            trade_date = self.date_utils.get_nearest_trade_date(trade_date)
        
        # 缓存文件路径
        cache_file = self.cache_dir / f"limit_step_{trade_date}.csv"
        if cache_file.exists():
            logger.debug(f"[get_limit_step] 从缓存加载 {trade_date} 连板天梯数据")
            return pd.read_csv(cache_file)
        
        try:
            # 构建请求参数
            params = {'trade_date': trade_date}
            if ts_code:
                params['ts_code'] = self.stock_code_utils.standardize_code(ts_code)
            
            # 调用Tushare接口
            df = self.ts_pro.limit_step(**params)
            
            if df is not None and not df.empty:
                # 标准化代码格式
                if 'ts_code' in df.columns:
                    df['code'] = df['ts_code'].astype(str).str.replace(r'\.SH|\.SZ|\.BJ', '', regex=True).str.zfill(6)
                
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_step] 获取 {trade_date} 连板天梯数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_step] {trade_date} 返回空数据")
                
        except Exception as e:
            logger.error(f"[get_limit_step] 获取 {trade_date} 连板天梯数据异常: {e}")
        
        return pd.DataFrame()
    
    def get_limit_cpt_list(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取最强板块统计数据
        
        接口：limit_cpt_list
        描述：获取每天涨停股票最多最强的概念板块，可以分析强势板块的轮动，判断资金动向
        限量：单次最大2000行数据，可根据股票代码或者日期循环提取全部
        积分：8000积分以上每分钟500次，每天总量不限制
        
        Args:
            trade_date: 交易日期（YYYYMMDD），默认使用最近交易日
            
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
        
        # 使用最近交易日
        if not trade_date:
            trade_date = self.date_utils.get_nearest_trade_date(self.today_str)
        
        # 确保是交易日
        if not self.date_utils.is_trade_date(trade_date):
            trade_date = self.date_utils.get_nearest_trade_date(trade_date)
        
        # 缓存文件路径
        cache_file = self.cache_dir / f"limit_cpt_list_{trade_date}.csv"
        if cache_file.exists():
            logger.debug(f"[get_limit_cpt_list] 从缓存加载 {trade_date} 最强板块数据")
            return pd.read_csv(cache_file)
        
        try:
            # 调用Tushare接口
            df = self.ts_pro.limit_cpt_list(trade_date=trade_date)
            
            if df is not None and not df.empty:
                # 缓存结果
                df.to_csv(cache_file, index=False)
                logger.info(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据: {len(df)}条")
                return df
            else:
                logger.warning(f"[get_limit_cpt_list] {trade_date} 返回空数据")
                
        except Exception as e:
            logger.error(f"[get_limit_cpt_list] 获取 {trade_date} 最强板块数据异常: {e}")
        
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
        cache_file = self.cache_dir / f"ths_index{type_suffix}.csv"
        
        # 缓存有效期为1天
        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 1:
                logger.debug(f"[get_ths_index] 从缓存加载同花顺板块指数")
                return pd.read_csv(cache_file)
        
        try:
            # 构建请求参数
            params = {}
            if index_type:
                params['type'] = index_type
            
            # 调用Tushare接口
            df = self.ts_pro.ths_index(**params)
            
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
        cache_file = self.cache_dir / f"ths_daily_{cache_key}.csv"
        
        if cache_file.exists():
            logger.debug(f"[get_ths_daily] 从缓存加载同花顺板块行情")
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
        cache_file = self.cache_dir / f"ths_member_{ts_code}.csv"
        
        # 缓存有效期为7天（成分相对稳定）
        if cache_file.exists():
            file_time = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if (datetime.now() - file_time).days < 7:
                logger.debug(f"[get_ths_member] 从缓存加载同花顺板块成分")
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
