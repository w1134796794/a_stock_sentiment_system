"""
弱转强策略 - 超预期博弈
核心：昨日烂板/断板，今日竞价高开爆量，资金主动表态
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, time
import loguru

logger = loguru.logger

class WeakToStrongStrategy:
    def __init__(self, data_manager, sector_engine):
        self.dm = data_manager
        self.se = sector_engine
        
        # 核心参数（超预期标准）
        self.params = {
            # 身份识别（必须是高标）
            "min_board_height": 3,           # 至少3板（有辨识度）
            "max_board_height": 8,           # 不超过8板（太高风险大）
            
            # 昨日"弱"的标准（烂板/断板）
            "weak_types": ["烂板", "断板", "尾盘板"],
            "max_blast_times": 5,            # 烂板次数<5（太烂不行）
            "min_yesterday_turnover": 15,    # 昨日换手>15%（充分分歧）
            
            # 今日"强"的标准（超预期）
            "min_gap": 0.02,                 # 高开>2%（超预期起点）
            "ideal_gap": 0.04,               # 理想高开4%
            "max_gap": 0.07,                 # 高开<7%（太高是加速，非转强）
            "min_auction_vol_ratio": 0.08,   # 竞价量>8%（资金抢筹）
            "min_auction_amount": 5000000,   # 竞价金额>500万
            
            # 确认标准（开盘后）
            "max_time_to_limit": 10,         # 10分钟内涨停（强的坚决）
            "max_open_drop": 0.02,           # 开盘回踩<2%（拒绝深V）
        }
    
    def detect_weak_to_strong(self,
                               stock_code: str,
                               stock_name: str,
                               board_height: int,              # 当前连板高度
                               yesterday_data: pd.Series,       # 昨日行情
                               yesterday_tick: pd.DataFrame,     # 昨日分时
                               today_auction: Dict,              # 今日竞价
                               today_tick: pd.DataFrame,        # 今日分时
                               sector_leader: bool               # 是否板块龙头
                               ) -> Optional[TradeSignal]:
        """
        检测弱转强机会
        买点：竞价末段或开盘第一笔（绝非打板）
        """
        
        # ========== 条件1：身份识别（高标才值得博弈）==========
        if not (self.params["min_board_height"] <= board_height <= self.params["max_board_height"]):
            return None
        
        # 必须是板块龙头或市场高标
        if not sector_leader and board_height < 5:
            return None  # 跟风股弱转强=自救，无接力
        
        # ========== 条件2：昨日"弱"的质量（烂板但有承接）==========
        weak_quality = self._analyze_yesterday_weak(
            yesterday_data, yesterday_tick
        )
        
        if not weak_quality['is_valid_weak']:
            return None  # 昨日不是弱，或弱得太彻底（出货）
        
        # ========== 条件3：今日"强"的态度（竞价超预期）==========
        strong_attitude = self._check_today_strong_attitude(
            today_auction, yesterday_data
        )
        
        if not strong_attitude['is_strong_attitude']:
            return None  # 竞价不强，不符合预期
        
        # ========== 条件4：开盘后确认（不回踩，快速上板）==========
        open_confirm = self._check_open_confirmation(today_tick, today_auction)
        
        if not open_confirm['is_confirmed']:
            # 竞价符合，但开盘走弱，放弃
            return None
        
        # ========== 计算买点和风控 ==========
        # 买点1：竞价末段（9:24:30后，价格确定向上）
        # 买点2：开盘第一笔（9:30:00，迅速跟进）
        
        entry_price, buy_timing = self._calculate_entry(
            today_auction, strong_attitude
        )
        
        # 止损：昨日最低价或-7%
        stop_loss = max(
            yesterday_data.get('最低价', entry_price * 0.93),
            entry_price * 0.93
        )
        
        # 止盈：看板块强度，强板块看高到+15%
        take_profit = entry_price * 1.15
        
        return TradeSignal(
            pattern_type=PatternType.WEAK_TO_STRONG,
            stock_code=stock_code,
            stock_name=stock_name,
            trigger_time=buy_timing,
            confidence=strong_attitude['confidence'],
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size="heavy" if strong_attitude['gap'] >= 0.04 else "medium",
            reason=f"{board_height}板{weak_quality['weak_type']}后，"
                   f"次日高开{strong_attitude['gap']*100:.1f}%竞价量{strong_attitude['auction_vol_ratio']*100:.1f}%，"
                   f"{buy_timing}介入",
            key_metrics={
                "连板高度": board_height,
                "昨日弱类型": weak_quality['weak_type'],
                "昨日烂板质量": weak_quality['score'],
                "次日高开": f"{strong_attitude['gap']*100:.1f}%",
                "竞价量比": f"{strong_attitude['auction_vol_ratio']*100:.1f}%",
                "竞价金额": f"{strong_attitude['auction_amount']/1e4:.0f}万",
                "开盘回踩": f"{open_confirm['max_drop']*100:.1f}%" if open_confirm else "N/A",
                "涨停用时": f"{open_confirm['time_to_limit']}分钟" if open_confirm else "N/A",
                "买点时机": buy_timing
            },
            validation_rules=[
                f"{board_height}板高标（身份）",
                f"昨日{weak_quality['weak_type']}（弱）",
                f"次日高开{strong_attitude['gap']*100:.1f}%（超预期）",
                f"竞价量{strong_attitude['auction_vol_ratio']*100:.1f}%（资金抢筹）",
                "开盘不回踩，快速上板（确认强）"
            ],
            buy_timing=buy_timing
        )
    
    # ==================== 核心判断方法 ====================
    
    def _analyze_yesterday_weak(self, yesterday: pd.Series, tick: pd.DataFrame) -> Dict:
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
        elif last_seal_time and last_seal_time > "14:30:00":
            weak_type = "尾盘板"
        elif yesterday.get('涨跌幅', 0) < 9.5:
            weak_type = "断板"
        else:
            return {'is_valid_weak': False, 'reason': '昨日不弱'}
        
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
        if last_seal_time and last_seal_time > "14:50:00":
            score += 10  # 尾盘回封说明有资金维护
        
        # 检查分时承接（从tick数据）
        if not tick.empty:
            limit_price = yesterday.get('涨停价', tick['price'].max())
            # 炸板后是否快速回拉
            blast_period = tick[tick['price'] < limit_price * 0.99]
            if not blast_period.empty:
                # 炸板后最低价是否远离跌停
                min_price = blast_period['price'].min()
                if min_price > limit_price * 0.95:  # 没砸太深
                    score += 10
        
        return {
            'is_valid_weak': score >= 60,
            'weak_type': weak_type,
            'score': score,
            'blast_times': blast_times,
            'turnover': turnover
        }
    
    def _check_today_strong_attitude(self, auction: Dict, yesterday: pd.Series) -> Dict:
        """
        检查今日竞价是否"强态度"——超预期
        核心：高开+爆量，资金隔夜抢筹
        """
        open_price = auction.get('开盘价', 0)
        yest_close = yesterday.get('收盘价', 1)
        gap = (open_price - yest_close) / yest_close
        
        # 高开范围：2%-7%（低于2%是符合预期，高于7%是加速）
        if not (self.params["min_gap"] <= gap <= self.params["max_gap"]):
            return {'is_strong_attitude': False, 'reason': f'高开{gap*100:.1f}%不符合'}
        
        # 竞价量
        auction_vol = auction.get('竞价成交量', 0)
        yest_vol = yesterday.get('成交量', 1)
        auction_vol_ratio = auction_vol / yest_vol if yest_vol > 0 else 0
        
        if auction_vol_ratio < self.params["min_auction_vol_ratio"]:
            return {'is_strong_attitude': False, 'reason': f'竞价量{auction_vol_ratio*100:.1f}%不足'}
        
        # 竞价金额（防止小盘股误导）
        auction_amount = auction.get('竞价成交额', auction_vol * open_price)
        if auction_amount < self.params["min_auction_amount"]:
            return {'is_strong_attitude': False, 'reason': '竞价金额不足'}
        
        # 竞价走势（最后一分钟向上）
        price_trend = auction.get('价格序列', [])
        if len(price_trend) >= 2 and price_trend[-1] < price_trend[-2]:
            return {'is_strong_attitude': False, 'reason': '竞价末端回落'}
        
        # 置信度计算
        confidence = 0.70
        if gap >= 0.04:
            confidence += 0.15  # 高开4%以上，强超预期
        if auction_vol_ratio >= 0.12:
            confidence += 0.10  # 竞价量12%以上，抢筹明显
        
        return {
            'is_strong_attitude': True,
            'gap': gap,
            'auction_vol_ratio': auction_vol_ratio,
            'auction_amount': auction_amount,
            'confidence': round(min(confidence, 0.95), 2)
        }
    
    def _check_open_confirmation(self, tick: pd.DataFrame, auction: Dict) -> Dict:
        """
        检查开盘后是否确认强（不回踩，快速上板）
        """
        if tick.empty:
            return {'is_confirmed': False}
        
        open_price = auction.get('开盘价', tick.iloc[0]['price'] if not tick.empty else 0)
        
        # 开盘后5分钟数据
        first_5min = tick.head(5)
        if first_5min.empty:
            return {'is_confirmed': False}
        
        # 最大回踩
        min_price = first_5min['price'].min()
        max_drop = (open_price - min_price) / open_price
        
        # 拒绝深V（回踩>2%是弱）
        if max_drop > self.params["max_open_drop"]:
            return {'is_confirmed': False, 'reason': f'开盘回踩{max_drop*100:.1f}%太深'}
        
        # 涨停时间
        limit_price = tick['price'].max()
        limit_ticks = tick[tick['price'] >= limit_price * 0.995]
        if limit_ticks.empty:
            return {'is_confirmed': False, 'reason': '未涨停'}
        
        first_limit_time = limit_ticks.iloc[0]['time']
        minutes_to_limit = self._calculate_minutes_from_open(first_limit_time)
        
        # 10分钟内涨停
        if minutes_to_limit > self.params["max_time_to_limit"]:
            return {'is_confirmed': False, 'reason': f'{minutes_to_limit}分钟才涨停，太慢'}
        
        return {
            'is_confirmed': True,
            'max_drop': max_drop,
            'time_to_limit': minutes_to_limit
        }
    
    def _calculate_entry(self, auction: Dict, attitude: Dict) -> Tuple[float, str]:
        """
        计算买点：竞价还是开盘
        """
        gap = attitude['gap']
        vol_ratio = attitude['auction_vol_ratio']
        
        # 强超预期：高开4%+竞价量12%，直接竞价买
        if gap >= 0.04 and vol_ratio >= 0.12:
            return auction.get('涨停价', auction['开盘价']), "竞价末段"
        
        # 一般超预期：高开2-4%，竞价量8-12%，开盘买
        return auction['开盘价'], "开盘第一笔"
    
    def _calculate_minutes_from_open(self, time_str: str) -> int:
        """计算从9:30到涨停的分钟数"""
        try:
            hour, minute = map(int, time_str.split(':')[:2])
            return (hour - 9) * 60 + (minute - 30)
        except:
            return 999

# ==================== 实战口诀 ====================

"""
弱转强，超预期
昨日烂板是表象，今日高开是真相
竞价定生死，开盘定强弱
高开2%是起点，4%是理想，7%是上限
竞价量8%是及格，12%是优秀
开盘不回踩，10分钟上板，才是真的强

杂毛股，不弱转强，因为无人接力
真龙头，才值得博弈，资金记忆还在

买点就两个：
竞价末段（强超预期）
开盘第一笔（一般超预期）

打板买？已经晚了，一致后无溢价
低开买？那是符合预期，不是超预期
"""

if __name__ == "__main__":
    print("弱转强策略加载完成")
    print("核心：超预期 = 昨日烂板/断板 + 今日竞价高开爆量")
    print("买点：竞价末段或开盘第一笔，绝非打板")
    print("关键：只有龙头才值得博弈，跟风股弱转强是自救")