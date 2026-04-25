"""
二板定龙头 - 真正的龙头筛选器
核心：首板硬逻辑 + 次日资金表态 + 板块地位确立
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import loguru

logger = loguru.logger


class PatternType(Enum):
    SECOND_BOARD_DRAGON = "二板定龙"


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
    buy_timing: str = ""  # 买点时机
    buy_strategy: str = ""  # 买点策略
    next_day_expectation: str = ""  # 次日预期


class SecondBoardDragonStrategy:
    def __init__(self, data_manager, sector_engine, mode="strict"):
        """
        data_manager: 数据管理器（DataManager）
        sector_engine: 板块热度引擎（可选）
        mode: 策略模式 - "strict"(严格模式) | "loose"(宽松模式)
        """
        self.dm = data_manager
        self.sector_engine = sector_engine
        self.mode = mode
        
        # 基础参数（新旧逻辑共用）
        self.params = {
            # 首板质量
            "min_seal_ratio": 0.08,        # 封单额>流通市值8%
            "ideal_turnover": (8, 20),     # 理想换手8-20%
            "min_concept_heat": 3,          # 首板当日概念涨停数≥3
            
            # 次日态度
            "min_gap": 0.02,                # 最低高开2%
            "max_gap": 0.08,                # 最高高开8%
            "min_auction_vol": 0.08,        # 竞价量>8%
            "min_auction_amount": 5000000,  # 竞价金额>500万
            
            # 分时坚决
            "max_time_to_limit": 15,        # 15分钟内涨停
            "min_seal_growth": 0.10,        # 封单持续增加，最终>10%
            
            # 板块地位
            "max_sector_second_board": 2    # 同板块最多2只二板
        }
        
        # 严格模式参数（新逻辑）
        self.strict_params = {
            # 竞价强度
            "min_gap": 0.05,                # 高开5%-8%
            "max_gap": 0.08,
            "min_auction_vol": 0.08,        # 竞价量能达首板成交量8%-15%
            "max_auction_vol": 0.15,
            
            # 分时质量
            "max_limit_up_time": "10:00",   # 10:00前封板
            "min_turnover": 0.15,           # 实际换手>15%
            
            # 板块梯队
            "min_sector_first_board": 1,    # 至少1家同板块首板助攻
            
            # 放弃信号阈值
            "skip_first_board_seal": True,  # 首板一字板放弃
            "skip_tail_board_time": "14:30", # 尾盘二板放弃
        }
    
    def detect_second_board_dragon(self,
                                   yesterday_zt: pd.DataFrame,      # 昨日首板池
                                   today_auction: pd.DataFrame,   # 今日竞价数据（9:25）
                                   today_tick: pd.DataFrame,      # 今日分时（实时）
                                   sector_mapping: Dict,           # 股票->板块映射
                                   today_first_board: pd.DataFrame = None  # 今日首板池（用于板块梯队）
                                   ) -> List[TradeSignal]:
        """
        二板定龙头：竞价定生死，开盘定地位
        买点：竞价末段或开盘第一笔，绝非打板！
        """
        signals = []
        
        # 前置：统计各板块二板数量（判断板块地位）
        sector_second_board_count = {}
        
        # 严格模式：统计各板块首板数量（板块梯队）
        sector_first_board_count = {}
        if self.mode == "strict" and today_first_board is not None:
            if '所属行业' in today_first_board.columns:
                sector_first_board_count = today_first_board['所属行业'].value_counts().to_dict()
            elif 'L2_Industry' in today_first_board.columns:
                sector_first_board_count = today_first_board['L2_Industry'].value_counts().to_dict()
            logger.info(f"[二板定龙] 板块首板统计: {len(sector_first_board_count)}个行业有首板")
        
        for _, yest_row in yesterday_zt.iterrows():
            code = str(yest_row['代码']).zfill(6)
            name = yest_row['名称']
            
            # ========== 前置过滤：今日是否二板 ==========
            today_row = today_auction[today_auction['代码'] == code]
            if today_row.empty:
                continue  # 今日没动静，放弃
            
            today_row = today_row.iloc[0]
            
            # 获取板块信息
            sector = sector_mapping.get(code, '未知')
            first_board_count = sector_first_board_count.get(sector, 0)
            
            # ========== 严格模式：放弃信号检查 ==========
            if self.mode == "strict":
                skip_reason = self._should_skip_stock(yest_row, today_row, first_board_count)
                if skip_reason:
                    logger.debug(f"[{code}] 放弃信号: {skip_reason}")
                    continue
            
            # 必须高开（低开直接排除，资金不认可）
            open_price = today_row['开盘价']
            yest_close = yest_row['收盘价']
            gap_ratio = (open_price - yest_close) / yest_close
            
            # 严格模式：高开5%-8%
            if self.mode == "strict":
                if gap_ratio < self.strict_params["min_gap"]:
                    logger.debug(f"[{code}] 高开不足: {gap_ratio*100:.1f}% < {self.strict_params['min_gap']*100:.0f}%")
                    continue
                if gap_ratio > self.strict_params["max_gap"]:
                    logger.debug(f"[{code}] 高开过多(一字板): {gap_ratio*100:.1f}% > {self.strict_params['max_gap']*100:.0f}%")
                    continue
            else:
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
            
            # ========== 条件3：板块地位（卡位优势） ==========
            # 检查是否是板块内前2个二板
            current_count = sector_second_board_count.get(sector, 0)
            if current_count >= self.params["max_sector_second_board"]:
                # 已经有2只二板了，这只可能是跟风
                is_leader = False
            else:
                is_leader = True
                sector_second_board_count[sector] = current_count + 1
            
            # ========== 严格模式：分时质量检查 ==========
            if self.mode == "strict":
                limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                if limit_up_time:
                    parsed = self._parse_time(limit_up_time)
                    if parsed:
                        hour, minute = parsed
                        max_hour = int(self.strict_params['max_limit_up_time'][:2])
                        max_minute = int(self.strict_params['max_limit_up_time'][3:5])
                        if hour > max_hour or (hour == max_hour and minute > max_minute):
                            logger.debug(f"[{code}] 封板过晚: {limit_up_time} > 10:00")
                            continue
                
                # 换手检查
                turnover = today_row.get('换手率', 0)
                if turnover < self.strict_params['min_turnover'] * 100:  # 转换为百分比
                    logger.debug(f"[{code}] 换手不足: {turnover:.1f}% < {self.strict_params['min_turnover']*100:.0f}%")
                    continue
            
            # ========== 条件4：分时坚决（开盘确认） ==========
            # 这里用tick数据实时监控，竞价阶段先标记候选
            tick_data = today_tick[today_tick['代码'] == code]
            
            # 计算买点
            entry_price, buy_timing = self._calculate_entry(
                gap_ratio, attitude['auction_vol_ratio'], today_row
            )
            
            # 严格模式：确定买点策略
            if self.mode == "strict":
                if is_leader and gap_ratio >= 0.05 and first_board_count >= 2:
                    buy_strategy = "主买点: 回封时扫板（确认资金态度）"
                    next_day_expectation = "正常: 一字板或T字板（龙头溢价）"
                elif is_leader:
                    buy_strategy = "次买点: 确定为板块最强二板，可提前扫板"
                    next_day_expectation = "正常: 高开5%+（龙头预期）"
                else:
                    buy_strategy = "观望: 跟风二板，谨慎参与"
                    next_day_expectation = "低于预期: 低开或高开<3% → 立即止损"
            else:
                buy_strategy = "主买点: 回封时扫板"
                next_day_expectation = "正常预期"
            
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
                    "首板助攻": f"{first_board_count}家",
                    "买点时机": buy_timing,
                    "买点策略": buy_strategy,
                    "次日预期": next_day_expectation
                },
                validation_rules=[
                    f"首板硬逻辑: {quality['hard_logic']}",
                    f"次日高开{gap_ratio*100:.1f}%（资金表态）",
                    f"竞价量{attitude['auction_vol_ratio']*100:.1f}%（抢筹）",
                    f"板块地位: {'龙头' if is_leader else '跟风（谨慎）'}",
                    f"首板助攻: {first_board_count}家",
                    "15分钟内涨停（盘中确认）"
                ],
                buy_timing=buy_timing,
                buy_strategy=buy_strategy,
                next_day_expectation=next_day_expectation
            )
            
            signals.append(signal)
        
        # 按置信度排序，只取前3（避免过多干扰）
        signals.sort(key=lambda x: x.confidence, reverse=True)
        return signals[:3]
    
    # ==================== 核心判断方法 ====================
    
    def _should_skip_stock(self, yest_row: pd.Series, today_row: pd.Series, 
                          sector_first_board_count: int = 0) -> Optional[str]:
        """
        检查放弃信号（严格模式）
        
        放弃信号（满足任一即放弃）：
        1. 首板一字板（无换手，筹码断层）
        2. 尾盘偷鸡二板（14:30后）
        3. 板块无首板助攻（独龙难飞）
        4. 竞价无量高开（诱多陷阱）- 无竞价数据时跳过
        
        Returns:
            str: 放弃原因，None表示不放弃
        """
        if self.mode != "strict":
            return None
            
        code = str(yest_row.get('代码', '')).zfill(6)
        
        # 1. 检查首板是否一字板
        break_count = yest_row.get('开板次数', 0)
        limit_type = yest_row.get('涨停类型', '')
        turnover = yest_row.get('换手率', 0)
        
        if self.strict_params.get('skip_first_board_seal', True):
            # 一字板特征：开板次数=0 且 涨停类型为1 且 换手极低
            if break_count == 0 and ('一字' in str(limit_type) or limit_type == '1'):
                return f"首板一字板(无换手)"
            # 换手<3%视为无充分换手
            if turnover < 3.0:
                return f"首板换手不足({turnover:.1f}%<3%)"
        
        # 2. 检查是否尾盘二板
        limit_up_time = str(today_row.get('首次封板时间', '')).strip()
        if limit_up_time:
            parsed = self._parse_time(limit_up_time)
            if parsed:
                hour, minute = parsed
                skip_time = self.strict_params.get('skip_tail_board_time', '14:30')
                skip_hour = int(skip_time[:2])
                skip_minute = int(skip_time[3:5])
                if hour > skip_hour or (hour == skip_hour and minute >= skip_minute):
                    return f"尾盘二板({limit_up_time})"
        
        # 3. 检查板块是否有首板助攻
        if sector_first_board_count < self.strict_params.get('min_sector_first_board', 1):
            return f"板块无首板助攻({sector_first_board_count}家)"
        
        # 4. 检查竞价数据（如果有的话）
        auction_vol = today_row.get('竞价成交量', 0)
        if auction_vol > 0:  # 有竞价数据才检查
            yest_vol = yest_row.get('成交量', 1)
            auction_ratio = auction_vol / yest_vol if yest_vol > 0 else 0
            if auction_ratio < self.strict_params.get('min_auction_vol', 0.08):
                return f"竞价无量({auction_ratio*100:.1f}%<8%)"
        
        return None
    
    def _parse_time(self, time_str: str) -> Optional[Tuple[int, int]]:
        """
        统一解析时间字符串为 (hour, minute)
        支持格式: HHMMSS, HMMSS, HHMM, HH:MM:SS, HH:MM
        """
        if not time_str or time_str == '-':
            return None
        
        try:
            cleaned = str(time_str).strip().replace(':', '')
            
            if len(cleaned) == 6:  # HHMMSS
                return (int(cleaned[:2]), int(cleaned[2:4]))
            elif len(cleaned) == 5:  # HMMSS
                return (int(cleaned[0]), int(cleaned[1:3]))
            elif len(cleaned) == 4:  # HHMM
                return (int(cleaned[:2]), int(cleaned[2:4]))
            elif len(cleaned) == 3:  # HMM
                return (int(cleaned[0]), int(cleaned[1:3]))
        except Exception as e:
            logger.debug(f"解析时间失败: {time_str}, {e}")
        
        return None
    
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