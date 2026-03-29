"""
交易执行引擎 - 统一整合所有模式的介入时机
实现：复盘生成次日计划 + 当日实时信号推送
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
import json
import loguru

logger = loguru.logger


def format_value(val, decimal_places=2):
    """格式化数值，处理numpy类型和精度"""
    if val is None or pd.isna(val):
        return ""
    if isinstance(val, (np.integer, np.int64, np.int32)):
        return int(val)
    if isinstance(val, (np.floating, np.float64, np.float32)):
        return round(float(val), decimal_places)
    if isinstance(val, (int, float)):
        if isinstance(val, float):
            return round(val, decimal_places)
        return val
    return val


def format_time_field(val):
    """格式化时间字段为 hh:mm:ss"""
    if val is None or pd.isna(val):
        return ""
    if isinstance(val, str):
        # 已经是字符串，检查格式
        val = val.strip()
        if len(val) == 5:  # HH:MM
            return val + ":00"
        elif len(val) == 6:  # HHMMSS
            return f"{val[:2]}:{val[2:4]}:{val[4:]}"
        elif len(val) == 8 and ':' in val:  # HH:MM:SS
            return val
        return val
    if isinstance(val, (int, float, np.integer, np.floating)):
        # 数字格式，假设是 HHMMSS
        val = int(val)
        if val < 100:  # 可能是小时
            return f"{val:02d}:00:00"
        elif val < 10000:  # 可能是 HHMM
            return f"{val//100:02d}:{val%100:02d}:00"
        else:  # HHMMSS
            return f"{val//10000:02d}:{(val//100)%100:02d}:{val%100:02d}"
    return str(val)

class TradeAction(Enum):
    BUY = "买入"
    WATCH = "观察"
    SELL = "卖出"
    HOLD = "持有"
    SKIP = "放弃"

class TimeSlot(Enum):
    PRE_AUCTION = "09:15-09:25"      # 竞价阶段
    AUCTION_END = "09:24:30-09:25:00" # 竞价末段
    OPEN = "09:30:00-09:31:00"       # 开盘第一笔
    EARLY_MORNING = "09:31-10:00"    # 早盘
    MORNING = "10:00-11:30"          # 上午
    AFTERNOON = "13:00-14:30"        # 下午
    LATE_AFTERNOON = "14:30-15:00"   # 尾盘

@dataclass
class TradePlan:
    """交易计划"""
    pattern_type: str           # 模式类型
    stock_code: str
    stock_name: str
    action: TradeAction         # 动作
    entry_timing: TimeSlot      # 介入时机
    entry_price: float          # 目标买入价
    stop_loss: float           # 止损价
    take_profit: float         # 止盈价
    position_size: str         # 仓位（light/medium/heavy）
    pre_conditions: List[str]   # 前置条件（必须满足）
    cancel_conditions: List[str] # 取消条件（任一满足则放弃）
    confidence: float            # 置信度
    reason: str                # 交易理由
    add_to_watchlist: bool     # 是否加入观察池

class UnifiedExecutionEngine:
    """
    统一交易执行引擎
    整合所有模式的介入时机，生成可执行的交易计划
    """
    
    def __init__(self, data_manager, strategy_engine):
        self.dm = data_manager
        self.se = strategy_engine
        self.today = datetime.now().strftime("%Y%m%d")
        
        # 各模式介入时机配置
        self.timing_config = {
            # 二板定龙：竞价定生死
            "二板定龙": {
                "primary": TimeSlot.AUCTION_END,    # 首选：竞价末段
                "secondary": TimeSlot.OPEN,          # 备选：开盘第一笔
                "deadline": TimeSlot.EARLY_MORNING, # 最晚：早盘10点前
                "price_type": "涨停价",
                "position": "medium"
            },
            
            # 分歧转一致：竞价确认转强
            "分歧转一致": {
                "primary": TimeSlot.AUCTION_END,    # 竞价末段
                "secondary": TimeSlot.OPEN,          # 开盘第一笔
                "deadline": TimeSlot.EARLY_MORNING,
                "price_type": "开盘价",
                "position": "medium"
            },
            
            # 弱转强：竞价超预期
            "弱转强": {
                "primary": TimeSlot.AUCTION_END,
                "secondary": TimeSlot.OPEN,
                "deadline": TimeSlot.EARLY_MORNING,
                "price_type": "开盘价",
                "position": "heavy"  # 高置信度，重仓
            },
            
            # 首板突破：早盘秒封
            "首板突破": {
                "primary": TimeSlot.EARLY_MORNING,  # 9:40前涨停瞬间
                "secondary": None,
                "deadline": TimeSlot.EARLY_MORNING,
                "price_type": "涨停价",
                "position": "light"
            },
            
            # 竞价爆量：竞价即介入
            "竞价爆量": {
                "primary": TimeSlot.AUCTION_END,
                "secondary": None,
                "deadline": TimeSlot.AUCTION_END,
                "price_type": "竞价匹配价",
                "position": "medium"
            },
            
            # 炸板回封：次日观察，非当日买
            "炸板回封": {
                "primary": TimeSlot.AUCTION_END,    # 次日竞价
                "secondary": TimeSlot.OPEN,          # 次日开盘
                "deadline": TimeSlot.EARLY_MORNING,
                "price_type": "开盘价",
                "position": "light",
                "note": "次日执行，非当日"
            },
            
            # 卡位板：涨停瞬间
            "卡位板": {
                "primary": TimeSlot.EARLY_MORNING,  # 涨停瞬间
                "secondary": None,
                "deadline": TimeSlot.EARLY_MORNING,
                "price_type": "涨停价",
                "position": "medium"
            },
            
            # 龙二波：启动日首板或次日竞价
            "龙二波": {
                "primary": TimeSlot.EARLY_MORNING,  # 首板打板
                "secondary": TimeSlot.AUCTION_END,  # 次日竞价
                "deadline": TimeSlot.AFTERNOON,
                "price_type": "涨停价/开盘价",
                "position": "medium"
            },
            
            # 龙回头：回踩均线低吸
            "龙回头": {
                "primary": TimeSlot.EARLY_MORNING,  # 早盘回踩
                "secondary": TimeSlot.AFTERNOON,     # 下午回踩
                "deadline": TimeSlot.LATE_AFTERNOON,
                "price_type": "均线价",
                "position": "light"
            }
        }
    
    # ==================== 复盘后生成次日计划 ====================
    
    def generate_next_day_plans(self, 
                               analysis_date: str,
                               all_signals: Dict[str, List]) -> pd.DataFrame:
        """
        复盘后生成次日交易计划
        输入：当日分析的所有模式信号
        输出：次日可执行的交易计划表
        """
        plans = []
        
        for pattern_name, signals in all_signals.items():
            for signal in signals:
                plan = self._convert_signal_to_plan(pattern_name, signal, analysis_date)
                if plan:
                    plans.append(plan)
        
        # 按介入时间排序
        plans_df = pd.DataFrame([self._plan_to_dict(p) for p in plans])
        if not plans_df.empty:
            plans_df = plans_df.sort_values(['介入时机', '置信度'], ascending=[True, False])
        
        return plans_df
    
    def _convert_signal_to_plan(self, pattern: str, signal, analysis_date: str) -> Optional[TradePlan]:
        """将模式信号转换为交易计划"""
        
        timing = self.timing_config.get(pattern)
        if not timing:
            return None
        
        # 根据模式确定具体参数
        if pattern == "二板定龙":
            return self._plan_second_board_dragon(signal, timing, analysis_date)
        elif pattern == "分歧转一致":
            return self._plan_divergence_consensus(signal, timing, analysis_date)
        elif pattern == "弱转强":
            return self._plan_weak_to_strong(signal, timing, analysis_date)
        elif pattern == "首板突破":
            return self._plan_first_board_breakout(signal, timing, analysis_date)
        elif pattern == "竞价爆量":
            return self._plan_auction_volume(signal, timing, analysis_date)
        elif pattern == "炸板回封":
            return self._plan_blast_reseal(signal, timing, analysis_date)
        elif pattern == "卡位板":
            return self._plan_position_battle(signal, timing, analysis_date)
        elif pattern == "龙二波":
            return self._plan_second_wave(signal, timing, analysis_date)
        elif pattern == "龙回头":
            return self._plan_dragon_pullback(signal, timing, analysis_date)
        
        return None
    
    def _plan_second_board_dragon(self, signal, timing, date) -> TradePlan:
        """二板定龙计划"""
        return TradePlan(
            pattern_type="二板定龙",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,  # 涨停价
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],
            pre_conditions=[
                "次日高开3%-7%",
                "竞价量>前日10%",
                "竞价价格向上"
            ],
            cancel_conditions=[
                "低开或平开",
                "竞价量<5%",
                "同板块已有2只二板"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_divergence_consensus(self, signal, timing, date) -> TradePlan:
        """分歧转一致计划"""
        return TradePlan(
            pattern_type="分歧转一致",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],
            pre_conditions=[
                "次日高开2%-5%",
                "竞价量>前日8%",
                "不跌破5日线"
            ],
            cancel_conditions=[
                "低开",
                "开盘后快速跌破昨日最低价",
                "15分钟未上板"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_weak_to_strong(self, signal, timing, date) -> TradePlan:
        """弱转强计划"""
        return TradePlan(
            pattern_type="弱转强",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],  # heavy
            pre_conditions=[
                "次日高开2%-7%",
                "竞价量>8%",
                "竞价价格向上"
            ],
            cancel_conditions=[
                "低开或平开",
                "开盘后回踩>2%",
                "10分钟未上板"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_first_board_breakout(self, signal, timing, date) -> TradePlan:
        """首板突破计划"""
        return TradePlan(
            pattern_type="首板突破",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],  # light
            pre_conditions=[
                "9:40前涨停",
                "封单持续增加",
                "板块效应明显"
            ],
            cancel_conditions=[
                "涨停时间>10:00",
                "反复炸板",
                "板块跟风不足"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_auction_volume(self, signal, timing, date) -> TradePlan:
        """竞价爆量计划"""
        return TradePlan(
            pattern_type="竞价爆量",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],  # 竞价末段
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],
            pre_conditions=[
                "竞价量>前日8%",
                "高开1%-7%",
                "竞价价格最后一分钟向上"
            ],
            cancel_conditions=[
                "竞价末端回落",
                "开盘低开",
                "开盘无量"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_blast_reseal(self, signal, timing, date) -> TradePlan:
        """炸板回封计划（次日执行）"""
        return TradePlan(
            pattern_type="炸板回封（次日）",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.WATCH,  # 先观察
            entry_timing=timing["primary"],
            entry_price=0,  # 次日开盘价
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],  # light
            pre_conditions=[
                "次日高开2%+",
                "竞价量>8%",
                "不跌破昨日收盘价"
            ],
            cancel_conditions=[
                "低开",
                "开盘后跌破昨日最低价",
                "10分钟未翻红"
            ],
            confidence=signal.confidence,
            reason=f"前日炸板回封，次日观察弱转强: {signal.description}",
            add_to_watchlist=True
        )
    
    def _plan_position_battle(self, signal, timing, date) -> TradePlan:
        """卡位板计划"""
        return TradePlan(
            pattern_type="卡位板",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],
            pre_conditions=[
                "低位股涨停瞬间",
                "高位股疲态（烂板/后封）",
                "领先时间>5分钟"
            ],
            cancel_conditions=[
                "高位股回封早于低位股",
                "低位股炸板",
                "板块整体走弱"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_second_wave(self, signal, timing, date) -> TradePlan:
        """龙二波计划"""
        # 判断是启动日还是次日
        if "今日首板" in signal.description:
            entry = timing["primary"]  # 早盘打板
        else:
            entry = timing["secondary"]  # 次日竞价
        
        return TradePlan(
            pattern_type="龙二波",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=entry,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],
            pre_conditions=[
                "启动日：首板打板确认",
                "次日：高开2%+竞价量8%"
            ],
            cancel_conditions=[
                "低开",
                "跌破10日线",
                "无板块支持"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_dragon_pullback(self, signal, timing, date) -> TradePlan:
        """龙回头计划"""
        return TradePlan(
            pattern_type="龙回头",
            stock_code=signal.stock_code,
            stock_name=signal.stock_name,
            action=TradeAction.BUY,
            entry_timing=timing["primary"],
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=timing["position"],  # light
            pre_conditions=[
                "回踩MA10/MA20",
                "缩量至前期30%",
                "出现阳线反弹"
            ],
            cancel_conditions=[
                "跌破均线",
                "放量下跌",
                "板块退潮"
            ],
            confidence=signal.confidence,
            reason=signal.description,
            add_to_watchlist=True
        )
    
    def _plan_to_dict(self, plan: TradePlan) -> Dict:
        """转换为字典"""
        return {
            "模式": plan.pattern_type,
            "代码": plan.stock_code,
            "名称": plan.stock_name,
            "动作": plan.action.value,
            "介入时机": plan.entry_timing.value,
            "目标价": plan.entry_price,
            "止损价": plan.stop_loss,
            "止盈价": plan.take_profit,
            "仓位": plan.position_size,
            "前置条件": "; ".join(plan.pre_conditions),
            "取消条件": "; ".join(plan.cancel_conditions),
            "置信度": plan.confidence,
            "理由": plan.reason,
            "加入观察池": plan.add_to_watchlist
        }
    
    # ==================== 当日实时信号 ====================
    
    def real_time_check(self, 
                       current_time: time,
                       auction_data: Dict,
                       tick_data: Dict,
                       watchlist: List[TradePlan]) -> List[Dict]:
        """
        当日实时检查，触发交易信号
        """
        triggered = []
        
        for plan in watchlist:
            # 检查是否到达介入时机
            if not self._is_in_timing_window(current_time, plan.entry_timing):
                continue
            
            # 检查是否满足条件
            check_result = self._check_conditions(plan, auction_data, tick_data)
            
            if check_result['should_execute']:
                triggered.append({
                    "plan": plan,
                    "action": "EXECUTE",
                    "price": check_result['price'],
                    "reason": check_result['reason']
                })
            elif check_result['should_cancel']:
                triggered.append({
                    "plan": plan,
                    "action": "CANCEL",
                    "reason": check_result['reason']
                })
        
        return triggered
    
    def _is_in_timing_window(self, current: time, target: TimeSlot) -> bool:
        """检查当前时间是否在介入窗口"""
        windows = {
            TimeSlot.AUCTION_END: (time(9, 24, 30), time(9, 25, 0)),
            TimeSlot.OPEN: (time(9, 30, 0), time(9, 31, 0)),
            TimeSlot.EARLY_MORNING: (time(9, 31, 0), time(10, 0, 0)),
            TimeSlot.MORNING: (time(10, 0, 0), time(11, 30, 0)),
            TimeSlot.AFTERNOON: (time(13, 0, 0), time(14, 30, 0)),
            TimeSlot.LATE_AFTERNOON: (time(14, 30, 0), time(15, 0, 0)),
        }
        
        if target not in windows:
            return False
        
        start, end = windows[target]
        return start <= current <= end
    
    def _check_conditions(self, plan: TradePlan, auction: Dict, tick: Dict) -> Dict:
        """检查计划的前置条件和取消条件"""
        # 简化实现，实际应根据模式类型检查具体数据
        return {
            'should_execute': True,
            'should_cancel': False,
            'price': plan.entry_price,
            'reason': "条件满足"
        }
    
    # ==================== 生成交易报告 ====================
    
    def generate_trade_report(self, plans_df: pd.DataFrame, date: str) -> str:
        """生成交易计划报告"""
        if plans_df.empty:
            return f"{date} 无交易计划"
        
        report = []
        report.append(f"\n{'='*60}")
        report.append(f"交易执行计划 - {date}")
        report.append(f"{'='*60}\n")
        
        # 按介入时机分组
        for timing in TimeSlot:
            group = plans_df[plans_df['介入时机'] == timing.value]
            if group.empty:
                continue
            
            report.append(f"\n【{timing.value}】")
            report.append(f"{'-'*40}")
            
            for _, row in group.head(3).iterrows():  # 每组最多3只
                # 格式化数值
                target_price = format_value(row['目标价'], 2)
                stop_loss = format_value(row['止损价'], 2)
                confidence = format_value(row['置信度'] * 100, 0) if isinstance(row['置信度'], (int, float, np.number)) else row['置信度']
                
                report.append(f"\n{row['模式']} | {row['名称']}({row['代码']})")
                report.append(f"  动作: {row['动作']} | 仓位: {row['仓位']}")
                report.append(f"  目标价: {target_price} | 止损: {stop_loss}")
                report.append(f"  前置: {row['前置条件']}")
                report.append(f"  取消: {row['取消条件']}")
                report.append(f"  置信度: {confidence}% | {str(row['理由'])[:30]}...")
        
        report.append(f"\n{'='*60}")
        return "\n".join(report)
    
    def save_trade_plans(self, plans_df: pd.DataFrame, date: str, output_dir: str) -> str:
        """
        保存交易计划到CSV文件
        
        Args:
            plans_df: 交易计划DataFrame
            date: 交易日期
            output_dir: 输出目录
            
        Returns:
            str: 保存的文件路径
        """
        if plans_df.empty:
            logger.warning(f"{date} 无交易计划，跳过保存")
            return ""
        
        # 确保输出目录存在
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # 按介入时机排序
        timing_order = {
            "09:15-09:25": 1,
            "09:24:30-09:25:00": 2,
            "09:30:00-09:31:00": 3,
            "09:31-10:00": 4,
            "10:00-11:30": 5,
            "13:00-14:30": 6,
            "14:30-15:00": 7
        }
        
        plans_df['排序'] = plans_df['介入时机'].map(timing_order)
        plans_df = plans_df.sort_values(['排序', '置信度'], ascending=[True, False])
        plans_df = plans_df.drop(columns=['排序'])
        
        # 格式化数据
        formatted_df = plans_df.copy()
        
        # 格式化数值列（保留2位小数）
        numeric_cols = ['目标价', '止损价', '止盈价', '置信度']
        for col in numeric_cols:
            if col in formatted_df.columns:
                formatted_df[col] = formatted_df[col].apply(lambda x: format_value(x, 2))
        
        # 格式化时间列
        time_cols = ['首次封板时间', '最后封板时间']
        for col in time_cols:
            if col in formatted_df.columns:
                formatted_df[col] = formatted_df[col].apply(format_time_field)
        
        # 格式化整数列（去除np.int64）
        int_cols = ['炸板次数', '连板数', 'BoardHeight']
        for col in int_cols:
            if col in formatted_df.columns:
                formatted_df[col] = formatted_df[col].apply(lambda x: int(x) if pd.notna(x) else "")
        
        # 保存CSV
        csv_file = output_path / f"交易计划_{date}.csv"
        formatted_df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        logger.info(f"✓ 交易计划已保存: {csv_file}")
        
        return str(csv_file)
    
    def generate_and_save_plans(self, 
                                analysis_date: str,
                                all_signals: Dict[str, List],
                                output_dir: str) -> Tuple[pd.DataFrame, str]:
        """
        生成并保存交易计划（复盘后完整流程）
        
        Args:
            analysis_date: 分析日期
            all_signals: 所有模式信号
            output_dir: 输出目录
            
        Returns:
            Tuple[pd.DataFrame, str]: (交易计划DataFrame, 交易报告文本)
        """
        # 1. 生成交易计划
        logger.info("生成次日交易计划...")
        plans_df = self.generate_next_day_plans(analysis_date, all_signals)
        
        if plans_df.empty:
            logger.warning("未生成交易计划")
            return plans_df, f"{analysis_date} 无交易计划"
        
        logger.info(f"生成 {len(plans_df)} 条交易计划")
        
        # 2. 保存CSV
        self.save_trade_plans(plans_df, analysis_date, output_dir)
        
        # 3. 生成交易报告
        report = self.generate_trade_report(plans_df, analysis_date)
        
        # 4. 保存报告文本
        report_file = Path(output_dir) / f"交易计划报告_{analysis_date}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"✓ 交易报告已保存: {report_file}")
        
        return plans_df, report

# ==================== 使用示例 ====================

if __name__ == "__main__":
    print("统一交易执行引擎加载完成")
    print("\n各模式介入时机汇总：")
    print("-" * 40)
    
    engine = UnifiedExecutionEngine(None, None)
    
    for pattern, config in engine.timing_config.items():
        primary = config['primary'].value if config['primary'] else "无"
        secondary = config['secondary'].value if config['secondary'] else "无"
        print(f"\n{pattern}:")
        print(f"  首选: {primary}")
        print(f"  备选: {secondary}")
        print(f"  仓位: {config['position']}")
