"""
板块状态联动系统 - 趋势阶段 × 共振强度
"""
from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, Dict, List
import pandas as pd

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

# ========== 联动状态组合 ==========

@dataclass
class SectorState:
    """板块完整状态"""
    trend: TrendStage           # 趋势阶段
    resonance: ResonanceType    # 共振类型
    combined_signal: str        # 联动信号（趋势+共振）
    priority: int               # 优先级（1-5，1最高）
    action: str                 # 具体行动
    position_size: str          # 仓位建议
    risk_warning: str           # 风险提示
    key_metrics: Dict           # 核心指标

class SectorStateEngine:
    """
    板块状态联动引擎
    
    输入：趋势指标 + 共振指标
    输出：联动状态 + 行动建议
    """
    
    # 联动决策矩阵（趋势 × 共振 = 行动）
    DECISION_MATRIX = {
        # 启动期组合
        (TrendStage.START, ResonanceType.STRONG): {
            'signal': '强共振启动',
            'priority': 1,
            'action': '🔥立即重仓，做首板/二板',
            'position': 'heavy',
            'risk': '最佳机会，次日有溢价'
        },
        (TrendStage.START, ResonanceType.QUANTITY_LEADS): {
            'signal': '数量启动-虚热',
            'priority': 2,
            'action': '⚠️只做龙头，不做跟风',
            'position': 'light',
            'risk': '大票未动，持续性存疑'
        },
        (TrendStage.START, ResonanceType.PRICE_LEADS): {
            'signal': '价格启动-独舞',
            'priority': 2,
            'action': '🎯只做龙头，放弃跟风',
            'position': 'medium',
            'risk': '缺乏梯队，龙头炸则全崩'
        },
        (TrendStage.START, ResonanceType.WEAK): {
            'signal': '弱启动',
            'priority': 4,
            'action': '👁️1成仓试探或观察',
            'position': 'light',
            'risk': '可能一日游，严格止损'
        },
        
        # 爆发期组合
        (TrendStage.EXPLOSION, ResonanceType.STRONG): {
            'signal': '强共振爆发',
            'priority': 1,
            'action': '🚀积极参与，做前排',
            'position': 'heavy',
            'risk': '加速期，注意分歧'
        },
        (TrendStage.EXPLOSION, ResonanceType.QUANTITY_LEADS): {
            'signal': '数量爆发-虚热警告',
            'priority': 3,
            'action': '❌回避，小票乱炒',
            'position': 'none',
            'risk': '虚热，次日分化严重'
        },
        
        # 加速期组合
        (TrendStage.ACCELERATION, ResonanceType.STRONG): {
            'signal': '强共振加速',
            'priority': 2,
            'action': '✅做龙头分歧转一致',
            'position': 'medium',
            'risk': '后期，精选个股'
        },
        (TrendStage.ACCELERATION, ResonanceType.PRICE_LEADS): {
            'signal': '价格加速-独舞',
            'priority': 2,
            'action': '🎯只做龙头，不补涨',
            'position': 'medium',
            'risk': '跟风已死，只做龙头'
        },
        
        # 确认期组合
        (TrendStage.CONFIRMED, ResonanceType.STRONG): {
            'signal': '强共振确认',
            'priority': 3,
            'action': '📈做核心龙头，不杂毛',
            'position': 'medium',
            'risk': '主线后期，控制仓位'
        },
        (TrendStage.CONFIRMED, ResonanceType.WEAK): {
            'signal': '弱确认-分化',
            'priority': 4,
            'action': '🔍精选个股，非全面参与',
            'position': 'light',
            'risk': '板块分化，多数股跌'
        },
        
        # 成熟期组合（统一降级）
        (TrendStage.MATURE, ResonanceType.STRONG): {
            'signal': '成熟期末期',
            'priority': 4,
            'action': '⚠️减仓，只留龙头',
            'position': 'light',
            'risk': '随时退潮，警惕'
        },
        (TrendStage.MATURE, ResonanceType.ANY): {
            'signal': '成熟期-回避',
            'priority': 5,
            'action': '❌不介入，等退潮后',
            'position': 'none',
            'risk': '高位震荡，风险大于机会'
        },
        
        # 退潮期组合（统一回避）
        (TrendStage.DECLINE_EARLY, ResonanceType.ANY): {
            'signal': '早期退潮',
            'priority': 1,  # 风险信号也是高优先级
            'action': '🚨坚决回避，不抄底',
            'position': 'none',
            'risk': '资金撤离，还有下跌空间'
        },
        (TrendStage.DECLINE_LATE, ResonanceType.ANY): {
            'signal': '晚期退潮',
            'priority': 1,
            'action': '❌彻底放弃，等下一轮',
            'position': 'none',
            'risk': '已确认死亡，不关注'
        },
        
        # 观察期
        (TrendStage.WATCH, ResonanceType.STRONG): {
            'signal': '观察-待爆发',
            'priority': 4,
            'action': '👀加入观察池，等启动',
            'position': 'none',
            'risk': '提前埋伏，可能等待较久'
        },
    }
    
    def calculate_combined_state(self,
                                  # 趋势指标
                                  today_zt: int,
                                  yesterday_zt: int,
                                  count_3d: int,
                                  count_5d: int,
                                  count_20d: int,
                                  # 共振指标
                                  sector_change: float,
                                  large_cap_change: float,
                                  zt_avg_change: float) -> SectorState:
        """
        计算联动状态
        """
        # 1. 判断趋势阶段
        trend = self._classify_trend_stage(
            today_zt, yesterday_zt, count_3d, count_5d, count_20d
        )
        
        # 2. 判断共振类型
        resonance = self._classify_resonance(
            today_zt, sector_change, large_cap_change, zt_avg_change
        )
        
        # 3. 查询决策矩阵
        decision = self._lookup_decision(trend, resonance)
        
        return SectorState(
            trend=trend,
            resonance=resonance,
            combined_signal=decision['signal'],
            priority=decision['priority'],
            action=decision['action'],
            position_size=decision['position'],
            risk_warning=decision['risk'],
            key_metrics={
                '今日涨停': today_zt,
                '昨日涨停': yesterday_zt,
                '3日累计': count_3d,
                '板块涨幅': f"{sector_change*100:.1f}%",
                '大票涨幅': f"{large_cap_change*100:.1f}%",
                '趋势阶段': trend.value,
                '共振类型': resonance.value
            }
        )
    
    def _classify_trend_stage(self, today, yesterday, d3, d5, d20) -> TrendStage:
        """判断趋势阶段"""
        # 启动：昨日0→今日有
        if yesterday == 0 and today >= 2:
            return TrendStage.START
        
        # 爆发：倍增
        if yesterday > 0 and today >= yesterday * 2 and today >= 3:
            return TrendStage.EXPLOSION
        
        # 加速：3日>5日，持续增
        if d3 > d5 * 0.8 and today >= 2:
            return TrendStage.ACCELERATION
        
        # 确认：多周期共振
        if d3 >= 8 and d5 >= 10 and d20 >= 15:
            return TrendStage.CONFIRMED
        
        # 退潮：3日骤降
        if d3 < d5 * 0.5 and today < yesterday * 0.7:
            return TrendStage.DECLINE_EARLY
        
        # 晚期退潮：5日也降
        if d5 < d20 * 0.3:
            return TrendStage.DECLINE_LATE
        
        return TrendStage.WATCH
    
    def _classify_resonance(self, zt_count, sector_change, large_cap_change, zt_avg) -> ResonanceType:
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

