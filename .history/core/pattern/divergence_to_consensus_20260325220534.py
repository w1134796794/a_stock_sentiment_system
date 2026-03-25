"""
分歧转一致策略 - 真正的弱转强博弈
核心：昨日烂板完成洗盘，今日竞价抢筹一致看涨
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import loguru

logger = loguru.logger

class PatternType(Enum):
    DIVERGENCE_TO_CONSENSUS = "分歧转一致"  # 日线级别

@dataclass
class TradeSignal:
    pattern_type: PatternType
    stock_code: str
    stock_name: str
    trigger_time: str      # 实际买点时间（竞价/开盘）
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: str
    reason: str
    key_metrics: Dict
    validation_rules: List[str]
    buy_timing: str        # 新增：明确买点时机（竞价/开盘/打板）

class DivergenceToConsensusStrategy:
    def __init__(self, data_manager, sector_engine):
        self.dm = data_manager
        self.sector_engine = sector_engine
        
        # 分歧转一致专用参数
        self.params = {
            "min_board_height": 3,          # 至少3板（高标才值得博弈）
            "max_yesterday_blast_times": 5,   # 昨日炸板次数<5（太烂的不行）
            "min_yesterday_turnover": 15,     # 昨日换手率>15%（充分换手）
            "max_yesterday_turnover": 35,     # 昨日换手率<35%（过度换手危险）
            "min_auction_gap": 0.02,          # 竞价高开>2%
            "max_auction_gap": 0.07,          # 竞价高开<7%（过高是加速非转强）
            "min_auction_volume_ratio": 0.08, # 竞价量>昨日8%（抢筹信号）
            "max_open_drop": 0.02,            # 开盘后回踩<2%（拒绝深V）
            "max_time_to_limit": 10,          # 开盘后10分钟内涨停
            "sector_support_threshold": 2     # 同板块至少2只跟风高开
        }
    
    def detect_divergence_to_consensus(self,
                                       stock_code: str,
                                       stock_name: str,
                                       board_height: int,           # 当前连板高度
                                       yesterday_data: pd.Series,  # 昨日行情
                                       yesterday_tick: pd.DataFrame, # 昨日分时（判断烂板质量）
                                       today_auction: Dict,         # 今日竞价数据
                                       today_tick: pd.DataFrame,    # 今日分时（实时监控）
                                       sector_stocks: List[str]     # 同板块其他股
                                       ) -> Optional[TradeSignal]:
        """
        检测分歧转一致机会（买点在竞价或开盘，非打板）
        """
        
        # ========== 前置条件：身份识别（必须是高标） ==========
        if board_height < self.params["min_board_height"]:
            return None  # 低标不值得博弈
        
        # ========== 条件1：昨日烂板质量检查（核心） ==========
        blast_times = yesterday_data.get('炸板次数', 0)
        if blast_times == 0 or blast_times > self.params["max_yesterday_blast_times"]:
            return None  # 没分歧 或 烂透了
        
        # 昨日分时质量：烂板时间、回封力度
        board_quality = self._analyze_yesterday_board_quality(yesterday_tick, yesterday_data)
        if not board_quality['is_quality_divergence']:
            return None  # 尾盘偷袭烂板、无承接烂板 排除
        
        # 昨日换手检查：充分但不过度
        turnover = yesterday_data.get('换手率', 0)
        if not (self.params["min_yesterday_turnover"] <= turnover <= self.params["max_yesterday_turnover"]):
            return None
        
        # 昨日量能：爆量但非天量
        vol_ratio = yesterday_data.get('成交量', 0) / yesterday_data.get('5日均量', 1)
        if vol_ratio < 1.5:  # 不够放量
            return None
        
        # ========== 条件2：今日竞价弱转强信号（最关键） ==========
        open_price = today_auction.get('开盘价', 0)
        yest_close = yesterday_data.get('收盘价', 0)
        gap_ratio = (open_price - yest_close) / yest_close
        
        # 高开范围：2%-7%（低于2%是弱，高于7%是加速一致，非分歧转强）
        if not (self.params["min_auction_gap"] <= gap_ratio <= self.params["max_auction_gap"]):
            return None
        
        # 竞价量能：抢筹信号（>昨日8%）
        auction_vol = today_auction.get('竞价成交量', 0)
        yest_total_vol = yesterday_data.get('成交量', 1)
        auction_vol_ratio = auction_vol / yest_total_vol
        
        if auction_vol_ratio < self.params["min_auction_volume_ratio"]:
            return None  # 竞价无量，非抢筹
        
        # 竞价价格走势：最后一分钟必须向上（拒绝高开低走）
        auction_price_trend = today_auction.get('价格趋势', [])
        if auction_price_trend and auction_price_trend[-1] < auction_price_trend[-2]:
            return None  # 竞价末端回落，资金犹豫
        
        # ========== 条件3：开盘即一致（不给你低吸机会） ==========
        # 开盘后迅速拉升，不回踩或回踩<2%
        first_5min_high = today_tick.head(5)['high'].max() if not today_tick.empty else open_price
        first_5min_low = today_tick.head(5)['low'].min() if not today_tick.empty else open_price
        
        max_drop = (open_price - first_5min_low) / open_price
        if max_drop > self.params["max_open_drop"]:
            return None  # 开盘深V，非一致
        
        # 开盘5分钟内冲击涨停
        limit_up_time = self._get_limit_up_time(today_tick, yesterday_data.get('涨停价', 0))
        if not limit_up_time:
            return None
        
        minutes_to_limit = self._calculate_minutes_from_open(limit_up_time)
        if minutes_to_limit > self.params["max_time_to_limit"]:
            return None  # 拉升太慢，非强一致
        
        # ========== 条件4：板块效应支持（防止孤龙） ==========
        sector_support = self._check_sector_support(sector_stocks, today_auction)
        if sector_support['follower_count'] < self.params["sector_support_threshold"]:
            return None  # 无板块支持，容易炸
        
        # ========== 条件5：龙虎榜验证（加分项） ==========
        dragon_tiger = self._check_yesterday_dragon_tiger(stock_code)
        
        # ========== 计算买点和风控 ==========
        # 买点1：竞价末段（9:24:30后，价格确定向上）
        # 买点2：开盘第一笔（9:30:00，迅速跟进）
        # 买点3：打板（备选，但已错过最佳位置）
        
        entry_price = open_price  # 默认开盘价介入
        buy_timing = "开盘"
        
        # 如果竞价完全符合，可提前到竞价
        if gap_ratio >= 0.03 and auction_vol_ratio >= 0.12:
            entry_price = today_auction.get('竞价匹配价', open_price)
            buy_timing = "竞价末段"
        
        # 止损：昨日最低价或-7%
        stop_loss = max(yesterday_data.get('最低价', 0), entry_price * 0.93)
        
        # 止盈：看板块强度，强板块看高到+15%，弱板块+8%
        take_profit = entry_price * (1.15 if sector_support['is_strong'] else 1.08)
        
        # 置信度计算
        confidence = 0.70
        confidence += 0.10 if dragon_tiger['has_big_player'] else 0  # 游资介入+10%
        confidence += 0.10 if sector_support['is_strong'] else 0    # 板块强+10%
        confidence += 0.05 if auction_vol_ratio > 0.15 else 0       # 竞价爆量+5%
        confidence += 0.05 if board_quality['late_resell'] else 0   # 尾盘回封+5%
        
        return TradeSignal(
            pattern_type=PatternType.DIVERGENCE_TO_CONSENSUS,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=limit_up_time,
            confidence=round(min(confidence, 0.95), 2),
            entry_price=round(entry_price, 2),
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            position_size="medium",
            reason=f"{board_height}板烂板后竞价弱转强，高开{gap_ratio*100:.1f}%竞价量{auction_vol_ratio*100:.1f}%，{buy_timing}介入",
            key_metrics={
                "连板高度": board_height,
                "昨日烂板质量": board_quality['score'],
                "昨日换手率": f"{turnover:.1f}%",
                "竞价高开": f"{gap_ratio*100:.1f}%",
                "竞价量比": f"{auction_vol_ratio*100:.1f}%",
                "开盘最大回踩": f"{max_drop*100:.1f}%",
                "涨停用时": f"{minutes_to_limit}分钟",
                "板块跟风数": sector_support['follower_count'],
                "游资介入": "是" if dragon_tiger['has_big_player'] else "否",
                "买点时机": buy_timing
            },
            validation_rules=[
                f"高标{board_height}板（身份）",
                "昨日烂板但质量合格（非偷袭）",
                f"昨日换手{turnover:.1f}%（充分换手）",
                f"竞价高开{gap_ratio*100:.1f}%（弱转强信号）",
                f"竞价量{auction_vol_ratio*100:.1f}%（抢筹）",
                f"开盘回踩<{self.params['max_open_drop']*100:.0f}%（一致性强）",
                f"{minutes_to_limit}分钟内涨停（速度）",
                f"板块跟风≥{self.params['sector_support_threshold']}只（不孤龙）"
            ],
            buy_timing=buy_timing
        )
    
    # ==================== 核心辅助方法 ====================
    
    def _analyze_yesterday_board_quality(self, tick_df: pd.DataFrame, daily_data: pd.Series) -> Dict:
        """
        分析昨日烂板质量
        优质烂板：盘中分歧、有承接、尾盘回封
        劣质烂板：尾盘偷袭开板、无承接下杀、一字炸板无回封
        """
        if tick_df.empty:
            return {'is_quality_divergence': False, 'score': 0, 'late_resell': False}
        
        # 获取昨日涨停价
        limit_price = daily_data.get('涨停价', 0)
        
        # 1. 烂板时间分布（盘中烂板优于尾盘烂板）
        blast_times = tick_df[tick_df['price'] < limit_price * 0.99]['time']
        if blast_times.empty:
            return {'is_quality_divergence': False, 'score': 0, 'late_resell': False}
        
        first_blast = blast_times.iloc[0]
        last_blast = blast_df.iloc[-1]
        
        # 尾盘偷袭（14:30后首次开板）质量差
        is_tail_blast = first_blast > "14:30:00"
        
        # 2. 回封质量（最后一次回封时间和力度）
        resell_times = tick_df[tick_df['price'] >= limit_price]['time']
        if resell_times.empty:
            return {'is_quality_divergence': False, 'score': 0, 'late_resell': False}
        
        last_resell = resell_times.iloc[-1]
        late_resell = last_resell > "14:50:00"  # 尾盘回封说明承接强
        
        # 3. 烂板次数（适中为佳，太多说明无承接）
        blast_count = len(blast_times)
        
        # 评分
        score = 60  # 基础分
        if not is_tail_blast:
            score += 20  # 盘中分歧加分
        if late_resell:
            score += 10  # 尾盘回封加分
        if 2 <= blast_count <= 4:
            score += 10  # 分歧次数适中
        
        return {
            'is_quality_divergence': score >= 70 and not is_tail_blast,
            'score': score,
            'late_resell': late_resell,
            'blast_count': blast_count,
            'is_tail_blast': is_tail_blast
        }
    
    def _check_sector_support(self, sector_stocks: List[str], today_auction: Dict) -> Dict:
        """
        检查板块效应：同板块其他股是否也高开（跟风）
        """
        if not sector_stocks:
            return {'follower_count': 0, 'is_strong': False}
        
        follower_high_open = 0
        for code in sector_stocks[:10]:  # 检查前10只同板块股
            stock_auction = today_auction.get(code, {})
            gap = stock_auction.get('高开幅度', 0)
            if gap > 0.02:  # 高开>2%
                follower_high_open += 1
        
        return {
            'follower_count': follower_high_open,
            'is_strong': follower_high_open >= 5  # 5只以上跟风视为强板块
        }
    
    def _check_yesterday_dragon_tiger(self, stock_code: str) -> Dict:
        """
        检查昨日龙虎榜是否有知名游资介入
        """
        # 实际应查询龙虎榜数据
        # 简化：假设有接口返回
        big_players = ['章盟主', '方新侠', '作手新一', '陈小群', '上塘路']
        
        # 模拟查询
        return {
            'has_big_player': False,  # 实际从龙虎榜数据判断
            'players': []
        }
    
    def _get_limit_up_time(self, tick_df: pd.DataFrame, limit_price: float) -> Optional[str]:
        """从分时数据获取首次涨停时间"""
        if tick_df.empty or limit_price == 0:
            return None
        
        limit_ticks = tick_df[tick_df['price'] >= limit_price * 0.995]
        if limit_ticks.empty:
            return None
        
        return limit_ticks.iloc[0]['time']
    
    def _calculate_minutes_from_open(self, time_str: str) -> int:
        """计算从开盘到涨停的分钟数"""
        try:
            hour, minute = map(int, time_str.split(':')[:2])
            return (hour - 9) * 60 + (minute - 30)  # 9:30开盘
        except:
            return 999

# ==================== 使用示例 ====================

if __name__ == "__main__":
    strategy = DivergenceToConsensusStrategy(None, None)
    
    # 模拟数据
    yesterday_data = pd.Series({
        '炸板次数': 3,
        '换手率': 22.5,
        '成交量': 5000000,
        '5日均量': 2000000,
        '收盘价': 10.0,
        '涨停价': 11.0,
        '最低价': 9.8
    })
    
    today_auction = {
        '开盘价': 10.5,
        '竞价成交量': 600000,  # 昨日12%
        '竞价匹配价': 10.5,
        '价格趋势': [10.2, 10.3, 10.5]  # 向上
    }
    
    print("分歧转一致策略初始化完成")
    print(f"参数配置: {strategy.params}")