"""
二板定龙头 - 真正的龙头筛选器
核心：首板硬逻辑 + 次日资金表态 + 板块地位确立
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
import loguru

logger = loguru.logger

class SecondBoardDragonStrategy:
    def __init__(self, data_manager, sector_engine):
        self.dm = data_manager
        self.sector_engine = sector_engine
        
        # 核心参数（经过回测优化）
        self.params = {
            # 首板质量
            "min_seal_ratio": 0.08,        # 封单额>流通市值8%（放宽到8%，10%太严）
            "ideal_turnover": (8, 20),     # 理想换手8-20%（根据市值调整）
            "min_concept_heat": 3,          # 首板当日概念涨停数≥3（有板块效应）
            
            # 次日态度（最关键）
            "min_gap": 0.02,                # 最低高开2%（低开直接放弃）
            "max_gap": 0.08,                # 最高高开8%（一字板不给机会）
            "min_auction_vol": 0.08,        # 竞价量>8%
            "min_auction_amount": 5000000,  # 竞价金额>500万（小盘股）
            
            # 分时坚决
            "max_time_to_limit": 15,        # 15分钟内涨停
            "min_seal_growth": 0.10,        # 封单持续增加，最终>10%
            
            # 板块地位（新增核心）
            "max_sector_second_board": 2    # 同板块最多2只二板，要做第一个
        }
    
    def detect_second_board_dragon(self,
                                   yesterday_zt: pd.DataFrame,      # 昨日首板池
                                   today_auction: pd.DataFrame,   # 今日竞价数据（9:25）
                                   today_tick: pd.DataFrame,      # 今日分时（实时）
                                   sector_mapping: Dict            # 股票->板块映射
                                   ) -> List[TradeSignal]:
        """
        二板定龙头：竞价定生死，开盘定地位
        买点：竞价末段或开盘第一笔，绝非打板！
        """
        signals = []
        
        # 前置：统计各板块二板数量（判断板块地位）
        sector_second_board_count = {}
        
        for _, yest_row in yesterday_zt.iterrows():
            code = yest_row['代码']
            name = yest_row['名称']
            
            # ========== 前置过滤：今日是否二板 ==========
            today_row = today_auction[today_auction['代码'] == code]
            if today_row.empty:
                continue  # 今日没动静，放弃
            
            today_row = today_row.iloc[0]
            
            # 必须高开（低开直接排除，资金不认可）
            open_price = today_row['开盘价']
            yest_close = yest_row['收盘价']
            gap_ratio = (open_price - yest_close) / yest_close
            
            if gap_ratio < self.params["min_gap"]:
                continue  # 低开=资金不认可，直接放弃
            
            # ========== 条件1：首板质量（硬逻辑） ==========
            quality = self._check_first_board_quality(yest_row)
            if not quality['is_hard_logic']:
                continue
            
            # ========== 条件2：次日资金态度（核心） ==========
            attitude = self._check_fund_attitude(today_row, yest_row)
            if not attitude['is_strong_attitude']:
                continue
            
            # ========== 条件3：板块地位（新增核心） ==========
            sector = sector_mapping.get(code, '未知')
            
            # 检查是否是板块内前2个二板
            current_count = sector_second_board_count.get(sector, 0)
            if current_count >= self.params["max_sector_second_board"]:
                # 已经有2只二板了，这只可能是跟风
                is_leader = False
            else:
                is_leader = True
                sector_second_board_count[sector] = current_count + 1
            
            # ========== 条件4：分时坚决（开盘确认） ==========
            # 这里用tick数据实时监控，竞价阶段先标记候选
            tick_data = today_tick[today_tick['代码'] == code]
            
            # 计算买点
            entry_price, buy_timing = self._calculate_entry(
                gap_ratio, attitude['auction_vol_ratio'], today_row
            )
            
            # 构建信号（竞价阶段输出，盘中确认）
            signal = TradeSignal(
                pattern_type=PatternType.SECOND_BOARD_DRAGON,
                stock_code=code,
                stock_name=name,
                trigger_time="09:25:00" if buy_timing == "竞价" else "09:30:00",
                confidence=self._calculate_confidence(quality, attitude, is_leader),
                entry_price=entry_price,
                stop_loss=entry_price * 0.95,
                take_profit=entry_price * 1.12,  # 二板后看高一线
                position_size="heavy" if is_leader and attitude['auction_vol_ratio'] > 0.15 else "medium",
                reason=self._generate_reason(quality, attitude, is_leader, gap_ratio),
                key_metrics={
                    "首板质量分": quality['score'],
                    "硬逻辑": quality['hard_logic'],
                    "次日高开": f"{gap_ratio*100:.1f}%",
                    "竞价量比": f"{attitude['auction_vol_ratio']*100:.1f}%",
                    "竞价金额": f"{attitude['auction_amount']/1e4:.0f}万",
                    "板块地位": "龙头" if is_leader else "跟风",
                    "板块内排名": current_count + 1,
                    "买点时机": buy_timing
                },
                validation_rules=[
                    f"首板硬逻辑: {quality['hard_logic']}",
                    f"次日高开{gap_ratio*100:.1f}%（资金表态）",
                    f"竞价量{attitude['auction_vol_ratio']*100:.1f}%（抢筹）",
                    f"板块地位: {'龙头' if is_leader else '跟风（谨慎）'}",
                    "15分钟内涨停（盘中确认）"
                ],
                buy_timing=buy_timing
            )
            
            signals.append(signal)
        
        # 按置信度排序，只取前3（避免过多干扰）
        signals.sort(key=lambda x: x.confidence, reverse=True)
        return signals[:3]
    
    # ==================== 核心判断方法 ====================
    
    def _check_first_board_quality(self, row: pd.Series) -> Dict:
        """
        检查首板是否有"硬逻辑"——政策/业绩/重大事件驱动
        """
        seal_amount = row.get('封单额', 0)
        float_cap = row.get('流通市值', 1) * 10000
        turnover = row.get('换手率', 0)
        concept = row.get('所属概念', '')
        limit_up_time = row.get('首次封板时间', '')
        
        # 硬逻辑识别（从概念和新闻判断）
        hard_logics = []
        
        # 政策驱动
        policy_keywords = ['政策', '利好', '规划', '补贴', '国产替代']
        if any(kw in concept for kw in policy_keywords):
            hard_logics.append('政策驱动')
        
        # 业绩驱动
        if '业绩' in concept or row.get('业绩预告', '') != '':
            hard_logics.append('业绩预增')
        
        # 事件驱动
        event_keywords = ['订单', '中标', '合作', '突破', '量产']
        if any(kw in concept for kw in event_keywords):
            hard_logics.append('事件催化')
        
        # 板块龙头（首板时就是板块内第一个涨停）
        if row.get('板块内排名', 99) == 1:
            hard_logics.append('板块龙头')
        
        # 封单质量（硬逻辑必须有资金认可）
        seal_ratio = seal_amount / float_cap
        seal_score = 40 if seal_ratio > 0.15 else (30 if seal_ratio > 0.08 else 0)
        
        # 换手质量（根据市值调整）
        market_cap = row.get('总市值', 50)  # 亿元
        if market_cap < 100:  # 小盘
            turnover_score = 30 if 5 <= turnover <= 25 else 20
        elif market_cap < 500:  # 中盘
            turnover_score = 30 if 8 <= turnover <= 20 else 20
        else:  # 大盘
            turnover_score = 30 if 10 <= turnover <= 15 else 20
        
        # 涨停时间（早盘板加分）
        time_score = 0
        if limit_up_time and limit_up_time < "10:00:00":
            time_score = 30
        elif limit_up_time and limit_up_time < "11:30:00":
            time_score = 20
        
        total_score = seal_score + turnover_score + time_score + (20 if hard_logics else 0)
        
        return {
            'is_hard_logic': total_score >= 70 and len(hard_logics) > 0,
            'score': total_score,
            'hard_logic': ' + '.join(hard_logics) if hard_logics else '技术反弹',
            'seal_ratio': seal_ratio,
            'turnover': turnover
        }
    
    def _check_fund_attitude(self, today_row: pd.Series, yest_row: pd.Series) -> Dict:
        """
        检查资金次日态度——是否愿意承担隔夜风险抢筹
        这是二板定龙头的核心！
        """
        # 竞价量（最重要指标）
        auction_vol = today_row.get('竞价成交量', 0)
        yest_total_vol = yest_row.get('成交量', 1)
        auction_vol_ratio = auction_vol / yest_total_vol
        
        # 竞价金额（防止小量高价误导）
        auction_amount = today_row.get('竞价成交额', auction_vol * today_row['开盘价'])
        
        # 竞价走势（最后一分钟必须向上）
        price_trend = today_row.get('竞价价格序列', [])
        last_min_trend = price_trend[-2:] if len(price_trend) >= 2 else []
        is_end_up = len(last_min_trend) == 2 and last_min_trend[1] > last_min_trend[0]
        
        # 综合判断
        is_strong = (
            auction_vol_ratio >= self.params["min_auction_vol"] and
            auction_amount >= self.params["min_auction_amount"] and
            is_end_up
        )
        
        return {
            'is_strong_attitude': is_strong,
            'auction_vol_ratio': auction_vol_ratio,
            'auction_amount': auction_amount,
            'is_end_up': is_end_up,
            'score': auction_vol_ratio * 100 + (20 if is_end_up else 0)
        }
    
    def _calculate_entry(self, gap_ratio: float, auction_vol_ratio: float, today_row: pd.Series) -> Tuple[float, str]:
        """
        计算买点：竞价还是开盘
        """
        # 强一致：高开>5% + 竞价量>12%，直接竞价挂涨停价
        if gap_ratio > 0.05 and auction_vol_ratio > 0.12:
            return today_row['涨停价'], "竞价"
        
        # 弱转强：高开2-5%，竞价量8-12%，开盘观察第一笔
        elif 0.02 <= gap_ratio <= 0.05 and 0.08 <= auction_vol_ratio <= 0.15:
            return today_row['开盘价'], "开盘"
        
        # 其他情况：放弃或盘中确认（非核心买点）
        else:
            return today_row['最新价'], "观察"
    
    def _calculate_confidence(self, quality: Dict, attitude: Dict, is_leader: bool) -> float:
        """计算置信度"""
        base = 0.70
        base += 0.10 if quality['score'] > 80 else 0.05
        base += 0.10 if attitude['auction_vol_ratio'] > 0.15 else 0.05
        base += 0.10 if is_leader else 0
        base += 0.05 if attitude['is_end_up'] else 0
        return round(min(base, 0.95), 2)
    
    def _generate_reason(self, quality: Dict, attitude: Dict, is_leader: bool, gap: float) -> str:
        """生成交易理由"""
        status = "龙头" if is_leader else "跟风"
        strength = "强一致" if gap > 0.05 else "弱转强"
        return f"{status}二板{strength}，{quality['hard_logic']}，竞价量{attitude['auction_vol_ratio']*100:.1f}%"

# ==================== 实战口诀 ====================

"""
二板定龙头，竞价定生死
首板要硬板，换手要合理
次日高开抢，资金表了态
板块第一名，重仓往里干
跟风二板少，容易被人卡
竞价不抢筹，开盘就放弃
十五分上板，确立真龙头
"""

if __name__ == "__main__":
    print("二板定龙头策略加载完成")
    print("核心：首板硬逻辑 + 次日资金表态 + 板块地位确立")