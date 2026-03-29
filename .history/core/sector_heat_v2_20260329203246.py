"""
多维度板块热度计算器 V2 - 优化版
核心改进：
1. 加入当日涨停统计（T+0灵敏度）
2. 退潮预警更灵敏（3日骤降即预警）
3. 观察期提前发现逻辑（潜在主线识别）
4. 散户聚焦：只输出核心 actionable 信号
5. 【新增】板块状态联动系统：趋势阶段 × 共振强度
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum, auto
import loguru
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import SECTOR_HEAT_WEIGHTS, SECTOR_HEAT_THRESHOLDS

logger = loguru.logger


# ========== 第一层：趋势阶段（时间维度）==========

class TrendStage(Enum):
    START = "启动期"           # 昨日0→今日有（质变）
    EXPLOSION = "爆发期"       # 今日倍增（量变加速）
    ACCELERATION = "加速期"    # 3日/5日持续增（趋势强化）
    CONFIRMED = "确认期"       # 多周期共振（趋势确立）
    MATURE = "成熟期"          # 高位震荡（后期）
    DECLINE_EARLY = "早期退潮" # 3日骤降（敏感撤退）
    DECLINE_LATE = "晚期退潮"  # 确认退潮（坚决回避）
    WATCH = "观察期"           # 无明确趋势


# ========== 第二层：共振强度（空间维度）==========

class ResonanceType(Enum):
    STRONG = "强共振"          # 涨停多+板块涨幅大+大票动
    QUANTITY_LEADS = "数量引领" # 涨停多+板块涨幅小（小票活跃）
    PRICE_LEADS = "价格引领"   # 涨停少+板块涨幅大（龙头独舞）
    WEAK = "弱共振"            # 涨停少+板块涨幅小
    NONE = "无共振"            # 单点异动，无板块效应
    ANY = "任意"               # 用于决策矩阵的模糊匹配


@dataclass
class SectorSignal:
    """板块信号 - 散户可直接使用的行动建议"""
    l2_name: str
    l1_name: str
    action: str                    # 具体行动建议
    priority: int                  # 优先级（1-5，1最高）
    confidence: float              # 置信度
    key_metrics: Dict              # 核心指标
    watch_reason: str              # 关注理由
    risk_warning: str              # 风险提示
    # 联动状态字段
    trend_stage: Optional[TrendStage] = None      # 趋势阶段
    resonance_type: Optional[ResonanceType] = None  # 共振类型
    combined_signal: Optional[str] = None          # 联动信号
    position_size: Optional[str] = None            # 仓位建议

class SectorHeatCalculatorV2:
    """
    多维度板块热度计算器 V2
    
    输入层：
      - 当日涨停数据（T+0，最敏感）
      - 3日/5日/20日历史数据（趋势判断）
    
    计算层：
      1. 当日爆发力 = 今日涨停数 / 昨日涨停数
      2. 短期动量 = (3日-5日) / 5日
      3. 中期趋势 = (5日-20日) / 20日
      4. 综合热度 = 加权得分 × 爆发力系数
    
    输出层（散户聚焦）：
      - 爆发期：当日突增，立即关注（优先级1）
      - 加速期：3日突增，新主线候选（优先级2）
      - 确认期：共振确认，积极参与（优先级3）
      - 早期退潮：3日骤降，敏感撤退（优先级1，风险）
      - 观察-待爆发：量能积蓄，提前埋伏（优先级4）
      
    不输出（忽略）：
      - 成熟期（老主线，难把握）
      - 晚期退潮（已确认，无机会）
      - 无趋势观察期（噪音）
    """
    
    # ========== 联动决策矩阵（趋势 × 共振 = 行动）==========
    DECISION_MATRIX = {
        # 启动期组合
        (TrendStage.START, ResonanceType.STRONG): {
            'signal': '强共振启动',
            'priority': 1,
            'action': '[启动]立即重仓，做首板/二板',
            'position': 'heavy',
            'risk': '最佳机会，次日有溢价'
        },
        (TrendStage.START, ResonanceType.QUANTITY_LEADS): {
            'signal': '数量启动-虚热',
            'priority': 2,
            'action': '[启动]只做龙头，不做跟风',
            'position': 'light',
            'risk': '大票未动，持续性存疑'
        },
        (TrendStage.START, ResonanceType.PRICE_LEADS): {
            'signal': '价格启动-独舞',
            'priority': 2,
            'action': '[启动]只做龙头，放弃跟风',
            'position': 'medium',
            'risk': '缺乏梯队，龙头炸则全崩'
        },
        (TrendStage.START, ResonanceType.WEAK): {
            'signal': '弱启动',
            'priority': 4,
            'action': '[启动]1成仓试探或观察',
            'position': 'light',
            'risk': '可能一日游，严格止损'
        },
        
        # 爆发期组合
        (TrendStage.EXPLOSION, ResonanceType.STRONG): {
            'signal': '强共振爆发',
            'priority': 1,
            'action': '[爆发]积极参与，做前排',
            'position': 'heavy',
            'risk': '加速期，注意分歧'
        },
        (TrendStage.EXPLOSION, ResonanceType.QUANTITY_LEADS): {
            'signal': '数量爆发-虚热警告',
            'priority': 3,
            'action': '[爆发]回避，小票乱炒',
            'position': 'none',
            'risk': '虚热，次日分化严重'
        },
        (TrendStage.EXPLOSION, ResonanceType.PRICE_LEADS): {
            'signal': '价格爆发-独舞',
            'priority': 2,
            'action': '[爆发]只做龙头，不补涨',
            'position': 'medium',
            'risk': '跟风已死，只做龙头'
        },
        (TrendStage.EXPLOSION, ResonanceType.WEAK): {
            'signal': '弱爆发',
            'priority': 3,
            'action': '[爆发]谨慎参与',
            'position': 'light',
            'risk': '板块效应弱，容易分化'
        },
        
        # 加速期组合
        (TrendStage.ACCELERATION, ResonanceType.STRONG): {
            'signal': '强共振加速',
            'priority': 2,
            'action': '[加速]做龙头分歧转一致',
            'position': 'medium',
            'risk': '后期，精选个股'
        },
        (TrendStage.ACCELERATION, ResonanceType.PRICE_LEADS): {
            'signal': '价格加速-独舞',
            'priority': 2,
            'action': '[加速]只做龙头，不补涨',
            'position': 'medium',
            'risk': '跟风已死，只做龙头'
        },
        (TrendStage.ACCELERATION, ResonanceType.QUANTITY_LEADS): {
            'signal': '数量加速-虚热',
            'priority': 3,
            'action': '[加速]只做龙头',
            'position': 'light',
            'risk': '大票未跟上，持续性存疑'
        },
        
        # 确认期组合
        (TrendStage.CONFIRMED, ResonanceType.STRONG): {
            'signal': '强共振确认',
            'priority': 3,
            'action': '[确认]做核心龙头，不杂毛',
            'position': 'medium',
            'risk': '主线后期，控制仓位'
        },
        (TrendStage.CONFIRMED, ResonanceType.WEAK): {
            'signal': '弱确认-分化',
            'priority': 4,
            'action': '[确认]精选个股，非全面参与',
            'position': 'light',
            'risk': '板块分化，多数股跌'
        },
        
        # 成熟期组合（统一降级）
        (TrendStage.MATURE, ResonanceType.STRONG): {
            'signal': '成熟期末期',
            'priority': 4,
            'action': '[成熟]减仓，只留龙头',
            'position': 'light',
            'risk': '随时退潮，警惕'
        },
        (TrendStage.MATURE, ResonanceType.ANY): {
            'signal': '成熟期-回避',
            'priority': 5,
            'action': '[成熟]不介入，等退潮后',
            'position': 'none',
            'risk': '高位震荡，风险大于机会'
        },
        
        # 退潮期组合（统一回避）
        (TrendStage.DECLINE_EARLY, ResonanceType.ANY): {
            'signal': '早期退潮',
            'priority': 1,  # 风险信号也是高优先级
            'action': '[退潮]坚决回避，不抄底',
            'position': 'none',
            'risk': '资金撤离，还有下跌空间'
        },
        (TrendStage.DECLINE_LATE, ResonanceType.ANY): {
            'signal': '晚期退潮',
            'priority': 1,
            'action': '[退潮]彻底放弃，等下一轮',
            'position': 'none',
            'risk': '已确认死亡，不关注'
        },
        
        # 观察期
        (TrendStage.WATCH, ResonanceType.STRONG): {
            'signal': '观察-待爆发',
            'priority': 4,
            'action': '[观察]加入观察池，等启动',
            'position': 'none',
            'risk': '提前埋伏，可能等待较久'
        },
    }

    def __init__(self, weights: Dict = None, thresholds: Dict = None):
        # 使用配置文件中的参数，允许通过参数覆盖
        self.weights = weights or {**SECTOR_HEAT_WEIGHTS, **SECTOR_HEAT_THRESHOLDS}
        self.declining_sectors = []  # 存储退潮板块信息
    
    def get_declining_sectors(self) -> pd.DataFrame:
        """获取退潮板块列表（今日无涨停但前几日有数据的板块）"""
        if not self.declining_sectors:
            return pd.DataFrame()
        return pd.DataFrame(self.declining_sectors)
    
    def _classify_trend_stage(self, today: int, yesterday: int, d3: int, d5: int, d20: int) -> TrendStage:
        """判断趋势阶段"""
        # 启动：昨日0→今日有
        if yesterday == 0 and today >= 2:
            return TrendStage.START
        
        # 爆发：倍增（使用配置阈值）
        explosion_threshold = self.weights.get('explosion_threshold', 1.5)
        explosion_min_today = self.weights.get('explosion_min_today', 2)
        if yesterday > 0 and today >= yesterday * explosion_threshold and today >= explosion_min_today:
            return TrendStage.EXPLOSION
        
        # 加速：3日>5日，持续增
        acceleration_threshold = self.weights.get('acceleration_threshold', 0.25)
        acceleration_min_3d = self.weights.get('acceleration_min_3d', 3)
        avg_3d = d3 / 3 if d3 > 0 else 0
        avg_5d = d5 / 5 if d5 > 0 else 0
        momentum = (avg_3d - avg_5d) / avg_5d if avg_5d > 0 else 0
        if momentum >= acceleration_threshold and d3 >= acceleration_min_3d and today >= 2:
            return TrendStage.ACCELERATION
        
        # 确认：多周期共振
        confirmed_min_3d = self.weights.get('confirmed_min_3d', 5)
        if d3 >= confirmed_min_3d and d5 >= confirmed_min_3d + 2 and d20 >= 10 and momentum > 0:
            return TrendStage.CONFIRMED
        
        # 退潮：3日骤降
        decline_3d_threshold = self.weights.get('decline_3d_threshold', -0.20)
        if momentum <= decline_3d_threshold and d3 < yesterday:
            return TrendStage.DECLINE_EARLY
        
        # 晚期退潮：5日也降
        if d5 < d20 * 0.3 and d20 > 0:
            return TrendStage.DECLINE_LATE
        
        return TrendStage.WATCH
    
    def _classify_resonance(self, zt_count: int, sector_change: float = 0.0, 
                           large_cap_change: float = 0.0, zt_avg: float = 9.8) -> ResonanceType:
        """判断共振类型"""
        # 强共振：数量多+板块涨+大票动
        if (zt_count >= 5 and sector_change >= 0.03 and 
            large_cap_change >= 0.02 and zt_avg >= 9.8):
            return ResonanceType.STRONG
        
        # 数量引领：数量多但板块一般
        if zt_count >= 5 and sector_change < 0.03:
            return ResonanceType.QUANTITY_LEADS
        
        # 价格引领：数量少但板块涨
        if zt_count < 5 and zt_count >= 2 and sector_change >= 0.03:
            return ResonanceType.PRICE_LEADS
        
        # 弱共振：都弱
        if zt_count >= 2 and sector_change >= 0.015:
            return ResonanceType.WEAK
        
        return ResonanceType.NONE
    
    def _lookup_decision(self, trend: TrendStage, resonance: ResonanceType) -> Dict:
        """查询决策矩阵"""
        key = (trend, resonance)
        
        # 精确匹配
        if key in self.DECISION_MATRIX:
            return self.DECISION_MATRIX[key]
        
        # 模糊匹配（ANY）
        for (t, r), decision in self.DECISION_MATRIX.items():
            if t == trend and r == ResonanceType.ANY:
                return decision
        
        # 默认
        return {
            'signal': f'{trend.value}-{resonance.value}',
            'priority': 5,
            'action': '观察',
            'position': 'none',
            'risk': '不明确状态'
        }
    
    def calculate_sector_heat_v2(self, 
                                today_count: int,           # 当日涨停数（T+0）
                                yesterday_count: int,        # 昨日涨停数
                                rolling_2d: int,             # 2日滚动（今+昨）
                                rolling_3d: int,             # 3日滚动（今+昨+前2）
                                rolling_5d: int,             # 5日滚动（今+最近4天）
                                rolling_20d: int,            # 20日滚动（今+最近19天）
                                continuity_days: int,
                                l1_name: str,
                                l2_name: str,
                                # 共振指标（可选）
                                sector_change: float = 0.0,
                                large_cap_change: float = 0.0,
                                zt_avg_change: float = 9.8) -> Optional[SectorSignal]:
        """
        计算单个板块热度，返回散户可直接使用的信号
        【增强】支持趋势阶段 × 共振强度的联动判断
        【改进】滚动统计包含今日数据（T+0）
        
        核心改进：
        - 当日数据权重最高（T+0）
        - 2日滚动（今+昨）：判断短期趋势
        - 3日滚动（今+昨+前2）：判断热度持续性
        - 5日/20日滚动：判断中长期趋势
        - 退潮预警更灵敏（3日-30%即预警）
        - 只输出 actionable 信号，忽略噪音
        """
        
        # 过滤：当日涨停太少，直接忽略
        if today_count < self.weights['min_today_count']:
            return None
        
        # ========== 核心指标计算 ==========
        
        # 1. 当日爆发力（最关键）= 今日 / 昨日
        today_explosion = today_count / max(yesterday_count, 1)
        
        # 2. 短期动量（3日滚动 vs 5日滚动）
        avg_3d = rolling_3d / 3 if rolling_3d > 0 else 0
        avg_5d = rolling_5d / 5 if rolling_5d > 0 else 0
        momentum_3d_5d = (avg_3d - avg_5d) / avg_5d if avg_5d > 0 else 0
        
        # 3. 中期趋势（5日滚动 vs 20日滚动）
        avg_20d = rolling_20d / 20 if rolling_20d > 0 else 0
        trend_5d_20d = (avg_5d - avg_20d) / avg_20d if avg_20d > 0 else 0
        
        # 4. 综合热度得分
        raw_score = (
            today_count * self.weights['today_weight'] +
            rolling_3d * self.weights['weight_3d'] / 3 +
            rolling_5d * self.weights['weight_5d'] / 5 +
            rolling_20d * self.weights['weight_20d'] / 20
        ) * 10  # 放大10倍
        
        # 爆发力系数（当日突增加分）
        explosion_factor = min(today_explosion, 3.0)  # 最高3倍
        total_score = raw_score * explosion_factor
        
        # ========== 状态判断（新版：趋势阶段 × 共振强度） ==========
        
        # 1. 判断趋势阶段（使用滚动统计）
        trend = self._classify_trend_stage(
            today_count, yesterday_count, rolling_3d, rolling_5d, rolling_20d
        )
        
        # 2. 判断共振类型（使用传入的参数或估算）
        # 如果没有传入板块涨幅数据，根据涨停数量估算
        estimated_sector_change = sector_change if sector_change > 0 else (0.05 if today_count >= 5 else 0.02)
        estimated_large_cap = large_cap_change if large_cap_change > 0 else estimated_sector_change * 0.7
        resonance = self._classify_resonance(
            today_count, estimated_sector_change, estimated_large_cap, zt_avg_change
        )
        
        # 3. 查询联动决策矩阵
        decision = self._lookup_decision(trend, resonance)
        
        # 4. 如果决策建议不操作（position=none）且不是风险信号，则忽略
        if decision['position'] == 'none' and '退潮' not in trend.value:
            return None
        
        # 5. 构建信号（包含联动状态字段）
        return SectorSignal(
            l2_name=l2_name,
            l1_name=l1_name,
            action=decision['action'],
            priority=decision['priority'],
            confidence=min(0.9, 0.6 + today_explosion * 0.1 + (0.1 if resonance == ResonanceType.STRONG else 0)),
            key_metrics={
                "今日涨停": today_count,
                "昨日涨停": yesterday_count,
                "2日滚动": rolling_2d,
                "3日滚动": rolling_3d,
                "5日滚动": rolling_5d,
                "20日滚动": rolling_20d,
                "爆发倍数": f"{today_explosion:.1f}x",
                "短期动量": f"{momentum_3d_5d:.1%}",
                "综合得分": f"{total_score:.1f}",
                "趋势阶段": trend.value,
                "共振类型": resonance.value,
            },
            watch_reason=f"{trend.value} + {resonance.value}，联动信号：{decision['signal']}",
            risk_warning=decision['risk'],
            # 联动状态字段
            trend_stage=trend,
            resonance_type=resonance,
            combined_signal=decision['signal'],
            position_size=decision['position']
        )
    
    def analyze_all_sectors_v2(self,
                               today_zt: pd.DataFrame,           # 当日涨停池
                               history_pools: Dict[str, pd.DataFrame],  # 历史涨停池
                               industry_mapper=None) -> pd.DataFrame:
        """
        分析所有板块，返回散户聚焦的信号列表
        
        改进：
        - 必须传入当日涨停池（T+0）
        - 只输出 actionable 信号（爆发、加速、退潮、待爆发）
        - 按优先级排序，散户只看前5
        """
        # 1. 统计当日各板块涨停数
        today_sector_counts = self._count_by_sector(today_zt, industry_mapper)
        logger.info(f"当日涨停统计: {len(today_sector_counts)}个板块, 总涨停数: {sum(today_sector_counts.values())}")
        # 显示前10个板块
        sorted_today = sorted(today_sector_counts.items(), key=lambda x: x[1], reverse=True)
        for l3, count in sorted_today[:10]:
            logger.info(f"  {l3}: {count}只")
        
        # 2. 统计历史数据（包含今日）
        sector_stats = self._calculate_history_stats(history_pools, today_zt, industry_mapper)
        logger.info(f"历史数据统计: {len(sector_stats)}个板块")
        
        # 3. 合并并计算信号
        signals = []
        self.declining_sectors = []  # 清空并重新收集退潮板块
        debug_info = []  # 调试信息
        all_sectors = set(today_sector_counts.keys()) | set(sector_stats.keys())
        
        for l2_name in all_sectors:
            today_count = today_sector_counts.get(l2_name, 0)
            stats = sector_stats.get(l2_name, {})
            
            yesterday_count = stats.get('yesterday_count', 0)
            rolling_2d = stats.get('rolling_2d', 0)  # 今+昨
            rolling_3d = stats.get('rolling_3d', 0)  # 今+昨+前2
            rolling_5d = stats.get('rolling_5d', 0)  # 今+最近4天
            rolling_20d = stats.get('rolling_20d', 0)  # 今+最近19天
            continuity_days = stats.get('continuity_days', 0)
            
            l1 = stats.get('L1', '未知')
            l2 = stats.get('L2', '未知')
            
            # 计算核心指标用于调试
            today_explosion = today_count / max(yesterday_count, 1)
            avg_3d = rolling_3d / 3 if rolling_3d > 0 else 0
            avg_5d = rolling_5d / 5 if rolling_5d > 0 else 0
            momentum_3d_5d = (avg_3d - avg_5d) / avg_5d if avg_5d > 0 else 0
            
            # 记录调试信息 - 使用与 _classify_trend_stage 相同的逻辑判断实际阶段
            trend_stage = self._classify_trend_stage(
                today_count, yesterday_count, rolling_3d, rolling_5d, rolling_20d
            )
            
            debug_info.append({
                '板块': l2_name,
                '今日': today_count,
                '昨日': yesterday_count,
                '2日滚动': rolling_2d,
                '3日滚动': rolling_3d,
                '5日滚动': rolling_5d,
                '20日滚动': rolling_20d,
                '爆发倍数': f"{today_explosion:.2f}",
                '短期动量': f"{momentum_3d_5d:.2%}",
                '趋势阶段': trend_stage.value  # 只显示实际判断的阶段
            })
            
            # 收集退潮板块：今日无涨停但3日滚动>=3（说明前几天有）
            if today_count == 0 and rolling_3d >= 3:
                self.declining_sectors.append({
                    '板块': l2_name,
                    '一级行业': l1,
                    '3日滚动': rolling_3d,
                    '5日滚动': rolling_5d,
                    '昨日涨停': yesterday_count,
                    '趋势阶段': trend_stage.value,
                    '说明': f'前几日有{rolling_3d}只涨停，今日无涨停，可能退潮'
                })
            
            # 计算信号（使用滚动统计）
            signal = self.calculate_sector_heat_v2(
                today_count, yesterday_count, rolling_2d, rolling_3d, 
                rolling_5d, rolling_20d, continuity_days, l1, l2
            )
            
            if signal:
                signals.append(signal)
        
        # 输出调试信息
        logger.info("="*70)
        logger.info("【板块热度调试信息】")
        logger.info(f"总板块数: {len(debug_info)}, 触发信号数: {len(signals)}")
        logger.info(f"当前阈值配置:")
        logger.info(f"  爆发阈值: {self.weights['explosion_threshold']}倍 (需今日>={self.weights.get('explosion_min_today', 2)}只)")
        logger.info(f"  加速阈值: {self.weights['acceleration_threshold']:.0%} (需3日滚动>={self.weights.get('acceleration_min_3d', 3)}只)")
        logger.info(f"  确认期: 动量>0 (需今日>={self.weights.get('confirmed_min_today', 2)}只, 3日滚动>={self.weights.get('confirmed_min_3d', 5)}只)")
        logger.info(f"  观察期: 动量5%-50% (需昨日>={self.weights.get('watch_min_yesterday', 1)}只, 5日滚动>=4只)")
        logger.info(f"  退潮阈值: {self.weights['decline_3d_threshold']:.0%}")
        logger.info(f"  最小关注数: {self.weights['min_today_count']}只")
        logger.info("-"*70)
        
        # 按今日涨停数排序显示前15个板块
        debug_info.sort(key=lambda x: x['今日'], reverse=True)
        for info in debug_info[:15]:
            trend_stage = info['趋势阶段']
            logger.info(f"{info['板块']}: 今日{info['今日']}只, 昨日{info['昨日']}只, "
                       f"3日滚动{info['3日滚动']}只, 爆发{info['爆发倍数']}倍, 动量{info['短期动量']} [{trend_stage}]")
        
        # 显示退潮板块
        if self.declining_sectors:
            logger.info("-"*70)
            logger.info(f"【退潮板块】今日无涨停但前几日有数据的板块（共{len(self.declining_sectors)}个）:")
            # 按3日滚动排序
            self.declining_sectors.sort(key=lambda x: x['3日滚动'], reverse=True)
            for ds in self.declining_sectors[:10]:  # 显示前10个
                logger.info(f"  {ds['板块']}: 3日滚动{ds['3日滚动']}只, 昨日{ds['昨日涨停']}只 [{ds['趋势阶段']}]")
        
        logger.info("="*70)
        
        # 4. 按优先级排序，只取前5（散户聚焦）
        signals.sort(key=lambda x: (x.priority, -x.confidence))
        top_signals = signals[:]
        
        # 5. 转换为DataFrame（包含新的联动状态字段）
        result = []
        for sig in top_signals:
            result.append({
                '优先级': sig.priority,
                '联动信号': sig.combined_signal or (sig.trend_stage.value if sig.trend_stage else ''),
                '趋势阶段': sig.trend_stage.value if sig.trend_stage else '',
                '共振类型': sig.resonance_type.value if sig.resonance_type else '',
                '一级行业': sig.l1_name,
                '二级行业': sig.l2_name,
                '行动建议': sig.action,
                '仓位建议': sig.position_size or 'light',
                '置信度': f"{sig.confidence:.0%}",
                '核心指标': str(sig.key_metrics),
                '关注理由': sig.watch_reason,
                '风险提示': sig.risk_warning
            })
        
        return pd.DataFrame(result)
    
    def _count_by_sector(self, zt_df: pd.DataFrame, mapper=None) -> Dict[str, int]:
        """统计当日各板块涨停数"""
        if zt_df.empty:
            return {}

        # 如果没有L2_Industry列，尝试用mapper映射
        if 'L2_Industry' not in zt_df.columns:
            if mapper:
                try:
                    zt_df = mapper.build_hierarchy_dataframe(zt_df)
                except Exception as e:
                    logger.warning(f"行业映射失败: {e}")

        # 检查是否有L2_Industry列或所属行业列
        industry_col = None
        if 'L2_Industry' in zt_df.columns:
            industry_col = 'L2_Industry'
        elif '所属行业' in zt_df.columns:
            industry_col = '所属行业'

        if industry_col is None:
            logger.warning("涨停池数据缺少行业列（L2_Industry或所属行业）")
            return {}

        # 过滤空值和"其他"行业
        valid_df = zt_df[zt_df[industry_col].notna() & (zt_df[industry_col] != '其他')]

        if valid_df.empty:
            return {}

        return valid_df.groupby(industry_col).size().to_dict()
    
    def _calculate_history_stats(self, history_pools: Dict, today_zt: pd.DataFrame = None, mapper=None) -> Dict:
        """
        计算历史统计数据 - 滚动统计方式（包含今日）

        统计维度（包含今日）：
        - yesterday_count: 昨日涨停数（用于计算爆发力）
        - rolling_2d: 2日滚动（今+昨）
        - rolling_3d: 3日滚动（今+昨+前2）
        - rolling_5d: 5日滚动（今+最近4天）
        - rolling_20d: 20日滚动（今+最近19天）
        """
        stats = defaultdict(lambda: {
            'yesterday_count': 0,
            'rolling_2d': 0,
            'rolling_3d': 0,
            'rolling_5d': 0,
            'rolling_20d': 0,
            'continuity_days': 0,
            'daily_counts': {},
            'L1': '未知',
            'L2': '未知'
        })

        # 辅助函数：获取行业列名
        def get_industry_col(df):
            if 'L2_Industry' in df.columns:
                return 'L2_Industry'
            elif '所属行业' in df.columns:
                return '所属行业'
            return None

        # 辅助函数：处理DataFrame获取行业
        def process_df(df, is_today=False):
            if df.empty:
                return df, None

            # 尝试映射行业
            if mapper:
                try:
                    if 'L2_Industry' not in df.columns:
                        df = mapper.build_hierarchy_dataframe(df)
                except Exception as e:
                    logger.debug(f"行业映射失败: {e}")

            industry_col = get_industry_col(df)
            return df, industry_col

        # 1. 先统计今日数据（如果传入）
        if today_zt is not None and not today_zt.empty:
            df, industry_col = process_df(today_zt, is_today=True)

            if industry_col:
                for _, row in df.iterrows():
                    industry = row.get(industry_col, '其他')
                    if industry == '其他' or pd.isna(industry):
                        continue

                    # 今日数据计入所有滚动统计
                    stats[industry]['rolling_2d'] += 1
                    stats[industry]['rolling_3d'] += 1
                    stats[industry]['rolling_5d'] += 1
                    stats[industry]['rolling_20d'] += 1

                    if 'L1_Industry' in row:
                        stats[industry]['L1'] = row['L1_Industry']
                    if 'L2_Industry' in row:
                        stats[industry]['L2'] = row['L2_Industry']

        # 2. 统计历史数据
        dates = sorted(history_pools.keys(), reverse=True)

        for idx, date in enumerate(dates):
            df = history_pools[date]
            if df.empty:
                continue

            df, industry_col = process_df(df)
            if not industry_col:
                continue

            for _, row in df.iterrows():
                industry = row.get(industry_col, '其他')
                if industry == '其他' or pd.isna(industry):
                    continue

                # idx=0 是昨日（最近一天）
                if idx == 0:
                    stats[industry]['yesterday_count'] += 1

                # 滚动统计（历史数据累加）
                # rolling_2d = 今日 + 昨日
                if idx < 1:
                    stats[industry]['rolling_2d'] += 1
                # rolling_3d = 今日 + 昨日 + 前2天
                if idx < 2:
                    stats[industry]['rolling_3d'] += 1
                # rolling_5d = 今日 + 最近4天
                if idx < 4:
                    stats[industry]['rolling_5d'] += 1
                # rolling_20d = 今日 + 最近19天
                if idx < 19:
                    stats[industry]['rolling_20d'] += 1

                stats[industry]['daily_counts'][date] = stats[industry]['daily_counts'].get(date, 0) + 1

                if 'L1_Industry' in row:
                    stats[industry]['L1'] = row['L1_Industry']
                if 'L2_Industry' in row:
                    stats[industry]['L2'] = row['L2_Industry']

        # 计算持续天数
        for industry, s in stats.items():
            sorted_dates = sorted(s['daily_counts'].keys(), reverse=True)
            continuity = 0
            for date in sorted_dates:
                if s['daily_counts'].get(date, 0) > 0:
                    continuity += 1
                else:
                    break
            s['continuity_days'] = continuity

        return dict(stats)


# ==================== 使用示例 ====================

if __name__ == "__main__":
    calculator = SectorHeatCalculatorV2()
    
    # 模拟数据测试
    test_cases = [
        # (今日, 昨日, 3日, 5日, 20日, 持续天数, L1, L2, L2)
        (8, 3, 15, 20, 40, 5, '电力设备', '光伏设备', '光伏组件'),  # 爆发期
        (2, 2, 3, 8, 50, 2, '电力设备', '电池', '锂电池'),         # 早期退潮
        (5, 3, 12, 10, 25, 4, '计算机', '软件', '人工智能'),       # 加速期
        (3, 3, 10, 12, 30, 6, '电子', '半导体', '芯片设计'),       # 确认期
        (2, 2, 5, 8, 20, 3, '汽车', '零部件', '汽车电子'),         # 观察-待爆发
    ]
    
    print("="*70)
    print("板块热度计算器V2 - 散户聚焦版")
    print("="*70)
    
    for case in test_cases:
        today, yest, d3, d5, d20, cont, l1, l2, l3 = case
        signal = calculator.calculate_sector_heat_v2(
            today, yest, d3, d5, d20, cont, l1, l2, l3
        )
        
        if signal:
            trend_value = signal.trend_stage.value if signal.trend_stage else '未知'
            print(f"【优先级{signal.priority}】{trend_value}")
            print(f"  板块: {l1} > {l2} > {l3}")
            print(f"  行动: {signal.action}")
            print(f"  置信度: {signal.confidence:.0%}")
            print(f"  指标: {signal.key_metrics}")
            print(f"  理由: {signal.watch_reason}")
            print(f"  风险: {signal.risk_warning}")
        else:
            print(f"【忽略】{l3} - 无明确信号，减少噪音")
    
    print("" + "="*70)
    print("核心改进：")
    print("1. 当日数据权重35%（T+0灵敏度）")
    print("2. 3日-30%即预警早期退潮（更灵敏）")
    print("3. 只输出前5优先级信号（散户聚焦）")
    print("4. 直接给出行动建议（ actionable ）")
    print("="*70)
