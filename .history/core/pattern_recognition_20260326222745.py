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
        
        # 高级模式参数配置
        self.params = {
            # 弱转强参数
            "weak_to_strong": {
                "min_board_height": 3,           # 至少3板
                "max_board_height": 8,           # 不超过8板
                "min_gap": 0.02,                 # 高开>2%
                "ideal_gap": 0.04,               # 理想高开4%
                "max_gap": 0.07,                 # 高开<7%
                "min_auction_vol_ratio": 0.08,   # 竞价量>8%
                "min_auction_amount": 5000000,   # 竞价金额>500万
                "max_time_to_limit": 10,         # 10分钟内涨停
                "max_open_drop": 0.02,           # 开盘回踩<2%
            },
            # 卡位板参数
            "position_battle": {
                "min_high_board": 3,             # 高位股至少3板
                "max_high_board": 6,             # 高位股不超过6板
                "max_low_board": 2,              # 低位股最多2板
                "min_lead_time": 5,              # 领先至少5分钟
                "ideal_lead_time": 15,           # 理想领先15分钟
                "high_blast_threshold": 2,       # 高位股炸板>2次视为疲态
                "low_seal_strength": 0.08,       # 低位股封单>流通市值8%
            }
        }
        
    # ==================== 基础模式识别 ====================
    
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                              day_before_yesterday_df: pd.DataFrame = None) -> List[PatternSignal]:
        """
        弱转强模式识别（高级版）：
        核心：超预期 = 昨日烂板/断板 + 今日竞价高开爆量
        
        条件：
        1. 身份识别：至少3板高标（有辨识度）
        2. 昨日"弱"：烂板/断板/尾盘板，但有承接
        3. 今日"强"：高开2-7% + 竞价量>8% + 竞价金额>500万
        4. 确认：开盘不回踩（<2%），10分钟内涨停
        
        买点：竞价末段或开盘第一笔，绝非打板
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        p = self.params["weak_to_strong"]
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', today_row.get('ts_code', ''))
            name = today_row.get('名称', today_row.get('name', ''))
            
            # 查找昨日数据
            yest_row = yesterday_df[yesterday_df['代码'] == code] if '代码' in yesterday_df.columns else yesterday_df[yesterday_df['ts_code'] == code]
            if yest_row.empty:
                continue
            yest_data = yest_row.iloc[0]
            
            # ========== 步骤1：计算连板高度（身份识别）==========
            board_height = self._calculate_board_height(
                code, today_row, yesterday_df, day_before_yesterday_df
            )
            
            # 至少3板高标才值得博弈
            if not (p["min_board_height"] <= board_height <= p["max_board_height"]):
                continue
            
            # ========== 步骤2：昨日"弱"的质量 ==========
            weak_quality = self._analyze_yesterday_weak(yest_data)
            if not weak_quality['is_valid_weak']:
                continue
            
            # ========== 步骤3：今日"强"的态度（竞价超预期）==========
            # 获取今日开盘数据
            today_open = today_row.get('开盘价', 0)
            yest_close = yest_data.get('收盘价', yest_data.get('最新价', 1))
            
            if yest_close <= 0:
                continue
            
            gap = (today_open - yest_close) / yest_close
            
            # 高开范围：2%-7%
            if not (p["min_gap"] <= gap <= p["max_gap"]):
                continue
            
            # 今日涨幅确认涨停
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))
            if today_change < 9.5:
                continue
            
            # 获取竞价数据（封单额作为竞价量的代理）
            seal_amount = today_row.get('封单额', 0)
            yest_turnover = yest_data.get('成交额', yest_data.get('成交量', 1) * yest_close)
            auction_vol_ratio = seal_amount / yest_turnover if yest_turnover > 0 else 0
            
            # 竞价量>8%（用封单额/昨日成交额作为代理）
            if auction_vol_ratio < p["min_auction_vol_ratio"]:
                continue
            
            # 封单金额>500万
            if seal_amount < p["min_auction_amount"]:
                continue
            
            # ========== 步骤4：涨停时间确认（快速上板）==========
            limit_time = today_row.get('首次封板时间', '')
            minutes_to_limit = self._calculate_minutes_from_open(limit_time)
            
            # 10分钟内涨停
            if minutes_to_limit > p["max_time_to_limit"]:
                continue
            
            # ========== 计算买点和置信度 ==========
            confidence = 0.70
            buy_timing = "开盘第一笔"
            
            # 强超预期：高开4%+竞价量12%
            if gap >= p["ideal_gap"] and auction_vol_ratio >= 0.12:
                confidence = 0.90
                buy_timing = "竞价末段"
            elif gap >= p["ideal_gap"]:
                confidence = 0.85
            elif auction_vol_ratio >= 0.12:
                confidence = 0.80
            
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            
            signal = PatternSignal(
                pattern_type="弱转强",
                stock_code=code,
                stock_name=name,
                confidence=round(confidence, 2),
                description=f"{board_height}板{weak_quality['weak_type']}后，次日高开{gap*100:.1f}%竞价量{auction_vol_ratio*100:.1f}%，{minutes_to_limit}分钟涨停",
                key_metrics={
                    "连板高度": board_height,
                    "昨日弱类型": weak_quality['weak_type'],
                    "昨日烂板质量": weak_quality['score'],
                    "次日高开": f"{gap*100:.1f}%",
                    "竞价量比": f"{auction_vol_ratio*100:.1f}%",
                    "竞价金额": f"{seal_amount/1e4:.0f}万",
                    "涨停用时": f"{minutes_to_limit}分钟",
                    "买点时机": buy_timing
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.93,
                take_profit=entry_price * 1.15,
                position_size="heavy" if confidence >= 0.85 else "medium",
                validation_rules=[
                    f"{board_height}板高标（身份）",
                    f"昨日{weak_quality['weak_type']}（弱）",
                    f"次日高开{gap*100:.1f}%（超预期）",
                    f"竞价量{auction_vol_ratio*100:.1f}%（资金抢筹）",
                    f"{minutes_to_limit}分钟涨停（确认强）",
                    f"买点：{buy_timing}"
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
    
    def detect_position_battle(self, today_df: pd.DataFrame, 
                               yesterday_df: pd.DataFrame,
                               sector_mapping: Dict[str, str] = None) -> List[PatternSignal]:
        """
        卡位板识别（高级版）：后发先至，低位股抢先涨停取代高位龙头
        
        核心逻辑（参考position_battle.py）：
        1. 识别高位龙头（3-6板）和低位股（1-2板）
        2. 对比涨停时间（低位领先至少5分钟）
        3. 封单质量对比（低位股封单>流通市值8%，高位股疲态信号）
        4. 判断卡位类型：成功/失败/待定/假卡位
        
        Args:
            today_df: 今日涨停池数据
            yesterday_df: 昨日涨停池数据
            sector_mapping: 股票代码到板块的映射 {code: sector}
        """
        signals = []
        
        if today_df.empty or yesterday_df.empty:
            return signals
        
        p = self.params["position_battle"]
        
        # 获取板块信息
        if sector_mapping is None:
            # 从数据中提取板块信息
            sector_mapping = {}
            for _, row in today_df.iterrows():
                code = row.get('代码', '')
                sector = row.get('所属概念', '') or row.get('L3_Industry', '')
                if code and sector:
                    sector_mapping[code] = sector
        
        # 按板块分组分析
        sector_stocks = {}
        for code, sector in sector_mapping.items():
            if sector not in sector_stocks:
                sector_stocks[sector] = []
            row = today_df[today_df['代码'] == code]
            if not row.empty:
                sector_stocks[sector].append(row.iloc[0])
        
        # 分析每个板块
        for sector, stocks in sector_stocks.items():
            if len(stocks) < 2:
                continue
            
            # 识别高位龙头（3-6板）和低位股（1-2板）
            high_stocks = []
            low_stocks = []
            
            for stock in stocks:
                code = stock.get('代码', '')
                boards = self._calculate_board_height(code, stock, yesterday_df, None)
                
                stock_info = {
                    'code': code,
                    'name': stock.get('名称', ''),
                    'boards': boards,
                    'limit_up_time': stock.get('首次封板时间', ''),
                    'blast_times': stock.get('炸板次数', 0),
                    'seal_amount': stock.get('封单额', 0),
                    'float_cap': stock.get('流通市值', 1),
                    'turnover': stock.get('换手率', 0)
                }
                
                # 高位股：3-6板
                if p["min_high_board"] <= boards <= p["max_high_board"]:
                    high_stocks.append(stock_info)
                # 低位股：1-2板
                elif 1 <= boards <= p["max_low_board"]:
                    low_stocks.append(stock_info)
            
            if not high_stocks or not low_stocks:
                continue
            
            # 按高度排序高位股，取最高的
            high_stocks.sort(key=lambda x: x['boards'], reverse=True)
            high_stocks = high_stocks[:2]  # 最多2个高位股
            
            # 对比每对高低位股
            for high in high_stocks:
                for low in low_stocks:
                    if low['code'] == high['code']:
                        continue
                    
                    battle_signal = self._analyze_position_battle(high, low, p, sector)
                    if battle_signal:
                        signals.append(battle_signal)
        
        # 只保留卡位成功和待定的信号，按置信度排序
        valid_signals = [s for s in signals if '卡位成功' in s.description or '卡位待定' in s.description]
        valid_signals.sort(key=lambda x: x.confidence, reverse=True)
        
        return valid_signals[:3]  # 最多3个
    
    def _analyze_position_battle(self, high: Dict, low: Dict, p: Dict, sector: str) -> Optional[PatternSignal]:
        """
        分析高低位股的卡位关系
        """
        # 时间对比
        high_time = str(high['limit_up_time']).strip()
        low_time = str(low['limit_up_time']).strip()
        
        if not high_time or not low_time:
            return None
        
        # 低位股必须抢先涨停
        lead_minutes = self._calculate_time_diff(low_time, high_time)
        if lead_minutes < p["min_lead_time"]:
            return None  # 领先时间不够，不算卡位
        
        # 封板质量对比
        high_seal_ratio = high['seal_amount'] / (high['float_cap'] * 10000) if high['float_cap'] > 0 else 0
        low_seal_ratio = low['seal_amount'] / (low['float_cap'] * 10000) if low['float_cap'] > 0 else 0
        
        # 高位股疲态信号
        high_weak_signs = 0
        if high['blast_times'] >= p["high_blast_threshold"]:
            high_weak_signs += 1
        if high_seal_ratio < 0.05:  # 封单弱
            high_weak_signs += 1
        if high['turnover'] > 25:   # 换手过高
            high_weak_signs += 1
        
        # 低位股强势信号
        low_strong_signs = 0
        if low['blast_times'] == 0:  # 不炸板
            low_strong_signs += 1
        if low_seal_ratio > p["low_seal_strength"]:
            low_strong_signs += 1
        if lead_minutes >= p["ideal_lead_time"]:
            low_strong_signs += 1
        
        # 判断卡位类型
        if high_weak_signs >= 2 and low_strong_signs >= 2:
            # 高位弱+低位强=卡位成功概率高
            battle_type = "卡位成功"
            confidence = 0.78
            description = f"低位{low['name']}({low['boards']}板)领先{lead_minutes}分钟涨停，卡位高位{high['name']}({high['boards']}板)"
            action = "打板买入低位股，回避高位股"
            
        elif high_weak_signs >= 1 and low_strong_signs >= 1:
            # 双方都有机会
            battle_type = "卡位待定"
            confidence = 0.60
            description = f"低位{low['name']}领先{lead_minutes}分钟，但高位{high['name']}仍强，次日竞争"
            action = "观望，等收盘确认谁封死"
            
        elif high_weak_signs == 0:
            # 高位股仍强，低位股是跟风
            battle_type = "假卡位"
            confidence = 0.35
            description = f"高位{high['name']}仍强势，低位{low['name']}是跟风"
            action = "不介入，低位股是跟风"
            
        else:
            battle_type = "卡位失败"
            confidence = 0.40
            description = f"低位{low['name']}卡位失败，高位{high['name']}回封"
            action = "放弃，卡位失败"
        
        # 只返回成功和待定的信号
        if battle_type not in ["卡位成功", "卡位待定"]:
            return None
        
        return PatternSignal(
            pattern_type="卡位板",
            stock_code=low['code'],
            stock_name=low['name'],
            confidence=confidence,
            description=description,
            key_metrics={
                "卡位类型": battle_type,
                "高位股": f"{high['name']}({high['boards']}板)",
                "低位股": f"{low['name']}({low['boards']}板)",
                "领先时间": f"{lead_minutes}分钟",
                "高位疲态": high_weak_signs,
                "低位强势": low_strong_signs,
                "操作建议": action
            },
            entry_price=low.get('涨停价', 0),
            stop_loss=low.get('涨停价', 0) * 0.93 if low.get('涨停价') else None,
            take_profit=low.get('涨停价', 0) * 1.10 if low.get('涨停价') else None,
            position_size="medium",
            validation_rules=[
                f"高位{high['boards']}板 vs 低位{low['boards']}板",
                f"低位领先{lead_minutes}分钟涨停",
                f"高位疲态信号{high_weak_signs}个",
                f"低位强势信号{low_strong_signs}个",
                battle_type
            ]
        )
    
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
                                 recent_zt_pools: Dict[str, pd.DataFrame],
                                 hot_sectors: List[str] = None) -> List[PatternSignal]:
        """
        龙二波：从近15日涨停池重建连板记录，识别近期真龙的二波启动
        
        核心逻辑（参考dragon_second_wave.py）：
        1. 从近15日每日涨停池重建该股的连板记录（非日线涨幅计算）
        2. 第一波至少4板，最多12板（真龙标准）
        3. 调整期最多10天（记忆未散）
        4. 今日首板启动（再次出现在涨停池）
        5. 板块热度未退
        
        Args:
            today_df: 今日涨停池数据
            recent_zt_pools: 近15日每日涨停池字典 {日期: DataFrame}
            hot_sectors: 热门板块列表
        """
        signals = []
        
        if today_df.empty or not recent_zt_pools:
            return signals
        
        # 龙二波参数
        params = {
            "recent_days": 15,       # 只取近15天内的行情
            "max_adjust_days": 10,   # 调整期最多10天
            "min_first_wave": 4,     # 第一波至少4板
            "max_first_wave": 12,    # 第一波最多12板
        }
        
        for _, today_row in today_df.iterrows():
            code = today_row.get('代码', '')
            name = today_row.get('名称', '')
            
            # ========== 步骤1: 从涨停池重建近期连板记录 ==========
            consecutive_record = self._rebuild_consecutive_from_pools(
                code, recent_zt_pools
            )
            
            if not consecutive_record['is_valid']:
                continue
            
            first_wave_info = consecutive_record['first_wave']
            
            # ========== 步骤2: 判断第一波高度（真龙标准）==========
            if not (params["min_first_wave"] <= first_wave_info['max_boards'] <= params["max_first_wave"]):
                continue
            
            # ========== 步骤3: 检查调整期是否在合理范围 ==========
            dates = sorted(recent_zt_pools.keys())
            today_str = dates[-1] if dates else None
            if not today_str:
                continue
            
            peak_date = first_wave_info['peak_date']
            days_since_peak = self._calculate_days_between(peak_date, today_str)
            
            # 调整期不能太久（记忆未散）
            if days_since_peak > params["max_adjust_days"]:
                continue
            
            # ========== 步骤4: 确认今日是首板启动 ==========
            # 检查昨日是否未涨停（确保今日是首板）
            if len(dates) >= 2:
                yesterday_str = dates[-2]
                yesterday_pool = recent_zt_pools.get(yesterday_str, pd.DataFrame())
                if not yesterday_pool.empty and code in yesterday_pool['代码'].values:
                    continue  # 昨日已涨停，不是首板启动
            
            # ========== 步骤5: 板块热度未退 ==========
            sector = today_row.get('L3_Industry', '') or today_row.get('所属概念', '')
            if hot_sectors and sector and sector not in hot_sectors:
                continue
            
            # ========== 构建信号 ==========
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            signal = PatternSignal(
                pattern_type="龙二波",
                stock_code=code,
                stock_name=name,
                confidence=0.82,
                description=f"近期{first_wave_info['max_boards']}板龙头，调整{days_since_peak}天后二波启动",
                key_metrics={
                    "第一波高度": first_wave_info['max_boards'],
                    "第一波日期": f"{first_wave_info['start_date']}至{first_wave_info['peak_date']}",
                    "调整天数": days_since_peak,
                    "涨停时间": today_row.get('首次封板时间', ''),
                    "所属板块": sector,
                    "封单强度": f"{today_row.get('封单额', 0) / (today_row.get('流通市值', 1) * 10000) * 100:.1f}%"
                },
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.15,
                position_size="medium",
                validation_rules=[
                    f"近15日内{first_wave_info['max_boards']}连板（真龙）",
                    f"调整{days_since_peak}天（记忆未散）",
                    "昨日未涨停（首板确认）",
                    "今日涨停启动",
                    "板块热度未退" if (hot_sectors and sector in hot_sectors) else "板块热度未知"
                ]
            )
            signals.append(signal)
        
        return signals
    
    def _rebuild_consecutive_from_pools(self, stock_code: str,
                                       recent_pools: Dict[str, pd.DataFrame]) -> Dict:
        """
        从近15日涨停池重建该股的连板记录
        返回：是否是近期龙头，第一波信息等
        """
        dates = sorted(recent_pools.keys())
        zt_dates = []  # 该股涨停的日期列表
        
        for date in dates:
            pool = recent_pools[date]
            if pool.empty or '代码' not in pool.columns:
                continue
            
            if stock_code in pool['代码'].values:
                zt_dates.append(date)
        
        if len(zt_dates) < 4:  # 至少4板才算真龙
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
        
        if max_boards < 4:  # 至少4板
            return {'is_valid': False, 'reason': '最大连板数不足'}
        
        # 检查是否是近期这一波（非开头几天）
        peak_date = max_group[-1]
        first_date = max_group[0]
        
        # 距离今天不能太久
        today = datetime.strptime(dates[-1], "%Y%m%d")
        peak = datetime.strptime(peak_date, "%Y%m%d")
        if (today - peak).days > 15:  # 第一波见顶距今超过15天
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
    
    def _calculate_days_between(self, start_date: str, end_date: str) -> int:
        """计算两个日期之间的天数"""
        try:
            start = datetime.strptime(start_date, "%Y%m%d")
            end = datetime.strptime(end_date, "%Y%m%d")
            return (end - start).days
        except:
            return 999
    
    def _calculate_board_height(self, code: str, today_row: pd.Series,
                                yesterday_df: pd.DataFrame,
                                day_before_yesterday_df: pd.DataFrame = None) -> int:
        """计算当前连板高度"""
        # 今日涨停？
        today_change = today_row.get('涨跌幅', 0)
        if isinstance(today_change, str):
            today_change = float(today_change.replace('%', ''))
        if today_change < 9.5:
            return 0
        
        height = 1  # 今日涨停，至少1板
        
        # 昨日涨停？
        if yesterday_df is not None and not yesterday_df.empty and '代码' in yesterday_df.columns:
            if code in yesterday_df['代码'].values:
                height += 1
                
                # 前日涨停？
                if day_before_yesterday_df is not None and not day_before_yesterday_df.empty:
                    if code in day_before_yesterday_df['代码'].values:
                        height += 1
        
        return height
    
    def _analyze_yesterday_weak(self, yesterday: pd.Series) -> Dict:
        """
        分析昨日"弱"的类型和质量
        关键：弱但有承接，非出货
        """
        blast_times = yesterday.get('炸板次数', 0)
        limit_up_time = yesterday.get('首次封板时间', '')
        last_seal_time = yesterday.get('最后封板时间', '')
        turnover = yesterday.get('换手率', 0)
        
        # 判断弱类型
        weak_type = ""
        if blast_times >= 2:
            weak_type = "烂板"
        elif last_seal_time and str(last_seal_time) > "14:30:00":
            weak_type = "尾盘板"
        elif yesterday.get('涨跌幅', 0) < 9.5:
            weak_type = "断板"
        else:
            return {'is_valid_weak': False, 'reason': '昨日不弱', 'score': 0}
        
        # 质量检查（必须有承接）
        score = 60  # 基础分
        
        # 换手充分（15-30%）
        if 15 <= turnover <= 30:
            score += 20
        elif turnover < 10:  # 换手不足，可能是庄股
            score -= 20
        
        # 烂板次数适中（2-4次）
        if 2 <= blast_times <= 4:
            score += 10
        elif blast_times > 5:  # 太烂
            score -= 30
        
        # 尾盘回封比不回封好
        if last_seal_time and str(last_seal_time) > "14:50:00":
            score += 10  # 尾盘回封说明有资金维护
        
        return {
            'is_valid_weak': score >= 60,
            'weak_type': weak_type,
            'score': score,
            'blast_times': blast_times,
            'turnover': turnover
        }
    
    def _calculate_minutes_from_open(self, time_str: str) -> int:
        """计算从9:30到涨停的分钟数"""
        try:
            if not time_str or time_str == '-':
                return 999
            
            time_str = str(time_str).strip()
            if time_str.isdigit():
                time_str = time_str.zfill(6)
            if len(time_str) == 6 and ':' not in time_str:
                time_str = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
            
            hour, minute = map(int, time_str.split(':')[:2])
            return (hour - 9) * 60 + (minute - 30)
        except:
            return 999
    
    def _calculate_time_diff(self, early_time: str, late_time: str) -> int:
        """计算时间差（分钟），early必须早于late"""
        try:
            fmt = "%H:%M:%S"
            
            # 处理各种格式
            early = str(early_time).strip()
            late = str(late_time).strip()
            
            if early.isdigit():
                early = early.zfill(6)
            if late.isdigit():
                late = late.zfill(6)
            
            if len(early) == 6 and ':' not in early:
                early = f"{early[:2]}:{early[2:4]}:{early[4:]}"
            if len(late) == 6 and ':' not in late:
                late = f"{late[:2]}:{late[2:4]}:{late[4:]}"
            
            early_dt = datetime.strptime(early, fmt)
            late_dt = datetime.strptime(late, fmt)
            diff = (late_dt - early_dt).total_seconds() / 60
            return int(diff) if diff > 0 else 0
        except:
            return 0
    
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
        
        # 8. 检测龙二波（需要近15日涨停池数据）
        logger.info("  准备龙二波检测数据（近15日涨停池）...")
        recent_pools = {}
        for i in range(15):
            try:
                date = self._get_date_offset(today_date, -i)
                pool = self.dm.get_limit_up_pool(date)
                if not pool.empty:
                    recent_pools[date] = pool
            except Exception as e:
                logger.warning(f"  获取{date}涨停池失败: {e}")
                continue
        
        if len(recent_pools) >= 5:  # 至少要有5天的数据
            results["龙二波"] = self.detect_dragon_second_wave(today_zt, recent_pools)
            logger.info(f"  龙二波: {len(results['龙二波'])}个（基于{len(recent_pools)}日数据）")
        else:
            logger.warning(f"  龙二波: 数据不足（仅{len(recent_pools)}日），跳过检测")
        
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
    
    def _get_date_offset(self, date_str: str, offset_days: int) -> str:
        """获取指定日期偏移后的日期"""
        from datetime import datetime, timedelta
        date = datetime.strptime(date_str, "%Y%m%d")
        new_date = date + timedelta(days=offset_days)
        return new_date.strftime("%Y%m%d")


if __name__ == "__main__":
    print("模式识别模块初始化成功")
    print("支持模式: 弱转强、二板定龙、首板突破、分歧转一致、卡位板、竞价爆量、炸板回封、龙二波")
