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
                    logger.debug(f"[get_stock_daily] 从Tushare获取并缓存数据: {cache_file}")
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
        获取个股某日的开盘价、收盘价、昨收价
        
        根据时间判断使用接口:
        - 交易日盘中(9:30-17:00): 使用 rt_k 实时接口
        - 17点之后: 使用 daily 历史接口
        
        Args:
            ts_code: 股票代码（如 002218.SZ 或 002218）
            trade_date: 交易日期（YYYYMMDD）
            
        Returns:
            Dict: 包含 open, close, pre_close 的字典，获取失败返回空字典
        """
        from datetime import datetime
        
        # 确保代码格式正确
        code = str(ts_code).strip()
        if '.' not in code:
            code = code.zfill(6)
            if code.startswith('6'):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
        
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
            
            # 交易日盘中(9:30-17:00)使用 rt_k 接口
            if "0930" <= current_time <= "1700":
                logger.debug(f"[get_stock_daily_price] 盘中时间，使用 rt_k 接口: {code}")
                df = self.ts_pro.rt_k(ts_code=code)
                if df is not None and not df.empty:
                    # rt_k 返回的列名可能不同，需要适配
                    # rt_k 通常返回: ts_code, trade_date, open, high, low, close, pre_close, change, pct_change, vol, amount
                    result = {
                        'open': float(df.iloc[0].get('open', 0)),
                        'close': float(df.iloc[0].get('close', 0)),
                        'pre_close': float(df.iloc[0].get('pre_close', 0))
                    }
                    logger.debug(f"[get_stock_daily_price] rt_k 返回: {result}")
                    
                    # 缓存结果
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(result, f)
                    return result
                else:
                    logger.warning(f"[get_stock_daily_price] rt_k 返回空数据，尝试使用 daily 接口")
            
            # 17点之后或 rt_k 失败，使用 daily 接口
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
                code_str = str(code).zfill(6)
                if '.' not in code_str:
                    if code_str.startswith('6'):
                        ts_code = f"{code_str}.SH"
                    else:
                        ts_code = f"{code_str}.SZ"
                else:
                    ts_code = code_str
                
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
        
        # 备用：使用AkShare获取分钟数据
        try:
            code_str = str(code).zfill(6)
            if code_str.startswith('6'):
                symbol = f"sh{code_str}"
            else:
                symbol = f"sz{code_str}"
            
            logger.debug(f"[get_stock_tick] 使用AkShare获取分钟数据: {symbol}")
            
            df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="qfq")
            
            if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                logger.warning(f"[get_stock_tick] AkShare返回空数据: {code}")
                return pd.DataFrame()
            
            # 检查必要的列是否存在
            if '时间' not in df.columns:
                logger.warning(f"[get_stock_tick] AkShare数据缺少'时间'列: {code}, 列={df.columns.tolist()}")
                return pd.DataFrame()
            
            # 重命名列以统一格式
            df = df.rename(columns={
                '时间': 'time',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
                '均价': 'vwap'
            })
            
            # 提取日期和时间
            df['date'] = df['time'].str[:10]
            df['time'] = df['time'].str[11:19]
            
            # 筛选指定日期的数据
            target_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
            df = df[df['date'] == target_date]
            
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.debug(f"[get_stock_tick] 缓存分时数据: {code} {trade_date}, {len(df)}条")
            
            return df
            
        except Exception as e:
            logger.error(f"[get_stock_tick] AkShare获取失败 {code} {trade_date}: {e}")
            return pd.DataFrame()
    
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

    def get_sector_moneyflow(self, trade_date: str, sector_type: str = 'industry') -> pd.DataFrame:
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
                from datetime import timedelta
                prev_date = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
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
    
    def get_sector_capital_flow_type(self, sector_name: str, trade_date: str) -> Dict:
        """
        分析板块资金流向类型
        
        Args:
            sector_name: 板块名称
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
        
        # 查找匹配的板块（支持模糊匹配）
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
                'description': f'未找到板块[{sector_name}]的资金流向数据'
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

if __name__ == "__main__":
    # 测试
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    print("数据管理器初始化成功")