# ==================== 实战输出 ====================

def generate_linked_report(states: List[SectorState]) -> str:
    """
    生成联动状态报告（散户极简版）
    """
    # 按优先级排序
    states.sort(key=lambda x: (x.priority, -x.key_metrics.get('今日涨停', 0)))
    
    report = []
    report.append("=" * 70)
    report.append("板块联动状态报告")
    report.append("=" * 70)
    
    # 立即行动（优先级1-2）
    urgent = [s for s in states if s.priority <= 2]
    if urgent:
        report.append(f"\n🔥【立即行动】{len(urgent)}个板块")
        for s in urgent:
            report.append(f"\n【{s.combined_signal}】{s.key_metrics.get('L3行业', '未知')}")
            report.append(f"  趋势：{s.trend.value} | 共振：{s.resonance.value}")
            report.append(f"  涨停：{s.key_metrics.get('今日涨停', 0)}只 | "
                         f"板块{s.key_metrics.get('板块涨幅', 'N/A')}")
            report.append(f"  👉 {s.action}")
            report.append(f"  仓位：{s.position_size} | ⚠️ {s.risk_warning}")
    
    # 观察池（优先级3-4）
    watch = [s for s in states if 3 <= s.priority <= 4]
    if watch:
        report.append(f"\n👀【观察池】{len(watch)}个板块")
        for s in watch:
            report.append(f"  • {s.combined_signal}：{s.action}")
    
    # 回避（风险信号）
    avoid = [s for s in states if '退潮' in s.trend.value]
    if avoid:
        report.append(f"\n❌【坚决回避】{len(avoid)}个板块")
        for s in avoid:
            report.append(f"  • {s.key_metrics.get('L3行业', '未知')}：{s.trend.value}")
    
    report.append("\n" + "=" * 70)
    return "\n".join.join(report)

# ==================== 测试 ====================

if __name__ == "__main__":
    engine = SectorStateEngine()
    
    # 测试案例：强共振启动
    state1 = engine.calculate_combined_state(
        today_zt=6, yesterday_zt=0, count_3d=6, count_5d=6, count_20d=6,
        sector_change=0.04, large_cap_change=0.03, zt_avg_change=10.0
    )
    
    # 测试案例：数量爆发-虚热
    state2 = engine.calculate_combined_state(
        today_zt=8, yesterday_zt=2, count_3d=12, count_5d=10, count_20d=8,
        sector_change=0.015, large_cap_change=0.005, zt_avg_change=9.5
    )
    
    print("案例1：强共振启动")
    print(f"  联动信号：{state1.combined_signal}")
    print(f"  行动：{state1.action}")
    print(f"  仓位：{state1.position_size}")
    
    print("\n案例2：数量爆发-虚热")
    print(f"  联动信号：{state2.combined_signal}")
    print(f"  行动：{state2.action}")
    print(f"  风险：{state2.risk_warning}")