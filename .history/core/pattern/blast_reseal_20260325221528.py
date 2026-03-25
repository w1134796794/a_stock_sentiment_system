"""
炸板回封策略 - 次日候选筛选器（非当日买点）
核心：识别洗盘性质，加入次日观察池，竞价确认后再介入
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
import loguru

logger = loguru.logger

class BlastResealType(Enum):
    WASH_SUCCESS = "洗盘成功"      # 优质回封，次日观察
    WASH_UNCERTAIN = "洗盘待定"    # 一般回封，次日谨慎
    SHIPMENT = "出货嫌疑"          # 劣质回封，回避
    TOO_LATE = "尾盘回封"          # 时间太晚，不观察

@dataclass
class BlastResealSignal:
    signal_type: BlastResealType
    stock_code: str
    stock_name: str
    blast_time: str           # 炸板时间
    reseal_time: str          # 回封时间
    is_next_day_candidate: bool  # 是否加入次日候选
    confidence: float
    reason: str
    key_metrics: Dict
    next_day_condition: str   # 次日介入条件

class BlastResealAnalyzer:
    def __init__(self, data_manager):
        self.dm = data_manager
        
        # 核心参数（区分洗盘vs出货）
        self.params = {
            # 时间窗口（早盘洗盘才有效）
            "max_blast_time": "10:30:00",     # 最晚炸板时间
            "max_reseal_duration": 20,        # 最长回封时间（分钟）
            "ideal_reseal_duration": 10,       # 理想回封时间（分钟）
            
            # 价格行为（判断主力意图）
            "max_blast_depth": 0.03,          # 炸板最深不超过-3%（拒绝大阴棒）
            "min_reseal_strength": 0.05,       # 回封必须封死+5%以上（拒绝反复）
            
            # 量能控制（洗盘vs出货分水岭）
            "max_blast_volume_ratio": 0.25,   # 炸板量<全天25%
            "min_reseal_volume_ratio": 1.5,   # 回封量>炸板1.5倍
            "max_total_turnover": 0.35,       # 全天换手<35%
            
            # 身份识别
            "min_board_height": 2,             # 至少2板（有辨识度）
            "max_blast_times": 3,              # 炸板次数<3（太多说明无控盘）
        }
    
    def analyze_blast_reseal(self,
                            stock_code: str,
                            stock_name: str,
                            board_height: int,              # 连板高度
                            today_tick: pd.DataFrame,       # 分时数据
                            today_summary: pd.Series,       # 日K数据
                            sector_leader: bool,             # 是否板块龙头
                            yest_zt_pool: pd.DataFrame       # 昨日涨停池（判断身份）
                            ) -> Optional[BlastResealSignal]:
        """
        分析炸板回封性质，决定是否加入次日候选
        注意：当日绝不买入，只筛选次日观察标的！
        """
        
        # ========== 前置条件：必须有炸板记录 ==========
        blast_times = today_summary.get('炸板次数', 0)
        if blast_times == 0 or blast_times > self.params["max_blast_times"]:
            return None
        
        # ========== 条件1：时间窗口（早盘才可能是洗盘） ==========
        blast_time = self._get_first_blast_time(today_tick)
        if not blast_time or blast_time > self.params["max_blast_time"]:
            return BlastResealSignal(
                signal_type=BlastResealType.TOO_LATE,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time or "未知",
                reseal_time=today_summary.get('最后封板时间', ''),
                is_next_day_candidate=False,
                confidence=0.3,
                reason="尾盘炸板回封，主力偷袭出货嫌疑",
                key_metrics={"炸板时间": blast_time},
                next_day_condition="不观察"
            )
        
        # ========== 条件2：炸板深度（判断是否恐慌洗盘） ==========
        blast_depth = self._get_blast_depth(today_tick, blast_time, today_summary['涨停价'])
        
        # 炸到水下-3%以下，恐慌过度，次日难修复
        if blast_depth < -self.params["max_blast_depth"]:
            return BlastResealSignal(
                signal_type=BlastResealType.SHIPMENT,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=today_summary.get('最后封板时间', ''),
                is_next_day_candidate=False,
                confidence=0.2,
                reason=f"炸板过深{blast_depth*100:.1f}%，恐慌盘涌出，出货嫌疑",
                key_metrics={"炸板深度": f"{blast_depth*100:.1f}%"},
                next_day_condition="回避"
            )
        
        # ========== 条件3：回封速度与质量 ==========
        reseal_time = today_summary.get('最后封板时间', '')
        reseal_duration = self._calculate_duration(blast_time, reseal_time)
        
        if reseal_duration > self.params["max_reseal_duration"]:
            return BlastResealSignal(
                signal_type=BlastResealType.WASH_UNCERTAIN,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=reseal_time,
                is_next_day_candidate=False,  # 回封太慢，不观察
                confidence=0.4,
                reason=f"回封用时{reseal_duration}分钟，资金犹豫，次日不确定",
                key_metrics={"回封用时": f"{reseal_duration}分钟"},
                next_day_condition="不观察"
            )
        
        # ========== 条件4：量能分析（洗盘vs出货关键） ==========
        volume_analysis = self._analyze_volume(today_tick, today_summary, blast_time, reseal_time)
        
        # 炸板量过大 = 出货
        if volume_analysis['blast_volume_ratio'] > self.params["max_blast_volume_ratio"]:
            return BlastResealSignal(
                signal_type=BlastResealType.SHIPMENT,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=reseal_time,
                is_next_day_candidate=False,
                confidence=0.3,
                reason=f"炸板量占全天{volume_analysis['blast_volume_ratio']*100:.1f}%，出货明显",
                key_metrics={"炸板量占比": f"{volume_analysis['blast_volume_ratio']*100:.1f}%"},
                next_day_condition="回避"
            )
        
        # 全天换手过高 = 筹码散乱
        total_turnover = today_summary.get('换手率', 0) / 100
        if total_turnover > self.params["max_total_turnover"]:
            return BlastResealSignal(
                signal_type=BlastResealType.SHIPMENT,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=reseal_time,
                is_next_day_candidate=False,
                confidence=0.3,
                reason=f"全天换手{total_turnover*100:.1f}%，筹码已乱，次日难接力",
                key_metrics={"全天换手": f"{total_turnover*100:.1f}%"},
                next_day_condition="回避"
            )
        
        # ========== 条件5：身份识别（龙头才值得观察） ==========
        if board_height < self.params["min_board_height"] and not sector_leader:
            # 首板炸板回封，无辨识度，次日无人记得
            return BlastResealSignal(
                signal_type=BlastResealType.WASH_UNCERTAIN,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=reseal_time,
                is_next_day_candidate=False,
                confidence=0.3,
                reason="首板无辨识度，炸板回封次日无人接力",
                key_metrics={"连板高度": board_height},
                next_day_condition="不观察"
            )
        
        # ========== 综合评分：决定是否加入次日候选 ==========
        score = self._calculate_wash_score(
            blast_time, reseal_duration, blast_depth,
            volume_analysis, board_height, sector_leader
        )
        
        # 优质洗盘信号，加入次日候选
        if score >= 75:
            return BlastResealSignal(
                signal_type=BlastResealType.WASH_SUCCESS,
                stock_code=stock_code,
                stock_name=stock_name,
                blast_time=blast_time,
                reseal_time=reseal_time,
                is_next_day_candidate=True,  # 关键：加入次日观察池
                confidence=score / 100,
                reason=f"{'龙头' if sector_leader else str(board_height)+'板'}早盘快速洗盘，"
                       f"炸板{reseal_duration}分钟回封，量能健康",
                key_metrics={
                    "连板高度": board_height,
                    "板块地位": "龙头" if sector_leader else "跟风",
                    "炸板时间": blast_time,
                    "回封用时": f"{reseal_duration}分钟",
                    "炸板深度": f"{blast_depth*100:.1f}%",
                    "炸板量占比": f"{volume_analysis['blast_volume_ratio']*100:.1f}%",
                    "回封/炸板量比": f"{volume_analysis['reseal_blast_ratio']:.1f}",
                    "全天换手": f"{total_turnover*100:.1f}%"
                },
                # 次日介入条件（核心！）
                next_day_condition="次日高开2%+竞价量>8%，则竞价介入；低开或平开则放弃"
            )
        
        # 一般信号，谨慎观察
        return BlastResealSignal(
            signal_type=BlastResealType.WASH_UNCERTAIN,
            stock_code=stock_code,
            stock_name=stock_name,
            blast_time=blast_time,
            reseal_time=reseal_time,
            is_next_day_candidate=False,
            confidence=score / 100,
            reason="回封质量一般，次日不确定性高",
            key_metrics={"综合评分": score},
            next_day_condition="不主动观察，除非次日超预期弱转强"
        )
    
    # ==================== 核心辅助方法 ====================
    
    def _get_first_blast_time(self, tick_df: pd.DataFrame) -> Optional[str]:
        """获取首次炸板时间（从涨停价打开）"""
        if tick_df.empty:
            return None
        
        limit_price = tick_df[tick_df['price'] == tick_df['price'].max()].iloc[0]['price']
        # 找到价格从limit_price跌下来的第一个时间点
        blast_ticks = tick_df[(tick_df['price'] < limit_price * 0.99) & 
                             (tick_df['time'] > "09:30:00")]
        if blast_ticks.empty:
            return None
        return blast_ticks.iloc[0]['time']
    
    def _get_blast_depth(self, tick_df: pd.DataFrame, blast_time: str, limit_price: float) -> float:
        """计算炸板最深跌幅（相对于涨停价）"""
        blast_period = tick_df[tick_df['time'] >= blast_time].head(10)  # 炸板后10个tick
        if blast_period.empty:
            return 0
        min_price = blast_period['price'].min()
        return (min_price - limit_price) / limit_price
    
    def _calculate_duration(self, start_time: str, end_time: str) -> int:
        """计算时间差（分钟）"""
        try:
            fmt = "%H:%M:%S"
            start = datetime.strptime(start_time, fmt)
            end = datetime.strptime(end_time, fmt)
            return int((end - start).total_seconds() / 60)
        except:
            return 999
    
    def _analyze_volume(self, tick_df: pd.DataFrame, summary: pd.Series,
                       blast_time: str, reseal_time: str) -> Dict:
        """分析炸板和回封的量能"""
        # 炸板量（炸板后5分钟）
        blast_period = tick_df[(tick_df['time'] >= blast_time) & 
                              (tick_df['time'] <= self._add_minutes(blast_time, 5))]
        blast_volume = blast_period['volume'].sum() if not blast_period.empty else 0
        
        # 回封量（回封前5分钟）
        reseal_period = tick_df[(tick_df['time'] >= self._add_minutes(reseal_time, -5)) & 
                               (tick_df['time'] <= reseal_time)]
        reseal_volume = reseal_period['volume'].sum() if not reseal_period.empty else 0
        
        total_volume = summary.get('成交量', 1)
        
        return {
            'blast_volume': blast_volume,
            'reseal_volume': reseal_volume,
            'blast_volume_ratio': blast_volume / total_volume,
            'reseal_blast_ratio': reseal_volume / blast_volume if blast_volume > 0 else 0
        }
    
    def _add_minutes(self, time_str: str, minutes: int) -> str:
        """时间加减"""
        try:
            fmt = "%H:%M:%S"
            dt = datetime.strptime(time_str, fmt)
            new_dt = dt + pd.Timedelta(minutes=minutes)
            return new_dt.strftime(fmt)
        except:
            return time_str
    
    def _calculate_wash_score(self, blast_time: str, reseal_duration: int,
                             blast_depth: float, vol_analysis: Dict,
                             board_height: int, is_leader: bool) -> int:
        """计算洗盘质量评分（0-100）"""
        score = 50  # 基础分
        
        # 时间加分（越早越好）
        if blast_time < "09:40:00":
            score += 15
        elif blast_time < "10:00:00":
            score += 10
        
        # 回封速度加分
        if reseal_duration <= 5:
            score += 20
        elif reseal_duration <= 10:
            score += 15
        elif reseal_duration <= 15:
            score += 10
        
        # 炸板深度加分（越浅越好）
        if blast_depth > -0.01:
            score += 15
        elif blast_depth > -0.02:
            score += 10
        
        # 量能加分
        if vol_analysis['blast_volume_ratio'] < 0.15:
            score += 10
        if vol_analysis['reseal_blast_ratio'] > 2:
            score += 10
        
        # 身份加分
        if is_leader:
            score += 15
        elif board_height >= 3:
            score += 10
        
        return min(score, 100)

# ==================== 次日介入策略 ====================

class NextDayEntryStrategy:
    """
    针对前日炸板回封票的次日介入策略
    核心：竞价确认洗盘成功，才介入
    """
    
    def __init__(self):
        self.params = {
            "min_gap": 0.02,           # 高开>2%
            "min_auction_vol": 0.08,    # 竞价量>8%
            "min_auction_amount": 3000000,  # 竞价金额>300万
            "max_low_gap": -0.03        # 低开>-3%还可观察，<-3%直接放弃
        }
    
    def check_next_day_entry(self, 
                            stock_code: str,
                            auction_data: Dict,
                            pre_day_signal: BlastResealSignal) -> Optional[TradeSignal]:
        """
        次日竞价检查，确认是否介入
        """
        if not pre_day_signal.is_next_day_candidate:
            return None
        
        open_price = auction_data.get('开盘价', 0)
        pre_close = auction_data.get('昨收', 1)
        gap_ratio = (open_price - pre_close) / pre_close
        
        # 竞价量
        auction_vol_ratio = auction_data.get('竞价成交量', 0) / auction_data.get('昨日成交量', 1)
        
        # 情况1：高开+爆量 = 洗盘成功，竞价介入
        if gap_ratio >= self.params["min_gap"] and auction_vol_ratio >= self.params["min_auction_vol"]:
            return TradeSignal(
                pattern_type="炸板回封次日弱转强",
                stock_code=stock_code,
                stock_name=pre_day_signal.stock_name,
                trigger_time="09:25:00",
                confidence=0.80,
                entry_price=open_price,
                stop_loss=pre_close * 0.97,  # 破昨收止损
                take_profit=open_price * 1.08,
                position_size="medium",
                reason=f"前日炸板回封洗盘，次日高开{gap_ratio*100:.1f}%竞价量{auction_vol_ratio*100:.1f}%，确认成功",
                key_metrics={
                    "前日炸板时间": pre_day_signal.blast_time,
                    "次日高开": f"{gap_ratio*100:.1f}%",
                    "次日竞价量": f"{auction_vol_ratio*100:.1f}%"
                },
                validation_rules=[
                    "前日优质炸板回封",
                    "次日高开>2%",
                    "次日竞价量>8%"
                ],
                buy_timing="竞价"
            )
        
        # 情况2：平开或低开-3%以内 = 观察，等开盘确认
        elif -0.03 <= gap_ratio < 0.02:
            return TradeSignal(
                pattern_type="炸板回封次日观察",
                stock_code=stock_code,
                stock_name=pre_day_signal.stock_name,
                trigger_time="09:30:00",
                confidence=0.50,  # 降低置信度
                entry_price=0,  # 待开盘确认
                stop_loss=0,
                take_profit=0,
                position_size="light",
                reason=f"次日平开{gap_ratio*100:.1f}%，观察开盘5分钟走势",
                key_metrics={"次日开盘": f"{gap_ratio*100:.1f}%"},
                validation_rules=["开盘5分钟内拉升翻红则跟进"],
                buy_timing="开盘确认"
            )
        
        # 情况3：低开<-3% = 放弃
        else:
            return None

# ==================== 实战口诀 ====================

"""
炸板回封，次日再看
早盘炸板是洗盘，尾盘炸板是出货
回封越快越强势，超过20分钟不观察
炸到水下是恐慌，炸到+5是出货
次日高开竞价量够，才是买点
次日低开直接放弃，不心存侥幸
只做龙头炸板回封，杂毛不看不买
"""

if __name__ == "__main__":
    print("炸板回封分析器加载完成")
    print("核心：识别洗盘性质，加入次日候选，竞价确认后介入")
    print("切记：当日绝不追板，次日竞价定生死！")