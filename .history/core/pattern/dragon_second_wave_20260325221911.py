"""
龙二波策略 - 情绪周期+资金记忆+新催化的共振
核心：前龙+情绪转暖+新催化，非单纯技术反弹
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger

class SecondWaveStage(Enum):
    NOT_READY = "未就绪"       # 调整不充分
    WATCHING = "观察期"        # 条件具备，等信号
    ENTRY_DAY = "启动日"       # 今日首板，可介入
    MISSED = "已错过"          # 已涨停，等分歧

@dataclass
class SecondWaveSignal:
    stage: SecondWaveStage
    stock_code: str
    stock_name: str
    confidence: float
    entry_timing: str      # 明确介入时机
    reason: str
    key_metrics: Dict
    risk_warning: str      # 风险提示

class DragonSecondWaveStrategy:
    def __init__(self, data_manager, sentiment_engine, news_engine):
        self.dm = data_manager
        self.se = sentiment_engine
        self.news = news_engine
        
        # 核心参数（情绪周期+技术形态+催化）
        self.params = {
            # 身份识别（必须是前龙）
            "min_first_wave_height": 5,      # 第一波至少5板（真龙）
            "max_time_since_peak": 20,        # 见顶后20日内（记忆未散）
            "min_sector_leader_score": 80,   # 板块龙头评分>80
            
            # 情绪周期（最关键）
            "market_sentiment_turn": "冰点转暖",  # 必须是情绪转暖第一天
            "sector_heat_3d": 5,              # 板块3日涨停数>=5（热度未退）
            
            # 技术形态（调整充分）
            "adjust_depth_min": 0.15,         # 调整幅度>15%（充分洗盘）
            "adjust_depth_max": 0.35,         # 调整幅度<35%（未破位）
            "adjust_days_min": 5,             # 调整天数>5天（时间充分）
            "adjust_days_max": 15,            # 调整天数<15天（记忆未散）
            
            # 支撑位置（多均线）
            "support_ma": ["MA5", "MA10", "MA20"],  # 任一均线支撑
            "max_distance_from_ma": 0.03,     # 距均线<3%
            
            # 量能（筹码沉淀）
            "min_shrink_ratio": 0.30,         # 缩量至30%以下（地量）
            "rebound_volume_ratio": 1.5,      # 反弹放量1.5倍
            
            # 催化（新逻辑）
            "news_score_threshold": 60,       # 新闻热度>60
        }
    
    def analyze_second_wave_potential(self,
                                     stock_code: str,
                                     stock_name: str,
                                     first_wave_data: Dict,    # 第一波数据
                                     adjust_data: pd.DataFrame,  # 调整期数据
                                     today_data: pd.Series,     # 今日数据
                                     market_sentiment: str,     # 市场情绪
                                     sector_data: Dict,         # 板块数据
                                     news_data: List[Dict]       # 新闻催化
                                     ) -> SecondWaveSignal:
        """
        分析龙二波潜力，明确介入时机（启动前/启动日）
        """
        
        # ========== 条件1：身份识别（必须是前一波真龙） ==========
        identity = self._verify_dragon_identity(stock_code, first_wave_data)
        if not identity['is_dragon']:
            return SecondWaveSignal(
                stage=SecondWaveStage.NOT_READY,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.3,
                entry_timing="不介入",
                reason="非前波龙头，无资金记忆",
                key_metrics={"第一波高度": first_wave_data.get('max_boards', 0)},
                risk_warning="跟风股无二波，只有龙头有二波"
            )
        
        # ========== 条件2：时机判断（情绪周期） ==========
        timing = self._check_cycle_timing(market_sentiment, sector_data)
        if not timing['is_right_timing']:
            return SecondWaveSignal(
                stage=SecondWaveStage.NOT_READY,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.4,
                entry_timing="等待",
                reason=f"情绪周期{market_sentiment}，非二波启动时机",
                key_metrics={"当前情绪": market_sentiment},
                risk_warning="退潮期做二波=接飞刀，必须等冰点转暖"
            )
        
        # ========== 条件3：技术形态（调整充分） ==========
        technical = self._analyze_adjustment(adjust_data, first_wave_data)
        if not technical['is_adequate_adjust']:
            return SecondWaveSignal(
                stage=SecondWaveStage.WATCHING if technical['score'] > 60 else SecondWaveStage.NOT_READY,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=technical['score'] / 100,
                entry_timing="观察" if technical['score'] > 60 else "不介入",
                reason=f"调整不充分：深度{technical['depth']*100:.1f}%，时间{technical['days']}天",
                key_metrics=technical,
                risk_warning="调整不充分=筹码未沉淀，拉升易遇抛压"
            )
        
        # ========== 条件4：催化验证（新逻辑） ==========
        catalyst = self._check_new_catalyst(stock_code, news_data, sector_data)
        
        # ========== 条件5：今日信号（启动确认） ==========
        today_signal = self._check_today_signal(today_data, technical)
        
        # 综合判断介入时机
        if today_signal['is_limit_up']:
            # 今日已涨停，启动确认，但买点已过
            return SecondWaveSignal(
                stage=SecondWaveStage.ENTRY_DAY,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.85,
                entry_timing="明日竞价" if today_signal['seal_quality'] else "放弃",
                reason=f"二波启动确认，{'封单强，明日竞价介入' if today_signal['seal_quality'] else '封单弱，观察'}",
                key_metrics={
                    "第一波高度": identity['first_wave_boards'],
                    "调整深度": f"{technical['depth']*100:.1f}%",
                    "调整天数": technical['days'],
                    "支撑均线": technical['support_ma'],
                    "地量缩比": f"{technical['shrink_ratio']*100:.1f}%",
                    "新催化": catalyst['has_catalyst'],
                    "催化强度": catalyst['score'],
                    "今日涨停时间": today_signal['limit_up_time'],
                    "封单质量": today_signal['seal_quality']
                },
                risk_warning="今日已板，明日高开追高风险大，必须等竞价确认"
            )
        
        elif today_signal['is_preparation']:
            # 今日未涨停但放量异动，明日观察
            return SecondWaveSignal(
                stage=SecondWaveStage.WATCHING,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.65,
                entry_timing="明日早盘",
                reason=f"二波条件具备，今日放量异动，明日早盘观察是否启动",
                key_metrics={
                    "今日涨幅": f"{today_data.get('涨跌幅', 0):.1f}%",
                    "今日量比": f"{today_data.get('量比', 0):.1f}",
                    "距均线": technical['ma_distance']
                },
                risk_warning="未涨停=未确认，明日必须等涨停才介入"
            )
        
        else:
            # 条件具备但无信号，继续观察
            return SecondWaveSignal(
                stage=SecondWaveStage.WATCHING,
                stock_code=stock_code,
                stock_name=stock_name,
                confidence=0.55,
                entry_timing="观察",
                reason="二波条件具备，等放量启动信号",
                key_metrics={
                    "调整状态": "充分",
                    "支撑位置": technical['support_ma'],
                    "催化状态": "有" if catalyst['has_catalyst'] else "无"
                },
                risk_warning="提前埋伏=赌，必须等涨停确认"
            )
    
    # ==================== 核心判断方法 ====================
    
    def _verify_dragon_identity(self, code: str, first_wave: Dict) -> Dict:
        """
        验证是否是前一波真龙头（非跟风）
        """
        max_boards = first_wave.get('max_boards', 0)
        sector_rank = first_wave.get('sector_rank', 99)  # 板块内排名
        is_highest_board = first_wave.get('is_market_highest', False)  # 是否市场最高板
        
        # 真龙标准：5板+且板块前3或市场最高
        is_dragon = (max_boards >= self.params["min_first_wave_height"] and 
                    (sector_rank <= 3 or is_highest_board))
        
        return {
            'is_dragon': is_dragon,
            'first_wave_boards': max_boards,
            'sector_rank': sector_rank,
            'score': 100 if is_dragon else (50 if max_boards >= 4 else 0)
        }
    
    def _check_cycle_timing(self, sentiment: str, sector: Dict) -> Dict:
        """
        判断情绪周期是否适合二波
        """
        # 最佳时机：冰点转暖第一天
        right_timing = sentiment in ["冰点转暖", "混沌期"]
        
        # 板块热度未退
        sector_hot = sector.get('3日涨停数', 0) >= self.params["sector_heat_3d"]
        
        return {
            'is_right_timing': right_timing and sector_hot,
            'sentiment': sentiment,
            'sector_hot': sector_hot,
            'score': 90 if right_timing else (60 if sentiment == "退潮末期" else 30)
        }
    
    def _analyze_adjustment(self, adjust_df: pd.DataFrame, first_wave: Dict) -> Dict:
        """
        分析调整是否充分（深度、时间、量能、支撑）
        """
        if adjust_df.empty or len(adjust_df) < 5:
            return {'is_adequate_adjust': False, 'score': 0}
        
        peak_price = first_wave.get('peak_price', adjust_df['high'].max())
        latest_price = adjust_df['close'].iloc[-1]
        lowest_price = adjust_df['low'].min()
        
        # 1. 调整深度
        adjust_depth = (peak_price - lowest_price) / peak_price
        
        # 2. 调整天数
        adjust_days = len(adjust_df)
        
        # 3. 均线支撑
        adjust_df['MA5'] = adjust_df['close'].rolling(5).mean()
        adjust_df['MA10'] = adjust_df['close'].rolling(10).mean()
        adjust_df['MA20'] = adjust_df['close'].rolling(20).mean()
        
        latest = adjust_df.iloc[-1]
        ma_distances = {
            'MA5': abs(latest['close'] - latest['MA5']) / latest['MA5'] if not pd.isna(latest['MA5']) else 1,
            'MA10': abs(latest['close'] - latest['MA10']) / latest['MA10'] if not pd.isna(latest['MA10']) else 1,
            'MA20': abs(latest['close'] - latest['MA20']) / latest['MA20'] if not pd.isna(latest['MA20']) else 1
        }
        
        # 找到最近的均线
        nearest_ma = min(ma_distances, key=ma_distances.get)
        ma_distance = ma_distances[nearest_ma]
        
        # 4. 量能萎缩
        vol_ma20 = adjust_df['vol'].tail(20).mean()
        min_vol = adjust_df['vol'].tail(10).min()
        shrink_ratio = min_vol / vol_ma20 if vol_ma20 > 0 else 1
        
        # 综合评分
        is_adequate = (
            self.params["adjust_depth_min"] <= adjust_depth <= self.params["adjust_depth_max"] and
            self.params["adjust_days_min"] <= adjust_days <= self.params["adjust_days_max"] and
            ma_distance <= self.params["max_distance_from_ma"] and
            shrink_ratio <= self.params["min_shrink_ratio"]
        )
        
        score = 50
        if self.params["adjust_depth_min"] <= adjust_depth <= 0.25:
            score += 20
        if 7 <= adjust_days <= 12:
            score += 20
        if shrink_ratio <= 0.25:
            score += 20
        if ma_distance <= 0.02:
            score += 10
        
        return {
            'is_adequate_adjust': is_adequate,
            'depth': adjust_depth,
            'days': adjust_days,
            'support_ma': nearest_ma,
            'ma_distance': ma_distance,
            'shrink_ratio': shrink_ratio,
            'score': score
        }
    
    def _check_new_catalyst(self, code: str, news: List[Dict], sector: Dict) -> Dict:
        """
        检查是否有新催化（政策/订单/涨价/技术突破）
        """
        if not news:
            return {'has_catalyst': False, 'score': 0, 'type': '无'}
        
        # 近3日新闻
        recent_news = [n for n in news if n.get('date', '') >= 
                      (datetime.now() - timedelta(days=3)).strftime('%Y%m%d')]
        
        # 催化类型识别
        catalyst_types = []
        scores = []
        
        for n in recent_news:
            title = n.get('title', '')
            if any(kw in title for kw in ['政策', '规划', '补贴', '国产替代']):
                catalyst_types.append('政策')
                scores.append(80)
            elif any(kw in title for kw in ['订单', '中标', '合同', '量产']):
                catalyst_types.append('订单')
                scores.append(75)
            elif any(kw in title for kw in ['涨价', '涨价函', '供不应求']):
                catalyst_types.append('涨价')
                scores.append(70)
            elif any(kw in title for kw in ['突破', '技术', '专利', '认证']):
                catalyst_types.append('技术')
                scores.append(65)
        
        max_score = max(scores) if scores else 0
        
        return {
            'has_catalyst': max_score >= self.params["news_score_threshold"],
            'score': max_score,
            'type': catalyst_types[0] if catalyst_types else '无',
            'news_count': len(recent_news)
        }
    
    def _check_today_signal(self, today: pd.Series, technical: Dict) -> Dict:
        """
        检查今日启动信号
        """
        is_limit_up = today.get('涨跌幅', 0) >= 9.5
        limit_up_time = today.get('首次封板时间', '')
        
        # 封单质量
        seal_amount = today.get('封单额', 0)
        float_cap = today.get('流通市值', 1) * 10000
        seal_quality = seal_amount > float_cap * 0.08 and today.get('炸板次数', 0) == 0
        
        # 放量情况
        volume_ratio = today.get('成交量', 0) / (technical.get('avg_vol', 1) * 1.5)
        is_preparation = 0.05 <= today.get('涨跌幅', 0) < 9.5 and volume_ratio > 1.2
        
        return {
            'is_limit_up': is_limit_up,
            'limit_up_time': limit_up_time,
            'seal_quality': seal_quality,
            'is_preparation': is_preparation,
            'volume_ratio': volume_ratio
        }

# ==================== 介入时机策略 ====================

class SecondWaveEntryTiming:
    """
    龙二波明确介入时机
    """
    
    def __init__(self):
        self.entry_rules = {
            "最佳": "启动日首板打板或次日竞价",
            "次佳": "启动次日分歧转一致",
            "放弃": "连续加速后"
        }
    
    def determine_entry(self, signal: SecondWaveSignal, 
                       tomorrow_auction: Dict = None) -> Dict:
        """
        根据信号阶段确定具体介入时机
        """
        stage = signal.stage
        
        if stage == SecondWaveStage.ENTRY_DAY and tomorrow_auction:
            # 启动次日竞价决策
            gap = tomorrow_auction.get('高开幅度', 0)
            vol_ratio = tomorrow_auction.get('竞价量比', 0)
            
            if gap >= 0.03 and vol_ratio >= 0.10:
                return {
                    'action': '竞价介入',
                    'price': tomorrow_auction['开盘价'],
                    'confidence': 0.85,
                    'reason': '二波启动次日高开确认，竞价抢筹'
                }
            elif 0 < gap < 0.03:
                return {
                    'action': '开盘观察',
                    'price': 0,
                    'confidence': 0.60,
                    'reason': '高开不足，等开盘5分钟确认'
                }
            else:
                return {
                    'action': '放弃',
                    'price': 0,
                    'confidence': 0.3,
                    'reason': '低开或平开，二波失败，放弃'
                }
        
        elif stage == SecondWaveStage.WATCHING:
            return {
                'action': '等涨停确认',
                'price': 0,
                'confidence': 0.5,
                'reason': '条件具备但未启动，必须等涨停才介入'
            }
        
        return {
            'action': '不介入',
            'price': 0,
            'confidence': 0,
            'reason': '条件不具备'
        }

# ==================== 实战口诀 ====================

"""
龙二波，三要素
前龙身份要确认，跟风无二波
情绪周期要转暖，冰点不接刀
新催化，必须有，无催化走不远

介入时机两选择
启动日，首板打，次日有溢价
启动次，竞价买，高开3%量10%

放弃信号要牢记
退潮期，不做二波，做就是接飞刀
无催化，技术反弹，高度有限
跟风股，假二波，次日被核
加速后，再买就是接盘
"""

if __name__ == "__main__":
    print("龙二波策略加载完成")
    print("核心：前龙身份+情绪转暖+新催化，三要素缺一不可")
    print("介入时机：启动日首板或次日竞价，绝不提前埋伏")