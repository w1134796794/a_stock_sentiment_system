"""
Layer 4: 交易计划层 - 定计划（定执行）

核心职责：
  1. 基于筛选结果生成交易计划
  2. 竞价条件设定（高开幅度、竞价量能等）
  3. 仓位矩阵计算（大盘环境 × 情绪周期 × 信号强度）
  4. 差异化止损止盈策略
  5. 次日预期与风险提示

输入：排序后的交易信号、大盘环境、情绪周期
输出：交易计划表、竞价观察清单、仓位建议
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from core.utils.date_utils import DateUtils
from datetime import timedelta
import loguru

logger = loguru.logger


class PositionLevel(Enum):
    """仓位等级"""
    HEAVY = "重仓"
    NORMAL = "正常"
    LIGHT = "轻仓"
    OBSERVE = "观察"
    AVOID = "回避"


class AuctionCondition(Enum):
    """竞价条件"""
    STRONG_OPEN = "强势高开"
    NORMAL_OPEN = "平开/小幅高开"
    WEAK_OPEN = "低开"
    ANY = "不限"


@dataclass
class TradePlan:
    """单笔交易计划"""
    stock_code: str = ""
    stock_name: str = ""
    pattern_type: str = ""
    priority: int = 0
    composite_score: float = 0.0

    position_level: PositionLevel = PositionLevel.OBSERVE
    position_pct: float = 0.0

    auction_condition: AuctionCondition = AuctionCondition.ANY
    auction_gap_min: float = -3.0
    auction_gap_max: float = 9.0
    auction_volume_ratio: float = 1.0

    entry_price_range: str = ""
    stop_loss_pct: float = -5.0
    take_profit_pct: float = 10.0

    next_day_expectation: str = ""
    risk_warning: str = ""
    key_metrics: Dict = field(default_factory=dict)


@dataclass
class TradePlanResult:
    """交易计划结果"""
    trade_date: str = ""

    plans: List[TradePlan] = field(default_factory=list)
    plans_df: pd.DataFrame = field(default_factory=pd.DataFrame)

    auction_watch_list: List[Dict] = field(default_factory=list)

    overall_position_advice: str = ""
    max_position_pct: float = 0.5

    plan_summary: str = ""

    # 新增因子
    market_emotion_divergence: float = 0.0    # F2: 大盘-情绪背离度
    prev_signal_win_rate: float = 0.0         # F3: 昨日信号胜率


class TradePlanLayer:
    """
    Layer 4: 交易计划层

    基于Layer 1-3的分析结果，生成可执行的交易计划
    """

    def __init__(self, data_manager):
        self.dm = data_manager
        self.date_utils = DateUtils()

    def analyze(self, trade_date: str, ranked_signals: List,
                composite_scores: List, market_env=None,
                emotion_cycle: str = "震荡期",
                sector_positions: Dict = None) -> TradePlanResult:
        """
        生成交易计划

        Args:
            trade_date: 交易日期
            ranked_signals: 排序后的交易信号
            composite_scores: 多因子综合评分
            market_env: 大盘环境分析结果
            emotion_cycle: 情绪周期
            sector_positions: 板块地位分析结果

        Returns:
            TradePlanResult: 交易计划结果
        """
        result = TradePlanResult(trade_date=trade_date)

        try:
            score_map = {}
            if composite_scores:
                for cs in composite_scores:
                    key = f"{cs.stock_code}_{cs.pattern_type}"
                    score_map[key] = cs

            for signal in ranked_signals[:20]:
                plan = self._create_single_plan(
                    signal, score_map, market_env, emotion_cycle, sector_positions
                )
                result.plans.append(plan)

            result.plans_df = self._plans_to_dataframe(result.plans)

            result.auction_watch_list = self._generate_auction_watchlist(result.plans)

            result.overall_position_advice, result.max_position_pct = \
                self._calculate_overall_position(market_env, emotion_cycle)

            # F2: 大盘-情绪背离度
            result.market_emotion_divergence = self._calc_market_emotion_divergence(
                market_env, emotion_cycle
            )

            # F3: 昨日信号胜率
            result.prev_signal_win_rate = self._calc_prev_signal_win_rate(trade_date)

            result.plan_summary = self._generate_summary(result)

            logger.info(f"[Layer4] 交易计划生成完成: {len(result.plans)}条计划, "
                       f"建议最大仓位={result.max_position_pct:.0%}")

        except Exception as e:
            logger.error(f"[Layer4] 交易计划生成失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

        return result

    def _create_single_plan(self, signal, score_map: Dict, market_env,
                             emotion_cycle: str, sector_positions: Dict) -> TradePlan:
        """创建单笔交易计划"""
        plan = TradePlan()

        plan.stock_code = getattr(signal, 'stock_code', '')
        plan.stock_name = getattr(signal, 'stock_name', '')
        plan.pattern_type = getattr(signal, 'pattern_type', '')
        plan.priority = getattr(signal, 'priority', 99)

        key = f"{plan.stock_code}_{plan.pattern_type}"
        if key in score_map:
            plan.composite_score = score_map[key].total_score

        plan.position_level, plan.position_pct = self._determine_position(
            plan.composite_score, plan.priority, market_env, emotion_cycle
        )

        plan.auction_condition, plan.auction_gap_min, plan.auction_gap_max = \
            self._determine_auction_condition(plan.pattern_type, plan.composite_score)

        plan.entry_price_range = self._calculate_entry_range(signal)
        plan.stop_loss_pct, plan.take_profit_pct = self._calculate_stop_profit(
            plan.pattern_type, plan.composite_score, emotion_cycle
        )

        plan.next_day_expectation = self._generate_expectation(
            plan.pattern_type, plan.composite_score, emotion_cycle
        )
        plan.risk_warning = self._generate_risk_warning(plan, market_env, emotion_cycle)

        plan.key_metrics = {
            'confidence': getattr(signal, 'confidence', 0),
            'board_height': getattr(signal, 'board_height', 1),
            'sector_rank': getattr(signal, 'sector_rank', 0),
        }

        return plan

    def _determine_position(self, composite_score: float, priority: int,
                             market_env, emotion_cycle: str) -> tuple:
        """确定仓位等级和比例"""
        market_score = getattr(market_env, 'composite_score', 50) if market_env else 50

        if market_score >= 70 and composite_score >= 70 and priority <= 3:
            return PositionLevel.HEAVY, 0.25
        elif market_score >= 50 and composite_score >= 60 and priority <= 5:
            return PositionLevel.NORMAL, 0.15
        elif market_score >= 30 and composite_score >= 50 and priority <= 8:
            return PositionLevel.LIGHT, 0.10
        elif composite_score >= 40:
            return PositionLevel.OBSERVE, 0.05
        else:
            return PositionLevel.AVOID, 0.0

    def _determine_auction_condition(self, pattern_type: str, composite_score: float) -> tuple:
        """确定竞价条件"""
        if '弱转强' in pattern_type:
            return AuctionCondition.STRONG_OPEN, 2.0, 9.0
        elif '首板' in pattern_type:
            return AuctionCondition.NORMAL_OPEN, 0.0, 7.0
        elif '龙头' in pattern_type:
            return AuctionCondition.NORMAL_OPEN, -2.0, 5.0
        else:
            return AuctionCondition.ANY, -3.0, 9.0

    def _calculate_entry_range(self, signal) -> str:
        """计算入场价格区间"""
        entry_price = getattr(signal, 'entry_price', 0)
        if entry_price > 0:
            return f"{entry_price * 0.98:.2f} - {entry_price * 1.02:.2f}"
        return "竞价确定"

    def _calculate_stop_profit(self, pattern_type: str, composite_score: float,
                                emotion_cycle: str) -> tuple:
        """计算止损止盈比例"""
        if emotion_cycle in ['冰点期', '回暖期']:
            stop_loss = -3.0
            take_profit = 8.0
        elif emotion_cycle == '高潮期':
            stop_loss = -5.0
            take_profit = 15.0
        else:
            stop_loss = -4.0
            take_profit = 10.0

        if composite_score >= 80:
            stop_loss = max(stop_loss, -3.0)
            take_profit = max(take_profit, 12.0)

        return stop_loss, take_profit

    def _generate_expectation(self, pattern_type: str, composite_score: float,
                               emotion_cycle: str) -> str:
        """生成次日预期"""
        if composite_score >= 80:
            return "高开高走预期，关注竞价确认"
        elif composite_score >= 60:
            return "震荡走高预期，关注开盘方向"
        else:
            return "需竞价确认，谨慎参与"

    def _generate_risk_warning(self, plan: TradePlan, market_env,
                                emotion_cycle: str) -> str:
        """生成风险提示"""
        warnings = []

        if emotion_cycle == '高潮期':
            warnings.append("情绪高潮期，追高风险大")
        elif emotion_cycle == '退潮期':
            warnings.append("情绪退潮期，注意高位股补跌")

        if plan.position_level == PositionLevel.HEAVY:
            warnings.append("重仓标的，严格止损")

        if plan.priority > 5:
            warnings.append("优先级较低，注意仓位控制")

        market_score = getattr(market_env, 'composite_score', 50) if market_env else 50
        if market_score < 40:
            warnings.append("大盘环境偏弱，降低预期")

        return "; ".join(warnings) if warnings else "无特别风险提示"

    def _generate_auction_watchlist(self, plans: List[TradePlan]) -> List[Dict]:
        """生成竞价观察清单"""
        watchlist = []
        for plan in plans:
            if plan.position_level in [PositionLevel.AVOID, PositionLevel.OBSERVE]:
                continue

            watchlist.append({
                'stock_code': plan.stock_code,
                'stock_name': plan.stock_name,
                'pattern_type': plan.pattern_type,
                'auction_condition': plan.auction_condition.value,
                'gap_range': f"{plan.auction_gap_min:+.1f}% ~ {plan.auction_gap_max:+.1f}%",
                'position_pct': plan.position_pct,
                'priority': plan.priority,
            })

        watchlist.sort(key=lambda x: x['priority'])
        return watchlist

    def _calculate_overall_position(self, market_env, emotion_cycle: str) -> tuple:
        """计算整体仓位建议"""
        market_score = getattr(market_env, 'composite_score', 50) if market_env else 50

        if market_score >= 70 and emotion_cycle in ['冰点期', '回暖期']:
            return "市场环境良好，情绪处于上升期，可积极操作", 0.8
        elif market_score >= 50 and emotion_cycle not in ['退潮期']:
            return "市场环境中性，可正常操作", 0.6
        elif market_score >= 30:
            return "市场环境偏弱，控制仓位", 0.3
        else:
            return "市场环境较差，建议观望或极轻仓", 0.1

    def _plans_to_dataframe(self, plans: List[TradePlan]) -> pd.DataFrame:
        """将交易计划列表转为DataFrame"""
        if not plans:
            return pd.DataFrame()

        records = []
        for p in plans:
            records.append({
                '股票代码': p.stock_code,
                '股票名称': p.stock_name,
                '模式类型': p.pattern_type,
                '优先级': p.priority,
                '综合评分': round(p.composite_score, 1),
                '仓位等级': p.position_level.value,
                '建议仓位': f"{p.position_pct:.0%}",
                '竞价条件': p.auction_condition.value,
                '竞价区间': f"{p.auction_gap_min:+.1f}%~{p.auction_gap_max:+.1f}%",
                '入场区间': p.entry_price_range,
                '止损': f"{p.stop_loss_pct:+.1f}%",
                '止盈': f"{p.take_profit_pct:+.1f}%",
                '次日预期': p.next_day_expectation,
                '风险提示': p.risk_warning,
            })

        return pd.DataFrame(records)

    def _generate_summary(self, result: TradePlanResult) -> str:
        """生成交易计划摘要"""
        lines = []
        lines.append(f"=== 交易计划摘要 ({result.trade_date}) ===")

        lines.append(f"\n💰 整体仓位建议: {result.overall_position_advice}")
        lines.append(f"   最大仓位: {result.max_position_pct:.0%}")

        lines.append(f"\n📋 交易计划: {len(result.plans)}条")
        for i, plan in enumerate(result.plans[:8], 1):
            lines.append(f"   {i}. {plan.stock_name}({plan.stock_code}) "
                        f"- {plan.pattern_type} "
                        f"- {plan.position_level.value}({plan.position_pct:.0%}) "
                        f"- 评分:{plan.composite_score:.0f}")

        lines.append(f"\n🔍 竞价观察清单: {len(result.auction_watch_list)}只")
        for i, item in enumerate(result.auction_watch_list[:5], 1):
            lines.append(f"   {i}. {item['stock_name']}({item['stock_code']}) "
                        f"- {item['auction_condition']} "
                        f"- 仓位:{item['position_pct']:.0%}")

        return "\n".join(lines)

    def _calc_market_emotion_divergence(self, market_env, emotion_cycle: str) -> float:
        """
        F2: 大盘-情绪背离度

        计算市场环境评分与情绪周期之间的背离程度。
        正常情况：大盘好→情绪好，大盘差→情绪差
        背离情况：大盘好但情绪差（警惕），大盘差但情绪好（可能反弹）

        Returns:
            float: 背离度，0=完全一致，100=完全背离
        """
        try:
            market_score = getattr(market_env, 'composite_score', 50) if market_env else 50

            emotion_score_map = {
                '冰点期': 10,
                '回暖期': 40,
                '高潮期': 80,
                '退潮期': 30,
                '震荡期': 50,
            }
            emotion_score = emotion_score_map.get(emotion_cycle, 50)

            divergence = abs(market_score - emotion_score)
            logger.info(f"[Layer4] F2-大盘情绪背离度: {divergence:.1f} "
                        f"(大盘={market_score:.0f}, 情绪={emotion_score})")
            return divergence
        except Exception as e:
            logger.debug(f"[Layer4] F2-背离度计算失败: {e}")
            return 0.0

    def _calc_prev_signal_win_rate(self, trade_date: str) -> float:
        """
        F3: 昨日信号胜率

        统计昨日涨停池中股票今日的表现，计算胜率。
        胜率 = 今日收涨的昨日涨停股数 / 昨日涨停股总数

        Returns:
            float: 胜率 0.0~1.0
        """
        try:
            prev_date = self.date_utils.get_n_trade_dates_before(1, trade_date)

            prev_zt = self.dm.get_limit_up_pool(prev_date)
            if prev_zt is None or prev_zt.empty:
                return 0.0

            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in prev_zt.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None:
                return 0.0

            today_df = self.dm.get_all_stocks_daily(trade_date=trade_date)
            if today_df is None or today_df.empty:
                return 0.0

            today_df['ts_code'] = today_df['ts_code'].astype(str)
            codes = prev_zt[ts_code_col].astype(str).tolist()
            today_sub = today_df[today_df['ts_code'].isin(codes)]

            if today_sub.empty or 'pct_chg' not in today_sub.columns:
                return 0.0

            total = len(today_sub)
            win = int((pd.to_numeric(today_sub['pct_chg'], errors='coerce') > 0).sum())
            win_rate = win / total if total > 0 else 0.0

            logger.info(f"[Layer4] F3-昨日信号胜率: {win_rate:.1%} ({win}/{total})")
            return win_rate
        except Exception as e:
            logger.debug(f"[Layer4] F3-胜率计算失败: {e}")
            return 0.0