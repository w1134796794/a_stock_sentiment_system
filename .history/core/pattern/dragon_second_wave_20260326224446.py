"""
龙二波策略 - 正确的历史连板判断
核心：从每日涨停池取近期连板，非日线涨幅计算
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
import loguru

logger = loguru.logger


class PatternType(Enum):
    DRAGON_SECOND_WAVE = "龙二波"


@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str
    reason: str
    key_metrics: Dict
    validation_rules: List[str]


class DragonSecondWaveStrategyV2:
    def __init__(self, data_manager, sentiment_engine):
        self.dm = data_manager
        self.se = sentiment_engine
        
        # 时间参数（近期记忆）
        self.params = {
            "recent_days": 15,           # 只取近15天内的行情（记忆未散）
            "max_adjust_days": 10,       # 调整期最多10天
            "min_first_wave": 4,         # 第一波至少4板
            "max_first_wave": 12,        # 第一波最多12板（太多则记忆透支）
        }
    
    def detect_second_wave(self,
                          stock_code: str,
                          stock_name: str,
                          today_str: str,
                          recent_zt_pools: Dict[str, pd.DataFrame],  # 近15日每日涨停池
                          today_data: pd.Series,
                          sector_hot: bool) -> Optional[TradeSignal]:
        """
        检测龙二波机会
        recent_zt_pools: {日期: 当日涨停池DataFrame}
        """
        
        # ========== 步骤1：从涨停池重建近期连板记录（关键！）==========
        consecutive_record = self._rebuild_consecutive_from_pools(
            stock_code, recent_zt_pools
        )
        
        if not consecutive_record['is_valid']:
            return None
        
        first_wave_info = consecutive_record['first_wave']
        
        # 检查是否是近期这一波（非历史久远）
        days_since_peak = self._calculate_days_since_peak(
            first_wave_info['peak_date'], today_str
        )
        
        if days_since_peak > self.params["max_adjust_days"] + 5:
            logger.debug(f"{stock_name} 第一波距今{days_since_peak}天，记忆已散")
            return None
        
        # ========== 步骤2：判断第一波高度（真龙标准）==========
        if not (self.params["min_first_wave"] <= first_wave_info['max_boards'] <= self.params["max_first_wave"]):
            return None
        
        # ========== 步骤3：检查调整期形态 ==========
        adjust_period = self._get_adjust_period(
            stock_code, first_wave_info['peak_date'], today_str
        )
        
        if not self._check_adjust_quality(adjust_period):
            return None
        
        # ========== 步骤4：今日启动确认 ==========
        if today_data.get('涨跌幅', 0) < 9.5:  # 今日未涨停
            return None
        
        # 检查今日是否在涨停池（确认真实涨停，非单纯涨幅）
        today_pool = recent_zt_pools.get(today_str, pd.DataFrame())
        if stock_code not in today_pool['代码'].values:
            return None  # 虽然涨幅>9.5%，但可能不是涨停（如科创板20%）
        
        # ========== 构建信号 ==========
        return TradeSignal(
            pattern_type=PatternType.DRAGON_SECOND_WAVE,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=today_data.get('首次封板时间', ''),
            confidence=0.82,
            entry_price=today_data.get('涨停价', 0),
            stop_loss=adjust_period['ma10'] * 0.97,
            take_profit=today_data.get('涨停价', 0) * 1.15,
            position_size="medium",
            reason=f"近期{first_wave_info['max_boards']}板龙头，调整{days_since_peak}天后二波启动",
            key_metrics={
                "第一波高度": first_wave_info['max_boards'],
                "第一波日期": f"{first_wave_info['start_date']}至{first_wave_info['peak_date']}",
                "调整天数": days_since_peak,
                "调整深度": f"{adjust_period['depth']*100:.1f}%",
                "支撑均线": f"MA10:{adjust_period['ma10']:.2f}",
                "地量缩比": f"{adjust_period['shrink_ratio']*100:.1f}%"
            },
            validation_rules=[
                f"近15日内{first_wave_info['max_boards']}连板（真龙）",
                f"调整{days_since_peak}天（记忆未散）",
                "回踩MA10获支撑",
                "地量后放量首板",
                "板块热度未退" if sector_hot else "板块已冷（风险）"
            ]
        )
    
    # ==================== 核心方法：从涨停池重建连板记录 ====================
    
    def _rebuild_consecutive_from_pools(self, 
                                       stock_code: str,
                                       recent_pools: Dict[str, pd.DataFrame]) -> Dict:
        """
        从近15日涨停池重建该股的连板记录
        返回：是否是近期龙头，第一波信息等
        """
        dates = sorted(recent_pools.keys())
        zt_dates = []  # 该股涨停的日期列表
        
        for date in dates:
            pool = recent_pools[date]
            if pool.empty:
                continue
            
            if stock_code in pool['代码'].values:
                zt_dates.append(date)
        
        if len(zt_dates) < self.params["min_first_wave"]:
            return {'is_valid': False, 'reason': '连板数不足'}
        
        # 计算连续涨停（允许断板1天）
        consecutive_groups = []
        current_group = [zt_dates[0]]
        
        for i in range(1, len(zt_dates)):
            prev_date = datetime.strptime(zt_dates[i-1], "%Y%m%d")
            curr_date = datetime.strptime(zt_dates[i], "%Y%m%d")
            gap = (curr_date - prev_date).days
            
            if gap <= 2:  # 间隔1-2天算连续（允许断板1天）
                current_group.append(zt_dates[i])
            else:
                consecutive_groups.append(current_group)
                current_group = [zt_dates[i]]
        
        consecutive_groups.append(current_group)
        
        # 找最大连板组
        max_group = max(consecutive_groups, key=len)
        max_boards = len(max_group)
        
        if max_boards < self.params["min_first_wave"]:
            return {'is_valid': False, 'reason': '最大连板数不足'}
        
        # 检查是否是近期这一波（非开头几天）
        peak_date = max_group[-1]
        first_date = max_group[0]
        
        # 距离今天不能太久
        today = datetime.strptime(dates[-1], "%Y%m%d")
        peak = datetime.strptime(peak_date, "%Y%m%d")
        if (today - peak).days > self.params["max_adjust_days"] + 5:
            return {'is_valid': False, 'reason': '第一波距今太久'}
        
        return {
            'is_valid': True,
            'first_wave': {
                'max_boards': max_boards,
                'start_date': first_date,
                'peak_date': peak_date,
                'zt_dates': max_group
            },
            'all_zt_dates': zt_dates
        }
    
    def _get_adjust_period(self, stock_code: str, 
                          peak_date: str, today: str) -> Dict:
        """
        获取调整期数据（peak_date到today之间）
        """
        # 从data_manager获取日线数据
        hist = self.dm.get_stock_daily(stock_code, peak_date, today)
        
        if hist.empty or len(hist) < 3:
            return {}
        
        peak_price = hist.iloc[0]['high']  # 第一波最高价
        latest = hist.iloc[-1]
        lowest = hist['low'].min()
        
        # 计算调整深度
        depth = (peak_price - lowest) / peak_price
        
        # 计算均线
        hist['MA10'] = hist['close'].rolling(10).mean()
        ma10 = hist.iloc[-1]['MA10']
        
        # 计算量能萎缩
        vol_peak = hist.head(3)['vol'].mean()  # 顶部量能
        vol_recent = hist.tail(3)['vol'].mean()  # 近期量能
        shrink_ratio = vol_recent / vol_peak if vol_peak > 0 else 1
        
        return {
            'depth': depth,
            'ma10': ma10,
            'lowest_price': lowest,
            'shrink_ratio': shrink_ratio,
            'days': len(hist)
        }
    
    def _check_adjust_quality(self, adjust: Dict) -> bool:
        """检查调整质量"""
        if not adjust:
            return False
        
        # 调整深度15-35%
        if not (0.15 <= adjust['depth'] <= 0.35):
            return False
        
        # 缩量至40%以下
        if adjust['shrink_ratio'] > 0.40:
            return False
        
        return True
    
    def _calculate_days_since_peak(self, peak_date: str, today: str) -> int:
        """计算从第一波见顶到今天的天数"""
        peak = datetime.strptime(peak_date, "%Y%m%d")
        today_dt = datetime.strptime(today, "%Y%m%d")
        return (today_dt - peak).days

# ==================== 数据准备示例 ====================

def prepare_recent_pools(data_manager, today: str, days: int = 15) -> Dict[str, pd.DataFrame]:
    """
    准备近15日每日涨停池
    """
    pools = {}
    
    for i in range(days):
        date = data_manager.get_date_offset(today, -i)
        pool = data_manager.get_limit_up_pool(date)
        if not pool.empty:
            pools[date] = pool
    
    return pools

# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("龙二波策略V2 - 正确的历史连板判断")
    print("核心：从每日涨停池取近期连板，非日线涨幅计算")
    print("时间范围：近15日内，记忆未散")
    print("")
    print("正确做法：")
    print("1. 取近15日每日涨停池")
    print("2. 检查目标股在哪些日期出现在涨停池")
    print("3. 计算连续出现次数（允许断板1天）")
    print("4. 确认是近期这一波（非3个月前的行情）")
    print("5. 今日再次出现在涨停池=二波启动确认")