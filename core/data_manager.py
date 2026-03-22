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
    
    def get_nearest_trade_date(self, date: str = None, direction: str = "backward") -> str:
        """
        获取最近的交易日
        direction: "backward" - 向前查找（默认），"forward" - 向后查找
        """
        if date is None:
            date = self.today_str
        
        date_obj = datetime.strptime(date, "%Y%m%d")
        
        # 尝试从缓存获取交易日历
        cal_file = self.cache_dir / "trade_calendar_2025_2026.csv"
        if cal_file.exists():
            cal_df = pd.read_csv(cal_file)
            cal_df['cal_date'] = cal_df['cal_date'].astype(str)
            
            if direction == "backward":
                # 向前查找最近的交易日
                valid_dates = cal_df[cal_df['cal_date'] <= date]
                if not valid_dates.empty:
                    trade_dates = valid_dates[valid_dates['is_open'] == 1]
                    if not trade_dates.empty:
                        return trade_dates.iloc[-1]['cal_date']
            else:
                # 向后查找
                valid_dates = cal_df[cal_df['cal_date'] >= date]
                if not valid_dates.empty:
                    trade_dates = valid_dates[valid_dates['is_open'] == 1]
                    if not trade_dates.empty:
                        return trade_dates.iloc[0]['cal_date']
        
        # 简化判断：如果今天是周末，返回上周五
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
        """获取涨停池数据（AkShare）"""
        cache_file = self.today_dir / f"zt_pool_{date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file)
        
        try:
            # AkShare涨停池
            df = ak.stock_zt_pool_em(date=date)
            if not df.empty:
                df.to_csv(cache_file, index=False)
                logger.info(f"获取涨停池数据: {len(df)}条")
                return df
        except Exception as e:
            logger.error(f"获取涨停池失败: {e}")
        return pd.DataFrame()
    
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
        cache_file = self.cache_dir / f"{ts_code}_{start_date}_{end_date}.csv"
        if cache_file.exists():
            return pd.read_csv(cache_file, parse_dates=['trade_date'])
        
        try:
            if self.ts_pro:
                df = self.ts_pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if not df.empty:
                    df['trade_date'] = pd.to_datetime(df['trade_date'])
                    df.to_csv(cache_file, index=False)
                    return df
        except Exception as e:
            logger.error(f"获取个股{ts_code}历史数据失败: {e}")
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

if __name__ == "__main__":
    # 测试
    from config.settings import TUSHARE_TOKEN, CACHE_DIR
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    print("数据管理器初始化成功")
