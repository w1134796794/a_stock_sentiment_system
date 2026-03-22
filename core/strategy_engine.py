"""
策略引擎 - 高确定性交易模式的程序化实现
包含：二板定龙、分歧转一致、首板突破、竞价爆量、炸板回封、龙二波
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger

class PatternType(Enum):
    WEAK_TO_STRONG = "弱转强"           # 已存在
    DRAGON_PULLBACK = "龙回头"          # 已存在
    POSITION_BATTLE = "卡位板"          # 已存在
    SECOND_BOARD_DRAGON = "二板定龙"    # 新增
    DIVERGENCE_TO_CONSENSUS = "分歧转一致" # 新增
    FIRST_BOARD_BREAKOUT = "首板突破"     # 新增
    AUCTION_VOLUME = "竞价爆量"          # 新增
    BLAST_RESEAL = "炸板回封"            # 新增
    DRAGON_SECOND_WAVE = "龙二波"         # 新增

@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str
    confidence: float  # 0-1
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str  # light/medium/heavy
    reason: str
    key_metrics: Dict
    validation_rules: List[str]  # 必须满足的条件列表

class StrategyEngine:
    def __init__(self, data_manager, sentiment_engine):
        self.dm = data_manager
        self.se = sentiment_engine
        self.today = datetime.now().strftime("%Y%m%d")
        
    # ==================== 1. 二板定龙（一进二） ====================
    def detect_second_board_dragon(self, yesterday_zt: pd.DataFrame, 
                                   today_data: pd.DataFrame,
                                   today_auction: pd.DataFrame) -> List[TradeSignal]:
        """
        二板定龙模式：首板硬逻辑 + 次日高开3-7% + 竞价量>10% + 分时坚决
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
            
            # 条件3: 竞价量 > 前日总成交10%
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
            
            signal = TradeSignal(
                pattern_type=PatternType.SECOND_BOARD_DRAGON,
                stock_code=code,
                stock_name=name,
                trigger_time=limit_up_time,
                confidence=0.85 if auction_vol_ratio > 0.15 else 0.75,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=entry_price * 1.10,  # +10%止盈
                position_size="medium" if auction_vol_ratio > 0.15 else "light",
                reason=f"首板硬逻辑+次日高开{open_gap*100:.1f}%+竞价量{auction_vol_ratio*100:.1f}%",
                key_metrics={
                    "首板封单质量": first_board_quality['score'],
                    "高开幅度": f"{open_gap*100:.2f}%",
                    "竞价量比": f"{auction_vol_ratio*100:.1f}%",
                    "涨停时间": limit_up_time,
                    "封单强度": f"{seal_strength*100:.1f}%"
                },
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
    
    # ==================== 2. 分歧转一致（日线级别） ====================
    def detect_divergence_to_consensus(self, hist_data: pd.DataFrame, 
                                       today_data: pd.Series,
                                       yesterday_data: pd.Series) -> Optional[TradeSignal]:
        """
        分歧转一致：三板烂板爆量分歧，次日弱转强上板
        """
        if hist_data.empty or len(hist_data) < 5:
            return None
        
        code = today_data.get('代码', '')
        name = today_data.get('名称', '')
        
        # 条件1: 昨日必须是烂板（炸板次数>0 或 开板）
        yesterday_bad = yesterday_data.get('炸板次数', 0) > 0 or                        yesterday_data.get('开板次数', 0) > 0
        if not yesterday_bad:
            return None
        
        # 条件2: 昨日爆量（成交量>前日2倍）
        yest_vol = yesterday_data.get('成交量', 0)
        prev_vol = hist_data.iloc[-2]['vol'] if len(hist_data) >= 2 else yest_vol
        if yest_vol < prev_vol * 1.5:
            return None
        
        # 条件3: 今日弱转强高开2%-5%
        open_price = today_data.get('开盘价', 0)
        yest_close = yesterday_data.get('收盘价', 0)
        gap_ratio = (open_price - yest_close) / yest_close
        if not (0.02 <= gap_ratio <= 0.05):
            return None
        
        # 条件4: 15分钟成交量达前日20%
        vol_15min = today_data.get('15分钟成交量', 0)
        if vol_15min < yest_vol * 0.20:
            return None
        
        # 条件5: 今日涨停（确认一致）
        if today_data.get('涨跌幅', 0) < 9.5:
            return None
        
        # 条件6: 不跌破关键支撑（5日线）
        ma5 = hist_data['close'].tail(5).mean()
        if open_price < ma5 * 0.98:
            return None
        
        entry_price = today_data.get('涨停价', 0)
        
        return TradeSignal(
            pattern_type=PatternType.DIVERGENCE_TO_CONSENSUS,
            stock_code=code,
            stock_name=name,
            trigger_time=today_data.get('首次封板时间', ''),
            confidence=0.80,
            entry_price=entry_price,
            stop_loss=entry_price * 0.93,  # 烂板后严格止损-7%
            take_profit=entry_price * 1.15,
            position_size="medium",
            reason=f"三板烂板后爆量分歧，次日弱转强高开{gap_ratio*100:.1f}%",
            key_metrics={
                "昨日炸板次数": yesterday_data.get('炸板次数', 0),
                "昨日成交量比": f"{yest_vol/prev_vol:.1f}倍",
                "今日高开": f"{gap_ratio*100:.1f}%",
                "15分钟量能比": f"{vol_15min/yest_vol*100:.1f}%",
                "5日线支撑": f"{ma5:.2f}"
            },
            validation_rules=[
                "昨日烂板（炸板>0）",
                "昨日爆量（>前日1.5倍）",
                "今日高开2%-5%",
                "15分钟量能>前日20%",
                "今日涨停确认",
                "不跌破5日线"
            ]
        )
    
    # ==================== 3. 首板突破（低位启动） ====================
    def detect_first_board_breakout(self, today_data: pd.Series,
                                    hist_30d: pd.DataFrame,
                                    sector_strength: float) -> Optional[TradeSignal]:
        """
        首板突破：突破关键压力位 + 早盘秒封 + 量能放大 + 主线题材
        """
        if hist_30d.empty or len(hist_30d) < 20:
            return None
        
        code = today_data.get('代码', '')
        name = today_data.get('名称', '')
        
        # 条件1: 突破关键压力位（前高/平台/年线）
        current_price = today_data.get('收盘价', 0)
        high_20d = hist_30d['high'].tail(20).max()
        high_60d = hist_30d['high'].tail(60).max()
        ma250 = hist_30d['close'].tail(250).mean() if len(hist_30d) >= 250 else high_60d
        
        breakout_level = max(high_20d, high_60d * 0.95, ma250)
        if current_price < breakout_level:
            return None
        
        # 条件2: 早盘秒封（9:40前）
        limit_up_time = today_data.get('首次封板时间', '')
        if not self._is_fast_limit_up(limit_up_time, max_minutes=40):
            return None
        
        # 条件3: 量能>前日1.5倍且>5日均量2倍
        today_vol = today_data.get('成交量', 0)
        prev_vol = hist_30d.iloc[-1]['vol'] if len(hist_30d) > 0 else today_vol
        vol_ma5 = hist_30d['vol'].tail(5).mean()
        
        if today_vol < prev_vol * 1.5 or today_vol < vol_ma5 * 2:
            return None
        
        # 条件4: 必须是主线题材（板块强度>阈值）
        if sector_strength < 0.6:  # 板块强度阈值
            return None
        
        # 条件5: 封单额>流通市值10%
        seal_amount = today_data.get('封单额', 0)
        float_cap = today_data.get('流通市值', 1) * 10000
        if seal_amount < float_cap * 0.10:
            return None
        
        entry_price = today_data.get('涨停价', current_price)
        
        return TradeSignal(
            pattern_type=PatternType.FIRST_BOARD_BREAKOUT,
            stock_code=code,
            stock_name=name,
            trigger_time=limit_up_time,
            confidence=0.75,
            entry_price=entry_price,
            stop_loss=entry_price * 0.93,
            take_profit=entry_price * 1.08,
            position_size="light",  # 首板确定性相对较低，轻仓
            reason=f"突破{breakout_level:.2f}压力位+早盘秒封+量能{today_vol/prev_vol:.1f}倍",
            key_metrics={
                "突破价位": f"{breakout_level:.2f}",
                "涨停时间": limit_up_time,
                "量能比": f"{today_vol/prev_vol:.1f}倍",
                "板块强度": f"{sector_strength:.2f}",
                "封单强度": f"{seal_amount/float_cap*100:.1f}%"
            },
            validation_rules=[
                "突破20日/60日/年线高点",
                "9:40前涨停",
                "量能>前日1.5倍且>5日均量2倍",
                "主线题材（板块强度>0.6）",
                "封单额>流通市值10%"
            ]
        )
    
    # ==================== 4. 竞价爆量战法 ====================
    def detect_auction_volume_surge(self, code: str, name: str,
                                    auction_data: Dict,
                                    prev_data: pd.Series) -> Optional[TradeSignal]:
        """
        竞价爆量：竞价量>前日5% + 价格持续抬高 + 买盘厚度>卖盘2倍
        """
        # 条件1: 竞价量>前日总成交5%-10%
        auction_vol = auction_data.get('竞价成交量', 0)
        prev_total_vol = prev_data.get('成交量', 1)
        vol_ratio = auction_vol / prev_total_vol
        
        if not (0.05 <= vol_ratio <= 0.20):  # 5%-20%最佳，过高可能是出货
            return None
        
        # 条件2: 竞价价格持续抬高（量价齐升）
        auction_prices = auction_data.get('竞价价格序列', [])
        if len(auction_prices) < 5:
            return None
        
        price_trend = np.polyfit(range(len(auction_prices)), auction_prices, 1)[0]
        if price_trend <= 0:  # 价格必须向上
            return None
        
        # 条件3: 买盘厚度>卖盘2倍
        bid_volume = auction_data.get('买盘量', 0)
        ask_volume = auction_data.get('卖盘量', 1)
        if bid_volume < ask_volume * 2:
            return None
        
        # 条件4: 高开幅度1%-7%
        open_price = auction_data.get('开盘价', 0)
        prev_close = prev_data.get('收盘价', 1)
        gap = (open_price - prev_close) / prev_close
        if not (0.01 <= gap <= 0.07):
            return None
        
        entry_price = open_price
        
        return TradeSignal(
            pattern_type=PatternType.AUCTION_VOLUME,
            stock_code=code,
            stock_name=name,
            trigger_time="09:25:00",
            confidence=0.80 if vol_ratio > 0.10 else 0.70,
            entry_price=entry_price,
            stop_loss=entry_price * 0.95,
            take_profit=entry_price * 1.08,
            position_size="medium" if vol_ratio > 0.10 else "light",
            reason=f"竞价量{vol_ratio*100:.1f}%+价格趋势向上+买盘>{ask_volume*2:.0f}倍",
            key_metrics={
                "竞价量比": f"{vol_ratio*100:.1f}%",
                "价格趋势": "向上" if price_trend > 0 else "向下",
                "买盘卖盘比": f"{bid_volume/ask_volume:.1f}:1",
                "高开幅度": f"{gap*100:.1f}%"
            },
            validation_rules=[
                "竞价量>前日5%",
                "竞价价格持续抬高",
                "买盘厚度>卖盘2倍",
                "高开1%-7%"
            ]
        )
    
    # ==================== 5. 炸板回封 ====================
    def detect_blast_reseal(self, today_intraday: pd.DataFrame,
                           today_summary: pd.Series) -> Optional[TradeSignal]:
        """
        炸板回封：早盘炸板后30分钟内放量回封
        """
        code = today_summary.get('代码', '')
        name = today_summary.get('名称', '')
        
        # 条件1: 今日必须涨停
        if today_summary.get('涨跌幅', 0) < 9.5:
            return None
        
        # 条件2: 有炸板记录
        blast_times = today_summary.get('炸板次数', 0)
        if blast_times == 0:
            return None
        
        # 条件3: 首次炸板时间在早盘（9:30-10:30）
        # 需要从分时数据判断
        first_blast_time = self._get_first_blast_time(today_intraday)
        if not first_blast_time or first_blast_time > "10:30:00":
            return None
        
        # 条件4: 回封时间在炸板后30分钟内
        reseal_time = today_summary.get('最后封板时间', '')
        blast_dt = datetime.strptime(first_blast_time, "%H:%M:%S")
        reseal_dt = datetime.strptime(reseal_time, "%H:%M:%S")
        if (reseal_dt - blast_dt).seconds > 1800:  # 30分钟
            return None
        
        # 条件5: 回封时放量（回封瞬间成交量>炸板时2倍）
        reseal_volume = self._get_reseal_volume(today_intraday, reseal_time)
        blast_volume = self._get_blast_volume(today_intraday, first_blast_time)
        if reseal_volume < blast_volume * 2:
            return None
        
        # 条件6: 炸板时不能放巨量下杀（避免出货）
        if blast_volume > today_summary.get('成交量', 1) * 0.30:  # 炸板量<全天30%
            return None
        
        entry_price = today_summary.get('涨停价', 0)
        
        return TradeSignal(
            pattern_type=PatternType.BLAST_RESEAL,
            stock_code=code,
            stock_name=name,
            trigger_time=reseal_time,
            confidence=0.75,
            entry_price=entry_price,
            stop_loss=entry_price * 0.95,
            take_profit=entry_price * 1.10,
            position_size="light",
            reason=f"早盘炸板{first_blast_time}后{reseal_time}放量回封",
            key_metrics={
                "首次炸板时间": first_blast_time,
                "回封时间": reseal_time,
                "回封/炸板量比": f"{reseal_volume/blast_volume:.1f}",
                "炸板次数": blast_times
            },
            validation_rules=[
                "早盘炸板（9:30-10:30）",
                "30分钟内回封",
                "回封放量>炸板时2倍",
                "炸板量<全天30%（非出货）"
            ]
        )
    
    # ==================== 6. 龙二波（大二波启动） ====================
    def detect_dragon_second_wave(self, code: str, name: str,
                                  hist_60d: pd.DataFrame,
                                  today_data: pd.Series,
                                  sector_hot: bool) -> Optional[TradeSignal]:
        """
        龙二波：历史龙头回踩10日线+RSI反弹+缩量后放量+板块热度不退
        """
        if hist_60d.empty or len(hist_60d) < 30:
            return None
        
        hist_60d = hist_60d.sort_values('trade_date')
        
        # 条件1: 历史必须有连板记录（曾>=3连板）
        hist_60d['is_limit_up'] = hist_60d['pct_change'] >= 9.5
        hist_60d['consecutive'] = hist_60d['is_limit_up'].rolling(3).sum()
        max_consecutive = hist_60d['consecutive'].max()
        if max_consecutive < 3:
            return None
        
        # 条件2: 回踩10日均线获支撑（不能有效跌破）
        hist_60d['MA10'] = hist_60d['close'].rolling(10).mean()
        latest = hist_60d.iloc[-1]
        prev = hist_60d.iloc[-2] if len(hist_60d) > 1 else latest
        
        touch_ma10 = abs(latest['close'] - latest['MA10']) / latest['MA10'] < 0.03
        if not touch_ma10:
            return None
        
        # 条件3: RSI(14)从超卖区(<30)反弹
        hist_60d['RSI'] = self._calculate_rsi(hist_60d['close'], 14)
        current_rsi = hist_60d.iloc[-1]['RSI']
        prev_rsi = hist_60d.iloc[-5]['RSI'] if len(hist_60d) >= 5 else current_rsi
        if not (prev_rsi < 35 and current_rsi > prev_rsi):
            return None
        
        # 条件4: 成交量极度萎缩后重新放大（地量后首板）
        vol_ma20 = hist_60d['vol'].tail(20).mean()
        min_vol_recent = hist_60d['vol'].tail(10).min()
        today_vol = today_data.get('成交量', 0)
        
        volume_shrink = min_vol_recent < vol_ma20 * 0.5  # 近期地量
        volume_rebound = today_vol > min_vol_recent * 2.0  # 今日放量
        
        if not (volume_shrink and volume_rebound):
            return None
        
        # 条件5: 今日首板启动（确认二波开始）
        if today_data.get('涨跌幅', 0) < 9.5:
            return None
        
        # 条件6: 板块热度未退
        if not sector_hot:
            return None
        
        entry_price = today_data.get('涨停价', 0)
        
        return TradeSignal(
            pattern_type=PatternType.DRAGON_SECOND_WAVE,
            stock_code=code,
            stock_name=name,
            trigger_time=today_data.get('首次封板时间', ''),
            confidence=0.78,
            entry_price=entry_price,
            stop_loss=latest['MA10'] * 0.98,  # 以10日线为止损
            take_profit=entry_price * 1.20,  # 二波空间较大
            position_size="medium",
            reason=f"历史{max_consecutive:.0f}连板后回踩10日线，RSI{prev_rsi:.1f}->{current_rsi:.1f}反弹",
            key_metrics={
                "历史最高连板": max_consecutive,
                "10日线支撑": f"{latest['MA10']:.2f}",
                "RSI变化": f"{prev_rsi:.1f} -> {current_rsi:.1f}",
                "地量后放量": f"{min_vol_recent/1e4:.0f}万 -> {today_vol/1e4:.0f}万",
                "板块热度": "热" if sector_hot else "冷"
            },
            validation_rules=[
                "历史>=3连板",
                "回踩10日线支撑",
                "RSI超卖反弹",
                "地量后放量首板",
                "板块热度未退"
            ]
        )
    
    # ==================== 辅助方法 ====================
    def _is_fast_limit_up(self, time_str: str, max_minutes: int = 15) -> bool:
        """判断是否快速涨停"""
        if not time_str or time_str == '-':
            return False
        try:
            hour, minute = map(int, time_str.split(':')[:2])
            total_minutes = hour * 60 + minute
            return total_minutes <= 9 * 60 + 30 + max_minutes  # 开盘后max_minutes分钟内
        except:
            return False
    
    def _get_auction_volume_ratio(self, code: str, auction_data: pd.DataFrame, 
                                  prev_row: pd.Series) -> float:
        """获取竞价量占前日总成交比例"""
        if auction_data.empty:
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
    
    def _get_first_blast_time(self, intraday_df: pd.DataFrame) -> str:
        """从分时数据获取首次炸板时间（简化实现）"""
        # 实际应从tick数据判断：涨停价打开的时间
        if intraday_df.empty:
            return ""
        # 模拟：找价格从涨停价回落的第一个时间点
        return "09:45:00"  # 占位实现
    
    def _get_reseal_volume(self, intraday_df: pd.DataFrame, reseal_time: str) -> float:
        """获取回封瞬间成交量"""
        return intraday_df[intraday_df['时间'] == reseal_time]['成交量'].values[0] if not intraday_df.empty else 0
    
    def _get_blast_volume(self, intraday_df: pd.DataFrame, blast_time: str) -> float:
        """获取炸板瞬间成交量"""
        return intraday_df[intraday_df['时间'] == blast_time]['成交量'].values[0] if not intraday_df.empty else 0

if __name__ == "__main__":
    print("策略引擎模块加载完成")
    print(f"支持模式: {[p.value for p in PatternType]}")
