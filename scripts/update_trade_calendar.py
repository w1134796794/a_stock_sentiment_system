"""
交易日历更新脚本

使用tushare的trade_cal接口获取交易日历并保存到data目录
供系统中判断交易日使用

使用方法:
    python scripts/update_trade_calendar.py
    
会自动获取2025-2026年的交易日历并保存到 data/trade_calendar.csv
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import tushare as ts
from datetime import datetime
from config.settings import TUSHARE_TOKEN, CACHE_DIR
import loguru

logger = loguru.logger


def update_trade_calendar(start_year: int = 2025, end_year: int = 2026) -> bool:
    """
    更新交易日历
    
    Args:
        start_year: 开始年份
        end_year: 结束年份
        
    Returns:
        是否成功
    """
    try:
        # 初始化tushare
        ts.set_token(TUSHARE_TOKEN)
        pro = ts.pro_api()
        
        # 获取交易日历
        start_date = f"{start_year}0101"
        end_date = f"{end_year}1231"
        
        logger.info(f"正在获取交易日历: {start_date} - {end_date}")
        
        df = pro.trade_cal(
            exchange='SSE',
            start_date=start_date,
            end_date=end_date
        )
        
        if df.empty:
            logger.error("获取交易日历失败：返回数据为空")
            return False
        
        # 转换日期格式
        df['cal_date'] = pd.to_datetime(df['cal_date'], format='%Y%m%d')
        
        # 保存到data目录
        calendar_file = Path(CACHE_DIR).parent / "trade_calendar.csv"
        df.to_csv(calendar_file, index=False)
        
        # 统计信息
        trade_days = df[df['is_open'] == 1]
        logger.info(f"交易日历更新成功: {len(df)}天，其中交易日{len(trade_days)}天")
        logger.info(f"保存路径: {calendar_file}")
        
        return True
        
    except Exception as e:
        logger.error(f"更新交易日历失败: {e}")
        return False


def get_trade_calendar() -> pd.DataFrame:
    """
    获取本地交易日历
    
    Returns:
        交易日历DataFrame
    """
    calendar_file = Path(CACHE_DIR).parent / "trade_calendar.csv"
    
    if not calendar_file.exists():
        logger.warning("交易日历文件不存在，尝试更新...")
        if update_trade_calendar():
            return pd.read_csv(calendar_file)
        else:
            return pd.DataFrame()
    
    return pd.read_csv(calendar_file)


def is_trade_date(date_str: str) -> bool:
    """
    判断是否为交易日
    
    Args:
        date_str: 日期字符串，格式YYYYMMDD
        
    Returns:
        是否为交易日
    """
    df = get_trade_calendar()
    
    if df.empty:
        logger.warning("交易日历为空，默认返回True")
        return True
    
    # 转换日期格式
    df['cal_date'] = pd.to_datetime(df['cal_date']).dt.strftime('%Y%m%d')
    
    # 查找日期
    date_data = df[df['cal_date'] == date_str]
    
    if date_data.empty:
        logger.warning(f"日期{date_str}不在交易日历中，默认返回True")
        return True
    
    return date_data['is_open'].values[0] == 1


def get_last_trade_date(date_str: str = None) -> str:
    """
    获取最近一个交易日
    
    Args:
        date_str: 日期字符串，格式YYYYMMDD，默认使用今天
        
    Returns:
        最近交易日字符串，格式YYYYMMDD
    """
    df = get_trade_calendar()
    
    if df.empty:
        logger.warning("交易日历为空")
        return date_str or datetime.now().strftime('%Y%m%d')
    
    # 转换日期格式
    df['cal_date'] = pd.to_datetime(df['cal_date'])
    df = df[df['is_open'] == 1].sort_values('cal_date')
    
    if date_str:
        target_date = pd.to_datetime(date_str)
    else:
        target_date = pd.to_datetime(datetime.now().strftime('%Y%m%d'))
    
    # 找到小于等于目标日期的最近交易日
    past_trades = df[df['cal_date'] <= target_date]
    
    if past_trades.empty:
        logger.warning(f"未找到{date_str}之前的交易日")
        return date_str or datetime.now().strftime('%Y%m%d')
    
    return past_trades['cal_date'].iloc[-1].strftime('%Y%m%d')


def get_next_trade_date(date_str: str = None) -> str:
    """
    获取下一个交易日
    
    Args:
        date_str: 日期字符串，格式YYYYMMDD，默认使用今天
        
    Returns:
        下一个交易日字符串，格式YYYYMMDD
    """
    df = get_trade_calendar()
    
    if df.empty:
        logger.warning("交易日历为空")
        return date_str or datetime.now().strftime('%Y%m%d')
    
    # 转换日期格式
    df['cal_date'] = pd.to_datetime(df['cal_date'])
    df = df[df['is_open'] == 1].sort_values('cal_date')
    
    if date_str:
        target_date = pd.to_datetime(date_str)
    else:
        target_date = pd.to_datetime(datetime.now().strftime('%Y%m%d'))
    
    # 找到大于目标日期的下一个交易日
    future_trades = df[df['cal_date'] > target_date]
    
    if future_trades.empty:
        logger.warning(f"未找到{date_str}之后的交易日")
        return date_str or datetime.now().strftime('%Y%m%d')
    
    return future_trades['cal_date'].iloc[0].strftime('%Y%m%d')


if __name__ == "__main__":
    # 更新交易日历
    update_trade_calendar()
    
    # 测试功能
    print("\n=== 交易日历功能测试 ===")
    
    today = datetime.now().strftime('%Y%m%d')
    print(f"\n今天: {today}")
    print(f"是否为交易日: {is_trade_date(today)}")
    print(f"最近交易日: {get_last_trade_date(today)}")
    print(f"下一个交易日: {get_next_trade_date(today)}")
    
    # 测试周末
    print(f"\n20260328(周六): {is_trade_date('20260328')}")
    print(f"20260329(周日): {is_trade_date('20260329')}")
    print(f"20260327(周五): {is_trade_date('20260327')}")
