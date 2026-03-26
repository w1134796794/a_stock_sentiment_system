"""
模式识别算法 - 整合strategy_engine.py的高级模式
包含：弱转强、二板定龙、分歧转一致、首板突破、龙回头、卡位板、竞价爆量、炸板回封、龙二波等
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import loguru

logger = loguru.logger


@dataclass
class PatternSignal:
    """模式信号数据结构"""
    pattern_type: str
    stock_code: str
    stock_name: str
    confidence: float
    description: str
    key_metrics: Dict
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size: str = "medium"  # light/medium/heavy
    validation_rules: List[str] = None
    
    def __post_init__(self):
        if self.validation_rules is None:
            self.validation_rules = []


class PatternRecognition:
    """模式识别引擎"""
    
    def __init__(self, data_manager):
        self.dm = data_manager
        self.lookback_days = 20
        
    # ==================== 基础模式识别 ====================
    
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        弱转强模式识别：
        条件：昨日涨停但烂板/炸板 + 今日跳空高开2%以上 + 今日涨停
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', today_row.get('ts_code', ''))
            name = today_row.get('名称', today_row.get('name', ''))
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code] if '代码' in yesterday_df.columns else yesterday_df[yesterday_df['ts_code'] == code]
            
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 关键检查：昨日必须是真的涨停（涨幅>=9.5%）
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            
            if yest_change < 9.5:
                continue
            
            # 判断条件
            # 1. 昨日涨停但烂板（炸板次数>0 或 最后封板时间晚于10:00）
            last_limit_time = str(yest_data.get('最后封板时间', '')).strip()
            if last_limit_time.isdigit():
                last_limit_time = last_limit_time.zfill(6)
            if len(last_limit_time) == 6:
                last_limit_time = f"{last_limit_time[:2]}:{last_limit_time[2:4]}:{last_limit_time[4:]}"
            
            yesterday_bad_board = (yest_data.get('炸板次数', 0) > 0 or 
                                  last_limit_time > '10:00:00')
            
            # 2. 今日跳空高开（涨幅>2%）
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            today_gap_up = today_change > 2.0
            
            # 3. 今日也是涨停（>=9.5%）
            today_limit_up = today_change >= 9.5
            
            if yesterday_bad_board and today_gap_up and today_limit_up:
                entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
                signal = PatternSignal(
                    pattern_type="弱转强",
                    stock_code=code,
                    stock_name=name,
                    confidence=0.85,
                    description=f"昨日烂板后今日跳空高开{today_change:.2f}%",
                    key_metrics={
                        "昨日涨幅": f"{yest_change:.2f}%",
                        "昨日炸板次数": yest_data.get('炸板次数', 0),
                        "昨日最后封板": yest_data.get('最后封板时间', ''),
                        "今日涨幅": f"{today_change:.2f}%",
                        "所属概念": today_row.get('所属概念', '')
                    },
                    entry_price=entry_price,
                    stop_loss=entry_price * 0.95,
                    take_profit=entry_price * 1.10,
                    position_size="medium",
                    validation_rules=[
                        "昨日涨停但烂板",
                        "今日跳空高开>2%",
                        "今日继续涨停"
                    ]
                )
                signals.append(signal)
        
        return signals
    
    def detect_second_board_dragon(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        二板定龙模式（基础版）：昨日首板 + 今日高开3-7% + 快速涨停（15分钟内）
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 条件1: 昨日首板（涨停且前日未涨停）
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            if yest_change < 9.5:
                continue
            
            # 条件2: 今日高开3%-7%
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if not (9.5 <= today_change <= 11):  # 今日也是涨停
                continue
            
            # 获取开盘价计算高开幅度
            open_price = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            
            gap_ratio = (open_price - yest_close) / yest_close
            if not (0.03 <= gap_ratio <= 0.07):
                continue
            
            # 条件3: 快速涨停（15分钟内）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '09:45:00':  # 15分钟后涨停不算快速
                continue
            
            # 条件4: 昨日首板质量好（炸板次数=0）
            if yest_data.get('炸板次数', 0) > 0:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="二板定龙",
                stock_code=code,
                stock_name=name,
                confidence=0.88,
                description=f"首板硬逻辑+次日高开{gap_ratio*100:.1f}%+15分钟内涨停",
                key_metrics={
                    "昨日涨幅": f"{yest_change:.2f}%",
                    "今日高开": f"{gap_ratio*100:.1f}%",
                    "涨停时间": limit_up_time,
                    "昨日炸板": yest_data.get('炸板次数', 0)
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.10,
                position_size="medium",
                validation_rules=[
                    "昨日首板硬逻辑",
                    "次日高开3%-7%",
                    "15分钟内快速涨停",
                    "昨日无炸板"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_second_board_dragon_advanced(self, yesterday_zt: pd.DataFrame, 
                                           today_data: pd.DataFrame,
                                           today_auction: pd.DataFrame = None) -> List[PatternSignal]:
        """
        二板定龙模式（高级版）：首板硬逻辑 + 次日高开3-7% + 竞价量>10% + 分时坚决
        """
        signals = []
        if yesterday_zt.empty or today_data.empty:
            return signals
        
        for _, yest_row in yesterday_zt.iterrows():
            code = yest_row.get('代码', '')
            name = yest_row.get('名称', '')
            
            # 查找今日数据
            today_row = today_data[today_data['代码'] == code]
            if today_row.empty:
                continue
            today_row = today_row.iloc[0]
            
            # 条件1: 首板质量检查（硬逻辑）
            first_board_quality = self._check_first_board_quality(yest_row)
            if not first_board_quality['is_valid']:
                continue
            
            # 条件2: 次日高开幅度 3%-7%
            open_gap = today_row.get('开盘价', 0) / yest_row.get('收盘价', 1) - 1
            if not (0.03 <= open_gap <= 0.07):
                continue
            
            # 条件3: 竞价量 > 前日总成交10%（如果有竞价数据）
            auction_vol_ratio = 0.0
            if today_auction is not None and not today_auction.empty:
                auction_vol_ratio = self._get_auction_volume_ratio(code, today_auction, yest_row)
                if auction_vol_ratio < 0.10:
                    continue
            
            # 条件4: 分时拉升坚决（开盘15分钟内涨停）
            limit_up_time = today_row.get('首次封板时间', '')
            if not self._is_fast_limit_up(limit_up_time, max_minutes=15):
                continue
            
            # 条件5: 封单持续增加
            seal_strength = today_row.get('封单额', 0) / (yest_row.get('流通市值', 1) * 10000)
            if seal_strength < 0.10:
                continue
            
            # 计算买点和风控
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            stop_loss = entry_price * 0.95  # -5%止损
            
            signal = PatternSignal(
                pattern_type="二板定龙(高级)",
                stock_code=code,
                stock_name=name,
                confidence=0.85 if auction_vol_ratio > 0.15 else 0.75,
                description=f"首板硬逻辑+次日高开{open_gap*100:.1f}%+竞价量{auction_vol_ratio*100:.1f}%",
                key_metrics={
                    "首板封单质量": first_board_quality['score'],
                    "高开幅度": f"{open_gap*100:.2f}%",
                    "竞价量比": f"{auction_vol_ratio*100:.1f}%",
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_strength*100:.1f}%"
                },
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=entry_price * 1.10,
                position_size="medium" if auction_vol_ratio > 0.15 else "light",
                validation_rules=[
                    "首板硬逻辑（政策加持主线）",
                    "高开3%-7%（避免秒板或低开）",
                    "竞价量>前日10%",
                    "15分钟内涨停",
                    "封单额>流通市值10%"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_first_board_breakout(self, today_df: pd.DataFrame) -> List[PatternSignal]:
        """
        首板突破模式（基础版）：早盘秒封（9:40前）+ 放量 + 突破形态
        """
        signals = []
        
        if today_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 条件1: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 早盘秒封（9:40前）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '09:40:00':
                continue
            
            # 条件3: 封单强度（封单额/流通市值 > 5%）
            seal_amount = today_row.get('封单额', 0)
            float_cap = today_row.get('流通市值', 1) * 10000  # 万元转元
            seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
            
            if seal_ratio < 0.05:
                continue
            
            # 条件4: 换手率适中（5%-20%）
            turnover = today_row.get('换手率', 0)
            if not (5 <= turnover <= 20):
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="首板突破",
                stock_code=code,
                stock_name=name,
                confidence=0.80,
                description=f"早盘秒封{limit_up_time}+封单强度{seal_ratio*100:.1f}%+换手{turnover:.1f}%",
                key_metrics={
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_ratio*100:.1f}%",
                    "换手率": f"{turnover:.1f}%",
                    "涨跌幅": f"{today_change:.2f}%"
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.93,
                take_profit=entry_price * 1.08,
                position_size="light",
                validation_rules=[
                    "早盘秒封（9:40前）",
                    "封单强度>5%",
                    "换手率5%-20%"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_first_board_breakout_advanced(self, today_data: pd.DataFrame,
                                             hist_30d: pd.DataFrame = None,
                                             sector_strength: float = 0.7) -> List[PatternSignal]:
        """
        首板突破模式（高级版）：突破关键压力位 + 早盘秒封 + 量能放大 + 主线题材
        """
        signals = []
        
        if today_data.empty:
            return signals
            
        for _, today_row in today_data.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 条件1: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 突破关键压力位（前高/平台/年线）- 如果有历史数据
            if hist_30d is not None and not hist_30d.empty and len(hist_30d) >= 20:
                current_price = today_row.get('收盘价', 0)
                high_20d = hist_30d['high'].tail(20).max() if 'high' in hist_30d.columns else current_price
                high_60d = hist_30d['high'].tail(60).max() if 'high' in hist_30d.columns and len(hist_30d) >= 60 else high_20d
                
                breakout_level = max(high_20d, high_60d * 0.95)
                if current_price < breakout_level:
                    continue
            
            # 条件3: 早盘秒封（9:40前）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if not self._is_fast_limit_up(limit_up_time, max_minutes=40):
                continue
            
            # 条件4: 必须是主线题材（板块强度>阈值）
            if sector_strength < 0.6:
                continue
            
            # 条件5: 封单额>流通市值10%
            seal_amount = today_row.get('封单额', 0)
            float_cap = today_row.get('流通市值', 1) * 10000
            if seal_amount < float_cap * 0.10:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="首板突破(高级)",
                stock_code=code,
                stock_name=name,
                confidence=0.75,
                description=f"突破压力位+早盘秒封+主线题材",
                key_metrics={
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_amount/float_cap*100:.1f}%",
                    "板块强度": f"{sector_strength:.2f}",
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.93,
                take_profit=entry_price * 1.08,
                position_size="light",
                validation_rules=[
                    "突破20日/60日高点",
                    "9:40前涨停",
                    "主线题材（板块强度>0.6）",
                    "封单额>流通市值10%"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_divergence_to_consensus(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        分歧转一致模式（基础版）：昨日烂板爆量 + 今日弱转强高开2-5% + 快速涨停
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            
            yest_data = yest_row.iloc[0]
            
            # 条件1: 昨日涨停但烂板
            yest_change = yest_data.get('涨跌幅', 0)
            if isinstance(yest_change, str):
                yest_change = float(yest_change.replace('%', ''))
            if yest_change < 9.5:
                continue
            
            # 昨日烂板判断
            yest_open_times = yest_data.get('炸板次数', 0)
            yest_last_time = str(yest_data.get('最后封板时间', '')).strip()
            if yest_last_time.isdigit():
                yest_last_time = yest_last_time.zfill(6)
            if len(yest_last_time) == 6:
                yest_last_time = f"{yest_last_time[:2]}:{yest_last_time[2:4]}:{yest_last_time[4:]}"
            
            if yest_open_times == 0 and yest_last_time <= '10:00:00':
                continue  # 昨日不是烂板
            
            # 条件2: 今日高开2%-5%
            open_price = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            
            gap_ratio = (open_price - yest_close) / yest_close
            if not (0.02 <= gap_ratio <= 0.05):
                continue
            
            # 条件3: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件4: 快速涨停（30分钟内）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if limit_up_time.isdigit():
                limit_up_time = limit_up_time.zfill(6)
            if len(limit_up_time) == 6:
                limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
            
            if limit_up_time > '10:00:00':
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="分歧转一致",
                stock_code=code,
                stock_name=name,
                confidence=0.82,
                description=f"昨日烂板后今日高开{gap_ratio*100:.1f}%转一致涨停",
                key_metrics={
                    "昨日炸板": yest_open_times,
                    "昨日最后封板": yest_last_time,
                    "今日高开": f"{gap_ratio*100:.1f}%",
                    "涨停时间": limit_up_time
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.93,
                take_profit=entry_price * 1.15,
                position_size="medium",
                validation_rules=[
                    "昨日烂板",
                    "今日高开2%-5%",
                    "30分钟内涨停"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_divergence_to_consensus_advanced(self, today_df: pd.DataFrame, 
                                                yesterday_df: pd.DataFrame,
                                                hist_data: pd.DataFrame = None) -> List[PatternSignal]:
        """
        分歧转一致模式（高级版）：三板烂板爆量分歧，次日弱转强上板
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            yesterday_data = yest_row.iloc[0]
            
            # 条件1: 昨日必须是烂板（炸板次数>0 或 开板）
            yesterday_bad = yesterday_data.get('炸板次数', 0) > 0 or yesterday_data.get('开板次数', 0) > 0
            if not yesterday_bad:
                continue
            
            # 条件2: 昨日爆量（成交量>前日2倍）- 如果有历史数据
            if hist_data is not None and not hist_data.empty and len(hist_data) >= 2:
                yest_vol = yesterday_data.get('成交量', 0)
                prev_vol = hist_data.iloc[-2]['vol'] if 'vol' in hist_data.columns else yest_vol
                if yest_vol < prev_vol * 1.5:
                    continue
            
            # 条件3: 今日弱转强高开2%-5%
            open_price = today_row.get('开盘价', 0)
            yest_close = yesterday_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            gap_ratio = (open_price - yest_close) / yest_close
            if not (0.02 <= gap_ratio <= 0.05):
                continue
            
            # 条件4: 今日涨停（确认一致）
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="分歧转一致(高级)",
                stock_code=code,
                stock_name=name,
                confidence=0.80,
                description=f"三板烂板后爆量分歧，次日弱转强高开{gap_ratio*100:.1f}%",
                key_metrics={
                    "昨日炸板次数": yesterday_data.get('炸板次数', 0),
                    "今日高开": f"{gap_ratio*100:.1f}%",
                    "涨停时间": today_row.get('首次封板时间', '')
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.93,
                take_profit=entry_price * 1.15,
                position_size="medium",
                validation_rules=[
                    "昨日烂板（炸板>0）",
                    "昨日爆量（>前日1.5倍）",
                    "今日高开2%-5%",
                    "今日涨停确认"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_position_battle(self, hierarchy_df: pd.DataFrame) -> List[PatternSignal]:
        """
        卡位板识别：同板块内，低位股抢先涨停
        """
        signals = []
        
        if hierarchy_df.empty:
            return signals
        
        # 按板块分组
        for sector_name, group in hierarchy_df.groupby('L3_Industry'):
            if len(group) < 2:  # 至少2只才能卡位
                continue
            
            # 按涨停时间排序
            group = group.copy()
            group['limit_up_time_sort'] = pd.to_datetime(group['LimitUpTime'], format='%H:%M:%S', errors='coerce')
            sorted_group = group.sort_values('limit_up_time_sort')
            
            if len(sorted_group) >= 2:
                first_limit = sorted_group.iloc[0]
                second_limit = sorted_group.iloc[1]
                
                # 第一个涨停时间早于第二个5分钟以上
                time_diff = (second_limit['limit_up_time_sort'] - first_limit['limit_up_time_sort']).total_seconds()
                if time_diff > 300:  # 5分钟
                    signal = PatternSignal(
                        pattern_type="卡位板",
                        stock_code=first_limit['Code'],
                        stock_name=first_limit['Name'],
                        confidence=0.75,
                        description=f"在{sector_name}板块中抢先{first_limit['LimitUpTime']}涨停，卡位成功",
                        key_metrics={
                            "涨停时间": first_limit['LimitUpTime'],
                            "板块内排名": 1,
                            "领先第二名": f"{int(time_diff)}秒",
                            "封板强度": "强" if first_limit['OpenTimes'] == 0 else "中"
                        },
                        validation_rules=[
                            "板块内最先涨停",
                            "领先第二名5分钟以上"
                        ]
                    )
                    signals.append(signal)
        
        return signals
    
    # ==================== 新增高级模式 ====================
    
    def detect_auction_volume_surge(self, today_df: pd.DataFrame, 
                                   yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        竞价爆量战法：竞价量>前日5% + 高开1%-7% + 封单坚决
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code]
            if yest_row.empty:
                continue
            yest_data = yest_row.iloc[0]
            
            # 条件1: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 高开1%-7%
            open_price = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', 0)
            if open_price == 0 or yest_close == 0:
                continue
            gap = (open_price - yest_close) / yest_close
            if not (0.01 <= gap <= 0.07):
                continue
            
            # 条件3: 早盘快速涨停（30分钟内）
            limit_up_time = str(today_row.get('首次封板时间', '')).strip()
            if not self._is_fast_limit_up(limit_up_time, max_minutes=30):
                continue
            
            # 条件4: 封单坚决（封单额/流通市值 > 8%）
            seal_amount = today_row.get('封单额', 0)
            float_cap = today_row.get('流通市值', 1) * 10000
            seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
            if seal_ratio < 0.08:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="竞价爆量",
                stock_code=code,
                stock_name=name,
                confidence=0.80 if seal_ratio > 0.15 else 0.70,
                description=f"高开{gap*100:.1f}%+早盘秒封+封单强度{seal_ratio*100:.1f}%",
                key_metrics={
                    "高开幅度": f"{gap*100:.1f}%",
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_ratio*100:.1f}%",
                    "昨日收盘": yest_close
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.08,
                position_size="medium" if seal_ratio > 0.15 else "light",
                validation_rules=[
                    "高开1%-7%",
                    "30分钟内涨停",
                    "封单额>流通市值8%"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_blast_reseal(self, today_df: pd.DataFrame) -> List[PatternSignal]:
        """
        炸板回封：早盘炸板后30分钟内放量回封
        """
        signals = []
        
        if today_df.empty:
            return signals
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 条件1: 今日必须涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 有炸板记录
            blast_times = today_row.get('炸板次数', 0)
            if blast_times == 0:
                continue
            
            # 条件3: 首次封板时间在早盘（10:00前）
            first_limit_time = str(today_row.get('首次封板时间', '')).strip()
            if not self._is_fast_limit_up(first_limit_time, max_minutes=60):
                continue
            
            # 条件4: 最后封板时间晚于首次封板（说明炸板后回封）
            last_limit_time = str(today_row.get('最后封板时间', '')).strip()
            if last_limit_time <= first_limit_time:
                continue
            
            # 条件5: 回封时间在炸板后30分钟内
            try:
                first_dt = datetime.strptime(first_limit_time, "%H:%M:%S")
                last_dt = datetime.strptime(last_limit_time, "%H:%M:%S")
                if (last_dt - first_dt).seconds > 1800:  # 30分钟
                    continue
            except:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="炸板回封",
                stock_code=code,
                stock_name=name,
                confidence=0.75,
                description=f"早盘{first_limit_time}炸板后{last_limit_time}放量回封",
                key_metrics={
                    "首次封板时间": first_limit_time,
                    "最后封板时间": last_limit_time,
                    "炸板次数": blast_times,
                    "封单强度": f"{today_row.get('封单额', 0) / (today_row.get('流通市值', 1) * 10000) * 100:.1f}%"
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.10,
                position_size="light",
                validation_rules=[
                    "早盘涨停",
                    "有炸板记录",
                    "30分钟内回封"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def detect_dragon_second_wave(self, today_df: pd.DataFrame,
                                 yesterday_df: pd.DataFrame = None,
                                 day_before_yesterday_df: pd.DataFrame = None,
                                 hist_60d_dict: Dict[str, pd.DataFrame] = None,
                                 hot_sectors: List[str] = None) -> List[PatternSignal]:
        """
        龙二波：历史龙头（曾>=3连板）+ 调整期（至少2日未涨停）+ 今日首板启动 + 板块热度不退
        
        关键条件：
        1. 历史上曾经有过>=3连板（证明是龙头）
        2. 昨日和前日都未涨停（调整期，今日是首板）
        3. 今日涨停启动
        4. 板块热度未退
        """
        signals = []
        
        if today_df.empty:
            return signals
        
        # 获取昨日和前日的涨停股票代码集合
        yesterday_zt_codes = set()
        day_before_yesterday_zt_codes = set()
        
        if yesterday_df is not None and not yesterday_df.empty:
            yesterday_zt_codes = set(yesterday_df['代码'].tolist()) if '代码' in yesterday_df.columns else set()
        
        if day_before_yesterday_df is not None and not day_before_yesterday_df.empty:
            day_before_yesterday_zt_codes = set(day_before_yesterday_df['代码'].tolist()) if '代码' in day_before_yesterday_df.columns else set()
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # 条件1: 今日涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 条件2: 昨日未涨停（确保今日是首板启动）
            if code in yesterday_zt_codes:
                continue  # 昨日已涨停，不是首板
            
            # 条件3: 前日也未涨停（确保有调整期）
            if code in day_before_yesterday_zt_codes:
                continue  # 前日已涨停，调整期不够
            
            # 条件4: 历史必须有连板记录（曾>=3连板）- 证明是龙头
            has_dragon_history = False
            if hist_60d_dict and code in hist_60d_dict:
                hist_data = hist_60d_dict[code]
                if hist_data is not None and not hist_data.empty and len(hist_data) >= 30:
                    hist_data = hist_data.sort_values('trade_date') if 'trade_date' in hist_data.columns else hist_data
                    
                    # 计算历史最大连板数
                    if 'pct_change' in hist_data.columns:
                        hist_data['is_limit_up'] = hist_data['pct_change'] >= 9.5
                        # 计算最大连续涨停天数
                        consecutive = 0
                        max_consecutive = 0
                        for is_zt in hist_data['is_limit_up']:
                            if is_zt:
                                consecutive += 1
                                max_consecutive = max(max_consecutive, consecutive)
                            else:
                                consecutive = 0
                        
                        if max_consecutive >= 3:
                            has_dragon_history = True
            
            if not has_dragon_history:
                continue  # 没有龙头历史
            
            # 条件5: 板块热度未退
            sector = today_row.get('L3_Industry', '') or today_row.get('所属概念', '')
            if hot_sectors and sector and sector not in hot_sectors:
                continue
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="龙二波",
                stock_code=code,
                stock_name=name,
                confidence=0.78,
                description=f"历史龙头调整后首板启动（已调整至少2日）",
                key_metrics={
                    "涨停时间": today_row.get('首次封板时间', ''),
                    "所属板块": sector,
                    "昨日涨停": "否",
                    "前日涨停": "否",
                    "封单强度": f"{today_row.get('封单额', 0) / (today_row.get('流通市值', 1) * 10000) * 100:.1f}%"
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.20,
                position_size="medium",
                validation_rules=[
                    "历史>=3连板（龙头）",
                    "昨日未涨停（调整期）",
                    "前日未涨停（调整期）",
                    "今日首板启动",
                    "板块热度未退"
                ]
            )
            signals.append(signal)
        
        return signals
    
    # ==================== 批量扫描接口 ====================
    
    def scan_all_patterns(self, today_date: str, yesterday_date: str) -> Dict[str, List[PatternSignal]]:
        """
        扫描全市场所有模式
        """
        results = {
            "弱转强": [],
            "二板定龙": [],
            "首板突破": [],
            "分歧转一致": [],
            "卡位板": [],
            "竞价爆量": [],
            "炸板回封": [],
            "龙二波": []
        }
        
        # 获取今日和昨日涨停池
        today_zt = self.dm.get_limit_up_pool(today_date)
        yesterday_zt = self.dm.get_limit_up_pool(yesterday_date)
        
        if today_zt.empty:
            logger.warning("今日涨停池为空，无法识别模式")
            return results
        
        logger.info(f"开始模式识别，今日涨停{len(today_zt)}只，昨日涨停{len(yesterday_zt)}只")
        
        # 1. 检测弱转强
        if not yesterday_zt.empty:
            results["弱转强"] = self.detect_weak_to_strong(today_zt, yesterday_zt)
            logger.info(f"  弱转强: {len(results['弱转强'])}个")
        
        # 2. 检测二板定龙
        if not yesterday_zt.empty:
            results["二板定龙"] = self.detect_second_board_dragon(today_zt, yesterday_zt)
            logger.info(f"  二板定龙: {len(results['二板定龙'])}个")
        
        # 3. 检测首板突破
        results["首板突破"] = self.detect_first_board_breakout(today_zt)
        logger.info(f"  首板突破: {len(results['首板突破'])}个")
        
        # 4. 检测分歧转一致
        if not yesterday_zt.empty:
            results["分歧转一致"] = self.detect_divergence_to_consensus(today_zt, yesterday_zt)
            logger.info(f"  分歧转一致: {len(results['分歧转一致'])}个")
        
        # 5. 检测卡位板
        from core.industry_mapper import IndustryMapper
        from config.settings import INDUSTRY_MAPPING_FILE
        mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
        hierarchy_df = mapper.build_hierarchy_dataframe(today_zt)
        results["卡位板"] = self.detect_position_battle(hierarchy_df)
        logger.info(f"  卡位板: {len(results['卡位板'])}个")
        
        # 6. 检测竞价爆量
        if not yesterday_zt.empty:
            results["竞价爆量"] = self.detect_auction_volume_surge(today_zt, yesterday_zt)
            logger.info(f"  竞价爆量: {len(results['竞价爆量'])}个")
        
        # 7. 检测炸板回封
        results["炸板回封"] = self.detect_blast_reseal(today_zt)
        logger.info(f"  炸板回封: {len(results['炸板回封'])}个")
        
        # 8. 检测龙二波（需要前日数据）
        # 计算前交易日日期
        from datetime import datetime, timedelta
        today_dt = datetime.strptime(today_date, "%Y%m%d")
        day_before_yesterday_dt = today_dt - timedelta(days=2)
        day_before_yesterday_date = day_before_yesterday_dt.strftime("%Y%m%d")
        day_before_yesterday_zt = self.dm.get_limit_up_pool(day_before_yesterday_date)
        
        results["龙二波"] = self.detect_dragon_second_wave(
            today_zt, yesterday_zt, day_before_yesterday_zt
        )
        logger.info(f"  龙二波: {len(results['龙二波'])}个")
        
        total = sum(len(v) for v in results.values())
        logger.info(f"模式识别完成，共{total}个信号")
        
        return results
    
    # ==================== 辅助方法 ====================
    
    def _is_fast_limit_up(self, time_str: str, max_minutes: int = 15) -> bool:
        """判断是否快速涨停"""
        if not time_str or time_str == '-':
            return False
        try:
            # 处理各种时间格式
            time_str = str(time_str).strip()
            if time_str.isdigit():
                time_str = time_str.zfill(6)
            if len(time_str) == 6 and ':' not in time_str:
                time_str = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
            
            hour, minute = map(int, time_str.split(':')[:2])
            total_minutes = hour * 60 + minute
            return total_minutes <= 9 * 60 + 30 + max_minutes  # 开盘后max_minutes分钟内
        except:
            return False
    
    def _check_first_board_quality(self, row: pd.Series) -> Dict:
        """检查首板质量：封单额、换手率、题材强度"""
        seal_amount = row.get('封单额', 0)
        float_cap = row.get('流通市值', 1) * 10000  # 万元转元
        turnover = row.get('换手率', 0)
        concept = row.get('所属概念', '')
        
        score = 0
        # 封单额>流通市值10%
        if seal_amount > float_cap * 0.10:
            score += 40
        # 换手率5%-15%
        if 5 <= turnover <= 15:
            score += 30
        # 有明确热点概念
        if concept and len(concept) > 3:
            score += 30
            
        return {'is_valid': score >= 60, 'score': score}
    
    def _get_auction_volume_ratio(self, code: str, auction_data: pd.DataFrame, 
                                  prev_row: pd.Series) -> float:
        """获取竞价量占前日总成交比例"""
        if auction_data.empty or '代码' not in auction_data.columns:
            return 0.0
        auction_vol = auction_data[auction_data['代码'] == code]['竞价成交量'].values
        if len(auction_vol) == 0:
            return 0.0
        prev_vol = prev_row.get('成交量', 1)
        return auction_vol[0] / prev_vol if prev_vol > 0 else 0
    
    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """计算RSI指标"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))


if __name__ == "__main__":
    print("模式识别模块初始化成功")
    print("支持模式: 弱转强、二板定龙、首板突破、分歧转一致、卡位板、竞价爆量、炸板回封、龙二波")
