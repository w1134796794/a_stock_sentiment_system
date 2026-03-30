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

logger = loguru.logger

class DataManager:
    def __init__(self, tushare_token: str, cache_dir: Path):
        self.ts_pro = ts.pro_api(tushare_token) if tushare_token != "your_tushare_token_here" else None
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.today_str = datetime.now().strftime("%Y%m%d")
        self.today_dir = self.cache_dir / self.today_str
        self.today_dir.mkdir(exist_ok=True)
        
    def get_trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        """获取交易日历"""
        cache_file = self.cache_dir / f"trade_cal_{start_date}_{end_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        if self.ts_pro:
            df = self.ts_pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date)
            df.to_csv(cache_file, index=False)
            return df
        return pd.DataFrame()
    
    def get_date_offset(self, date: str, offset: int) -> str:
        """获取偏移交易日"""
        # 简化处理，实际应该使用交易日历
        date_obj = datetime.strptime(date, "%Y%m%d")
        target = date_obj + timedelta(days=offset)
        return target.strftime("%Y%m%d")
    
    def _get_trade_calendar_df(self) -> pd.DataFrame:
        """
        获取交易日历DataFrame
        
        Returns:
            交易日历DataFrame
        """
        # 优先使用新的交易日历文件
        cal_file = self.cache_dir.parent / "trade_calendar.csv"
        if cal_file.exists():
            cal_df = pd.read_csv(cal_file)
            cal_df['cal_date'] = pd.to_datetime(cal_df['cal_date']).dt.strftime('%Y%m%d')
            return cal_df
        
        # 兼容旧版缓存文件
        cal_file = self.cache_dir / "trade_calendar_2025_2026.csv"
        if cal_file.exists():
            cal_df = pd.read_csv(cal_file)
            cal_df['cal_date'] = cal_df['cal_date'].astype(str)
            return cal_df
        
        return pd.DataFrame()
    
    def is_trade_date(self, date: str) -> bool:
        """
        判断是否为交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            
        Returns:
            是否为交易日
        """
        cal_df = self._get_trade_calendar_df()
        
        if cal_df.empty:
            logger.warning("交易日历为空，默认返回True")
            return True
        
        date_data = cal_df[cal_df['cal_date'] == date]
        
        if date_data.empty:
            logger.warning(f"日期{date}不在交易日历中，默认返回True")
            return True
        
        return date_data['is_open'].values[0] == 1
    
    def get_nearest_trade_date(self, date: str = None, direction: str = "backward") -> str:
        """
        获取最近的交易日
        direction: "backward" - 向前查找（默认），"forward" - 向后查找
        """
        if date is None:
            date = self.today_str
        
        cal_df = self._get_trade_calendar_df()
        
        if not cal_df.empty:
            # 将cal_date转换为datetime进行比较，并按日期排序
            cal_df_copy = cal_df.copy()
            cal_df_copy['cal_date_dt'] = pd.to_datetime(cal_df_copy['cal_date'], format='%Y%m%d')
            cal_df_copy = cal_df_copy.sort_values('cal_date_dt').reset_index(drop=True)
            target_date = pd.to_datetime(date, format='%Y%m%d')
            
            if direction == "backward":
                # 向前查找最近的交易日
                valid_dates = cal_df_copy[cal_df_copy['cal_date_dt'] <= target_date]
                if not valid_dates.empty:
                    trade_dates = valid_dates[valid_dates['is_open'] == 1]
                    if not trade_dates.empty:
                        return trade_dates.iloc[-1]['cal_date']
            else:
                # 向后查找
                valid_dates = cal_df_copy[cal_df_copy['cal_date_dt'] >= target_date]
                if not valid_dates.empty:
                    trade_dates = valid_dates[valid_dates['is_open'] == 1]
                    if not trade_dates.empty:
                        return trade_dates.iloc[0]['cal_date']
        
        # 备用方案：简化判断
        date_obj = datetime.strptime(date, "%Y%m%d")
        weekday = date_obj.weekday()
        if weekday >= 5:  # 周六或周日
            if direction == "backward":
                # 返回上周五
                days_back = weekday - 4
                nearest = date_obj - timedelta(days=days_back)
                return nearest.strftime("%Y%m%d")
            else:
                # 返回下周一
                days_forward = 7 - weekday
                nearest = date_obj + timedelta(days=days_forward)
                return nearest.strftime("%Y%m%d")
        
        return date
    
    def validate_trade_date(self, date: str) -> tuple:
        """
        验证并返回有效的交易日
        返回: (is_valid, actual_date, message)
        """
        date_obj = datetime.strptime(date, "%Y%m%d")
        weekday = date_obj.weekday()
        
        # 检查是否是周末
        if weekday >= 5:
            nearest = self.get_nearest_trade_date(date, "backward")
            return (False, nearest, f"{date}是周末，自动使用最近交易日{nearest}")
        
        # 尝试获取当日数据验证是否是交易日
        test_data = self.get_limit_up_pool(date)
        if test_data.empty:
            # 可能是非交易日，尝试向前查找
            nearest = self.get_nearest_trade_date(date, "backward")
            if nearest != date:
                return (False, nearest, f"{date}无交易数据，自动使用最近交易日{nearest}")
        
        return (True, date, f"{date}是有效交易日")
    
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
        """获取涨停池数据（AkShare，失败时回退到Tushare）"""
        cache_file = self.today_dir / f"zt_pool_{date}.csv"
        if cache_file.exists():
            df = pd.read_csv(cache_file)
            # 即使从缓存读取，也要确保追加到汇总文件
            self._append_to_limit_up_summary(df, date)
            return df

        # 首先尝试AkShare
        try:
            df = ak.stock_zt_pool_em(date=date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"从AkShare获取涨停池数据: {len(df)}条")
                self._append_to_limit_up_summary(df, date)
                return df
        except Exception as e:
            logger.warning(f"AkShare获取涨停池失败: {e}")

        # 如果AkShare失败，尝试Tushare
        if self.ts_pro:
            try:
                df = self._get_limit_up_from_tushare(date)
                if not df.empty:
                    # 转换列名以匹配AkShare格式
                    df = self._convert_tushare_to_akshare_format(df)
                    df.to_csv(cache_file, index=False)
                    logger.info(f"从Tushare获取涨停池数据: {len(df)}条")
                    self._append_to_limit_up_summary(df, date)
                    return df
            except Exception as e:
                logger.warning(f"Tushare获取涨停池失败: {e}")

        return pd.DataFrame()

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
                # 只保留涨停数据（涨跌幅>9%）
                df = df[df['pct_chg'] >= 9.0].copy()
                logger.info(f"Tushare limit_list_d获取到{len(df)}条涨停数据")
            return df
        except Exception as e:
            logger.warning(f"Tushare limit_list_d接口调用失败: {e}")
            return pd.DataFrame()

    def _convert_tushare_to_akshare_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        将Tushare limit_list_d数据格式转换为AkShare格式

        Args:
            df: Tushare格式的DataFrame

        Returns:
            AkShare格式的DataFrame
        """
        if df.empty:
            return df

        # 列名映射：Tushare -> AkShare
        column_mapping = {
            'ts_code': '代码',
            'name': '名称',
            'close': '最新价',
            'pct_chg': '涨跌幅',
            'amp': '振幅',
            'fc_amount': '封单额',
            'first_time': '首次封板时间',
            'last_time': '最后封板时间',
            'open_times': '炸板次数',
            'strth': '封单量',
            'industry': '所属行业'
        }

        # 选择并重命名列
        result_df = pd.DataFrame()
        for tushare_col, akshare_col in column_mapping.items():
            if tushare_col in df.columns:
                result_df[akshare_col] = df[tushare_col]

        # 添加缺失的列（默认值）
        if '连板数' not in result_df.columns:
            result_df['连板数'] = 1  # 默认为1板
        if '涨停统计' not in result_df.columns:
            result_df['涨停统计'] = '1/1'

        # 转换时间格式（Tushare是HHMMSS，AkShare也是HHMMSS）
        for col in ['首次封板时间', '最后封板时间']:
            if col in result_df.columns:
                result_df[col] = result_df[col].astype(str).str.replace(':', '')

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
            if not self.is_trade_date(trade_date):
                actual_trade_date = self.get_nearest_trade_date(trade_date, "backward")
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
                        logger.debug(f"汇总文件中已存在{trade_date}的涨停数据，跳过追加")
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
                                logger.info(f"{trade_date}的数据与最近交易日{latest_date}完全相同，判定为非交易日数据，跳过追加")
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
    
    def get_concept_industry(self) -> pd.DataFrame:
        """获取东方财富概念/行业数据"""
        cache_file = self.today_dir / "concept_industry.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 获取东方财富行业板块
            df = ak.stock_board_industry_name_em()
            df.to_csv(cache_file, index=False)
            return df
        except Exception as e:
            logger.error(f"获取行业数据失败: {e}")
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
        code = str(ts_code).strip()
        if '.' not in code:
            # 补齐6位
            code = code.zfill(6)
            # 根据代码前缀判断交易所
            if code.startswith('6'):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"

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
                    logger.debug(f"[get_stock_daily] 数据已缓存: {cache_file}")
                    return df
                else:
                    logger.debug(f"[get_stock_daily] Tushare返回空数据")
            else:
                logger.debug(f"[get_stock_daily] Tushare未初始化")
        except Exception as e:
            logger.error(f"[get_stock_daily] 获取个股{code}历史数据失败: {e}")
        return pd.DataFrame()
    
    def get_stock_5min_kline(self, code: str) -> pd.DataFrame:
        """获取5分钟K线（用于5日走势微图）"""
        # 使用AkShare获取近期5日分钟数据
        try:
            # 股票代码格式转换
            code = str(code)
            if code.startswith('6'):
                code = f"sh{code}"
            else:
                code = f"sz{code}"
            df = ak.stock_zh_a_hist_min_em(symbol=code, period="5", adjust="qfq")
            return df.tail(240)  # 约5个交易日（每天48个5分钟）
        except Exception as e:
            logger.error(f"获取5分钟线失败 {code}: {e}")
            return pd.DataFrame()
    
    def is_limit_up(self, pct_change: float) -> bool:
        """判断是否涨停（考虑创业板20%）"""
        # 主板10%，创业板科创板20%
        return pct_change >= 9.5 or (pct_change >= 19.5)
    
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
        code = str(stock_code).zfill(6)
        if not ('.' in code):
            # 根据代码前缀判断交易所
            if code.startswith('6'):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
        
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
        code = str(stock_code).strip()
        if '.' in code:
            code_short = code.split('.')[0]
        else:
            code_short = code.zfill(6)
            if code_short.startswith('6'):
                code = f"{code_short}.SH"
            else:
                code = f"{code_short}.SZ"
        
        # 匹配股票代码
        matched = members_df[members_df['con_code'] == code]
        if matched.empty:
            # 尝试用纯数字代码匹配
            matched = members_df[members_df['con_code'].str.contains(code_short, na=False)]
        
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
            trade_date = datetime.now().strftime("%Y%m%d")
        
        cache_file = self.today_dir / f"industry_sector_{trade_date}.csv"
        
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # 使用dc_index接口获取行业板块数据
            df = self.ts_pro.dc_index(idx_type='行业板块', trade_date=trade_date)
            
            # 如果当日数据为空，尝试获取上一交易日数据
            if df.empty:
                from datetime import timedelta
                prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
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
    
    def get_sector_factors(self, industry_name: str, trade_date: str = None) -> dict:
        """
        获取指定行业的因子数据
        
        Args:
            industry_name: 行业名称（如'火力发电'）
            trade_date: 交易日期
        
        Returns:
            因子字典
        """
        df = self.get_industry_sector_data(trade_date)
        if df.empty:
            return {}
        
        # 模糊匹配行业名称
        matched = df[df['name'].str.contains(industry_name, na=False, regex=False)]
        if matched.empty:
            return {}
        
        row = matched.iloc[0]
        return {
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
                # 尝试获取上一交易日数据
                from datetime import timedelta
                prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
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
        code = str(stock_code).strip()
        if '.' in code:
            # 提取纯数字代码用于匹配
            code_short = code.split('.')[0]
        else:
            code_short = code.zfill(6)
            # 添加后缀用于匹配
            if code_short.startswith('6'):
                code = f"{code_short}.SH"
            else:
                code = f"{code_short}.SZ"
        
        # 获取概念成分股数据
        members_df = self.get_concept_members(trade_date)
        if members_df.empty:
            return ''
        
        # 匹配股票代码（支持带后缀和不带后缀的匹配）
        matched = members_df[members_df['con_code'] == code]
        if matched.empty:
            # 尝试用纯数字代码匹配
            matched = members_df[members_df['con_code'].str.contains(code_short, na=False)]
        
        if matched.empty:
            return ''
        
        # 获取所有概念名称并去重
        concepts = matched['concept_name'].dropna().unique()
        return ','.join(concepts)

if __name__ == "__main__":
    # 测试
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    print("数据管理器初始化成功")
