"""
交易日管理器 - 统一处理交易日相关逻辑
提供交易日历管理、日期计算、交易日验证等功能
"""
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple
import loguru

logger = loguru.logger


class TradeDateManager:
    """交易日管理器 - 单例模式"""
    _instance = None
    _cal_df: Optional[pd.DataFrame] = None
    _cal_file_path: Optional[Path] = None
    
    def __new__(cls, cache_dir: Path = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, cache_dir: Path = None):
        if cache_dir is not None:
            self._cache_dir = Path(cache_dir)
        elif self._cal_file_path is not None:
            self._cache_dir = self._cal_file_path.parent
        else:
            # 默认缓存目录
            self._cache_dir = Path(__file__).parent.parent / "data" / "cache"
        
        # 延迟加载交易日历
        if self._cal_df is None:
            self._load_trade_calendar()
    
    def _load_trade_calendar(self) -> None:
        """加载交易日历"""
        # 尝试多个可能的文件路径
        possible_paths = [
            self._cache_dir.parent / "trade_calendar.csv",  # data/trade_calendar.csv
            self._cache_dir / "trade_calendar_2025_2026.csv",  # cache/trade_calendar_2025_2026.csv
            self._cache_dir / "trade_calendar.csv",  # cache/trade_calendar.csv
        ]
        
        for cal_file in possible_paths:
            if cal_file.exists():
                try:
                    cal_df = pd.read_csv(cal_file)
                    # 统一日期格式
                    if 'cal_date' in cal_df.columns:
                        cal_df['cal_date'] = pd.to_datetime(cal_df['cal_date']).dt.strftime('%Y%m%d')
                    self._cal_df = cal_df
                    self._cal_file_path = cal_file
                    logger.debug(f"交易日历加载成功: {cal_file}")
                    return
                except Exception as e:
                    logger.warning(f"加载交易日历失败 {cal_file}: {e}")
                    continue
        
        logger.warning("未找到交易日历文件，将使用简化判断（仅考虑周末）")
        self._cal_df = pd.DataFrame()
    
    def reload_calendar(self, cache_dir: Path = None) -> None:
        """重新加载交易日历"""
        if cache_dir is not None:
            self._cache_dir = Path(cache_dir)
        self._cal_df = None
        self._load_trade_calendar()
    
    def is_trade_date(self, date: str) -> bool:
        """
        判断是否为交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            
        Returns:
            是否为交易日
        """
        if self._cal_df is not None and not self._cal_df.empty:
            date_data = self._cal_df[self._cal_df['cal_date'] == date]
            if not date_data.empty:
                return date_data['is_open'].values[0] == 1
            logger.warning(f"日期{date}不在交易日历中，使用简化判断")
        
        # 简化判断：检查是否是周末
        date_obj = datetime.strptime(date, "%Y%m%d")
        return date_obj.weekday() < 5  # 周一到周五为交易日
    
    def get_nearest_trade_date(self, date: str, direction: str = "backward") -> str:
        """
        获取最近的交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            direction: "backward" - 向前查找（默认），"forward" - 向后查找
            
        Returns:
            最近的交易日字符串
        """
        if self._cal_df is not None and not self._cal_df.empty:
            try:
                cal_df_copy = self._cal_df.copy()
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
            except Exception as e:
                logger.warning(f"使用交易日历查找失败: {e}，使用简化判断")
        
        # 简化判断
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
    
    def get_prev_trade_date(self, date: str, n: int = 1) -> str:
        """
        获取前N个交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            n: 向前回溯的交易日数量，默认1
            
        Returns:
            前N个交易日字符串
        """
        current = date
        for _ in range(n):
            current = self.get_nearest_trade_date(current, direction="backward")
            # 如果返回的日期和当前日期相同，说明已经是最前的日期了
            if current == date:
                break
            date = current
        return current
    
    def get_next_trade_date(self, date: str, n: int = 1) -> str:
        """
        获取后N个交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            n: 向后查找的交易日数量，默认1
            
        Returns:
            后N个交易日字符串
        """
        current = date
        for _ in range(n):
            # 先往后推一天，再找最近的交易日
            date_obj = datetime.strptime(current, "%Y%m%d") + timedelta(days=1)
            next_day = date_obj.strftime("%Y%m%d")
            current = self.get_nearest_trade_date(next_day, direction="forward")
            if current == date:
                break
            date = current
        return current
    
    def get_trade_dates_between(self, start_date: str, end_date: str) -> List[str]:
        """
        获取两个日期之间的所有交易日
        
        Args:
            start_date: 开始日期，格式YYYYMMDD
            end_date: 结束日期，格式YYYYMMDD
            
        Returns:
            交易日列表
        """
        if self._cal_df is not None and not self._cal_df.empty:
            try:
                mask = (self._cal_df['cal_date'] >= start_date) & \
                       (self._cal_df['cal_date'] <= end_date) & \
                       (self._cal_df['is_open'] == 1)
                return self._cal_df[mask]['cal_date'].tolist()
            except Exception as e:
                logger.warning(f"使用交易日历获取区间失败: {e}")
        
        # 简化实现
        trade_dates = []
        current = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        
        while current <= end:
            date_str = current.strftime("%Y%m%d")
            if self.is_trade_date(date_str):
                trade_dates.append(date_str)
            current += timedelta(days=1)
        
        return trade_dates
    
    def validate_trade_date(self, date: str) -> Tuple[bool, str, str]:
        """
        验证并返回有效的交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            
        Returns:
            (is_valid, actual_date, message)
        """
        if self.is_trade_date(date):
            return (True, date, f"{date}是有效交易日")
        
        nearest = self.get_nearest_trade_date(date, "backward")
        return (False, nearest, f"{date}非交易日，自动使用最近交易日{nearest}")
    
    def get_date_offset(self, date: str, offset: int) -> str:
        """
        获取偏移交易日
        
        Args:
            date: 日期字符串，格式YYYYMMDD
            offset: 偏移天数（正数向后，负数向前）
            
        Returns:
            偏移后的交易日字符串
        """
        if offset > 0:
            return self.get_next_trade_date(date, offset)
        elif offset < 0:
            return self.get_prev_trade_date(date, abs(offset))
        return date


# 全局实例，方便直接导入使用
trade_date_manager: Optional[TradeDateManager] = None


def get_trade_date_manager(cache_dir: Path = None) -> TradeDateManager:
    """获取交易日管理器实例（单例）"""
    global trade_date_manager
    if trade_date_manager is None:
        trade_date_manager = TradeDateManager(cache_dir)
    return trade_date_manager


def init_trade_date_manager(cache_dir: Path) -> TradeDateManager:
    """初始化交易日管理器（指定缓存目录）"""
    global trade_date_manager
    trade_date_manager = TradeDateManager(cache_dir)
    return trade_date_manager


# 便捷函数，直接通过模块调用
def is_trade_date(date: str) -> bool:
    """判断是否为交易日"""
    return get_trade_date_manager().is_trade_date(date)


def get_nearest_trade_date(date: str, direction: str = "backward") -> str:
    """获取最近的交易日"""
    return get_trade_date_manager().get_nearest_trade_date(date, direction)


def get_prev_trade_date(date: str, n: int = 1) -> str:
    """获取前N个交易日"""
    return get_trade_date_manager().get_prev_trade_date(date, n)


def get_next_trade_date(date: str, n: int = 1) -> str:
    """获取后N个交易日"""
    return get_trade_date_manager().get_next_trade_date(date, n)


def validate_trade_date(date: str) -> Tuple[bool, str, str]:
    """验证并返回有效的交易日"""
    return get_trade_date_manager().validate_trade_date(date)
