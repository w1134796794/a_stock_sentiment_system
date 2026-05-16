"""
Layer 5: 盘后总结 - Review & Feedback Analyzer

职责：新增模块，填补当前空白

功能：
  1. 今日信号回顾
     - 各模式发出了多少信号
     - 信号标的表现如何（涨停/大涨/冲高回落/低开）
     - 按模式统计胜率

  2. 情绪周期变化趋势
     - 近5日情绪周期变化
     - 关键指标趋势图（涨停家数/炸板率/溢价率）

  3. 参数敏感度分析
     - 如果阈值调高/调低，信号数量变化
     - 建议调参方向
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import loguru

logger = loguru.logger


class SignalOutcome(Enum):
    LIMIT_UP = "涨停"
    BIG_GAIN = "大涨(>5%)"
    SMALL_GAIN = "小涨(0-5%)"
    FLAT = "平盘"
    SMALL_LOSS = "小跌(0-5%)"
    BIG_LOSS = "大跌(>5%)"
    LIMIT_DOWN = "跌停"
    UNKNOWN = "未知"


@dataclass
class SignalPerformance:
    """单个信号的表现"""
    pattern_type: str
    stock_code: str
    stock_name: str
    signal_date: str
    confidence: float
    next_day_open_change: float = 0.0    # 次日开盘涨跌幅
    next_day_close_change: float = 0.0   # 次日收盘涨跌幅
    next_day_high_change: float = 0.0    # 次日最高涨跌幅
    outcome: SignalOutcome = SignalOutcome.UNKNOWN
    is_profitable: bool = False          # 是否有盈利机会


@dataclass
class PatternStats:
    """模式统计"""
    pattern_name: str
    total_signals: int = 0
    profitable_signals: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0              # 平均收益率
    max_return: float = 0.0              # 最大收益率
    min_return: float = 0.0              # 最小收益率
    avg_confidence: float = 0.0          # 平均置信度


@dataclass
class EmotionTrend:
    """情绪周期趋势"""
    date: str
    cycle_name: str
    limit_up_count: int = 0
    broken_rate: float = 0.0
    premium_rate: float = 0.0
    max_board_height: int = 0


@dataclass
class ReviewResult:
    """盘后总结完整结果"""
    trade_date: str

    # 信号表现
    signal_performances: List[SignalPerformance] = field(default_factory=list)
    pattern_stats: Dict[str, PatternStats] = field(default_factory=dict)

    # 情绪周期趋势
    emotion_trends: List[EmotionTrend] = field(default_factory=list)
    emotion_trend_summary: str = ""

    # 参数敏感度
    sensitivity_analysis: Dict = field(default_factory=dict)

    # 综合建议
    review_summary: str = ""
    parameter_advice: str = ""


class ReviewAnalyzer:
    """
    盘后总结分析器 - Layer 5

    回顾信号表现，分析情绪趋势，提供参数调优建议
    """

    def __init__(self, data_manager):
        self.dm = data_manager

        # 信号表现阈值
        self.performance_thresholds = {
            'limit_up': 9.5,       # 涨停阈值
            'big_gain': 5.0,       # 大涨阈值
            'small_gain': 0.0,     # 小涨阈值
            'small_loss': -5.0,    # 小跌阈值
            'big_loss': -9.5,      # 大跌阈值
        }

        logger.info("[ReviewAnalyzer] 初始化完成")

    def analyze(self, trade_date: str,
                patterns: Dict[str, List],
                emotion_result: Dict,
                market_env_result=None,
                history_emotion_data: List[Dict] = None) -> ReviewResult:
        """
        执行盘后总结分析

        Args:
            trade_date: 交易日期
            patterns: 当日模式信号
            emotion_result: 情绪周期分析结果
            market_env_result: 大盘环境分析结果
            history_emotion_data: 历史情绪数据（近N天）

        Returns:
            ReviewResult: 盘后总结结果
        """
        logger.info("=" * 60)
        logger.info(f"[Layer5-盘后总结] 开始分析: {trade_date}")
        logger.info("=" * 60)

        result = ReviewResult(trade_date=trade_date)

        # 1. 信号表现统计
        self._analyze_signal_performance(trade_date, patterns, result)

        # 2. 情绪周期趋势
        self._analyze_emotion_trend(trade_date, emotion_result, history_emotion_data, result)

        # 3. 参数敏感度分析
        self._analyze_parameter_sensitivity(patterns, result)

        # 4. 生成综合建议
        self._generate_review_summary(result, emotion_result, market_env_result)

        logger.info(f"[Layer5-盘后总结] 分析完成: "
                   f"信号总数={sum(s.total_signals for s in result.pattern_stats.values())}, "
                   f"情绪趋势={result.emotion_trend_summary}")

        return result

    def _analyze_signal_performance(self, trade_date: str,
                                     patterns: Dict[str, List],
                                     result: ReviewResult):
        """分析信号表现"""
        logger.info("[Layer5] 分析信号表现...")

        all_signals = []
        for pattern_name, signals in patterns.items():
            for signal in signals:
                perf = SignalPerformance(
                    pattern_type=pattern_name,
                    stock_code=getattr(signal, 'stock_code', ''),
                    stock_name=getattr(signal, 'stock_name', ''),
                    signal_date=trade_date,
                    confidence=getattr(signal, 'confidence', 0),
                )
                all_signals.append(perf)

        # 尝试获取次日表现数据
        try:
            next_date = self._get_next_trade_date(trade_date)
            if next_date:
                for perf in all_signals:
                    try:
                        daily = self.dm.get_daily_data(
                            perf.stock_code,
                            next_date,
                            next_date
                        )
                        if daily is not None and not daily.empty:
                            row = daily.iloc[0]
                            perf.next_day_open_change = float(row.get('open', 0))
                            perf.next_day_close_change = float(row.get('pct_chg', 0))
                            perf.next_day_high_change = float(row.get('high', 0))

                            # 判断结果
                            close_chg = perf.next_day_close_change
                            if close_chg >= self.performance_thresholds['limit_up']:
                                perf.outcome = SignalOutcome.LIMIT_UP
                                perf.is_profitable = True
                            elif close_chg >= self.performance_thresholds['big_gain']:
                                perf.outcome = SignalOutcome.BIG_GAIN
                                perf.is_profitable = True
                            elif close_chg >= self.performance_thresholds['small_gain']:
                                perf.outcome = SignalOutcome.SMALL_GAIN
                                perf.is_profitable = True
                            elif close_chg >= self.performance_thresholds['small_loss']:
                                perf.outcome = SignalOutcome.SMALL_LOSS
                                perf.is_profitable = False
                            elif close_chg >= self.performance_thresholds['big_loss']:
                                perf.outcome = SignalOutcome.BIG_LOSS
                                perf.is_profitable = False
                            else:
                                perf.outcome = SignalOutcome.LIMIT_DOWN
                                perf.is_profitable = False
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[Layer5] 获取次日表现数据失败: {e}")

        result.signal_performances = all_signals

        # 按模式统计
        pattern_groups = {}
        for perf in all_signals:
            if perf.pattern_type not in pattern_groups:
                pattern_groups[perf.pattern_type] = []
            pattern_groups[perf.pattern_type].append(perf)

        for pattern_name, perfs in pattern_groups.items():
            stats = PatternStats(pattern_name=pattern_name)
            stats.total_signals = len(perfs)
            stats.profitable_signals = sum(1 for p in perfs if p.is_profitable)
            stats.win_rate = stats.profitable_signals / stats.total_signals if stats.total_signals > 0 else 0

            returns = [p.next_day_close_change for p in perfs if p.next_day_close_change != 0]
            if returns:
                stats.avg_return = np.mean(returns)
                stats.max_return = max(returns)
                stats.min_return = min(returns)

            confidences = [p.confidence for p in perfs if p.confidence > 0]
            if confidences:
                stats.avg_confidence = np.mean(confidences)

            result.pattern_stats[pattern_name] = stats

            logger.info(f"[Layer5] {pattern_name}: 信号{stats.total_signals}个, "
                       f"盈利{stats.profitable_signals}个, 胜率{stats.win_rate:.1%}, "
                       f"平均收益{stats.avg_return:.2f}%")

    def _analyze_emotion_trend(self, trade_date: str,
                                emotion_result: Dict,
                                history_data: List[Dict],
                                result: ReviewResult):
        """分析情绪周期变化趋势"""
        logger.info("[Layer5] 分析情绪周期趋势...")

        # 当前情绪数据
        metrics = emotion_result.get('metrics', {})
        current = EmotionTrend(
            date=trade_date,
            cycle_name=emotion_result.get('cycle_name', '未知'),
            limit_up_count=metrics.get('limit_up_count', 0),
            broken_rate=metrics.get('broken_rate', 0),
            premium_rate=metrics.get('prev_limit_up_premium', 0),
            max_board_height=metrics.get('max_board_height', 0),
        )
        result.emotion_trends.append(current)

        # 历史情绪数据
        if history_data:
            for hist in history_data:
                hist_metrics = hist.get('metrics', {})
                trend = EmotionTrend(
                    date=hist.get('date', ''),
                    cycle_name=hist.get('cycle_name', '未知'),
                    limit_up_count=hist_metrics.get('limit_up_count', 0),
                    broken_rate=hist_metrics.get('broken_rate', 0),
                    premium_rate=hist_metrics.get('prev_limit_up_premium', 0),
                    max_board_height=hist_metrics.get('max_board_height', 0),
                )
                result.emotion_trends.append(trend)

        # 按日期排序
        result.emotion_trends.sort(key=lambda x: x.date)

        # 生成趋势摘要
        if len(result.emotion_trends) >= 2:
            prev = result.emotion_trends[-2]
            curr = result.emotion_trends[-1]

            changes = []
            if curr.limit_up_count > prev.limit_up_count:
                changes.append(f"涨停家数增加({prev.limit_up_count}→{curr.limit_up_count})")
            elif curr.limit_up_count < prev.limit_up_count:
                changes.append(f"涨停家数减少({prev.limit_up_count}→{curr.limit_up_count})")

            if curr.broken_rate > prev.broken_rate:
                changes.append(f"炸板率上升({prev.broken_rate:.1f}%→{curr.broken_rate:.1f}%)")
            elif curr.broken_rate < prev.broken_rate:
                changes.append(f"炸板率下降({prev.broken_rate:.1f}%→{curr.broken_rate:.1f}%)")

            if curr.premium_rate > prev.premium_rate:
                changes.append(f"溢价率上升({prev.premium_rate:.1f}%→{curr.premium_rate:.1f}%)")
            elif curr.premium_rate < prev.premium_rate:
                changes.append(f"溢价率下降({prev.premium_rate:.1f}%→{curr.premium_rate:.1f}%)")

            if curr.cycle_name != prev.cycle_name:
                changes.append(f"情绪周期切换: {prev.cycle_name}→{curr.cycle_name}")

            if changes:
                result.emotion_trend_summary = "；".join(changes)
            else:
                result.emotion_trend_summary = f"情绪维持{curr.cycle_name}，各项指标稳定"
        else:
            result.emotion_trend_summary = f"当前情绪: {current.cycle_name}"

        logger.info(f"[Layer5] 情绪趋势: {result.emotion_trend_summary}")

    def _analyze_parameter_sensitivity(self, patterns: Dict[str, List],
                                        result: ReviewResult):
        """参数敏感度分析"""
        logger.info("[Layer5] 参数敏感度分析...")

        total_signals = sum(len(v) for v in patterns.values())

        sensitivity = {
            'total_signals': total_signals,
            'suggestions': [],
        }

        # 基于信号数量的建议
        if total_signals == 0:
            sensitivity['suggestions'].append({
                'direction': '放宽',
                'reason': '当日无任何信号，可能阈值过严',
                'action': '考虑适当降低各模式的置信度阈值',
            })
        elif total_signals > 20:
            sensitivity['suggestions'].append({
                'direction': '收紧',
                'reason': f'当日信号过多({total_signals}个)，可能阈值过宽',
                'action': '考虑适当提高各模式的置信度阈值',
            })
        else:
            sensitivity['suggestions'].append({
                'direction': '维持',
                'reason': f'当日信号数量({total_signals}个)在合理范围内',
                'action': '当前参数设置合理，无需调整',
            })

        # 按模式分析
        for pattern_name, signals in patterns.items():
            if len(signals) >= 5:
                sensitivity['suggestions'].append({
                    'direction': '收紧',
                    'pattern': pattern_name,
                    'reason': f'{pattern_name}信号过多({len(signals)}个)',
                    'action': f'考虑提高{pattern_name}的置信度阈值',
                })

        result.sensitivity_analysis = sensitivity

    def _generate_review_summary(self, result: ReviewResult,
                                   emotion_result: Dict,
                                   market_env_result=None):
        """生成综合回顾摘要"""
        parts = []

        # 信号总结
        total = sum(s.total_signals for s in result.pattern_stats.values())
        profitable = sum(s.profitable_signals for s in result.pattern_stats.values())
        parts.append(f"今日共发出{total}个交易信号")

        if profitable > 0:
            parts.append(f"其中{profitable}个盈利")

        # 最佳模式
        if result.pattern_stats:
            best_pattern = max(result.pattern_stats.values(),
                              key=lambda x: x.win_rate if x.total_signals > 0 else 0)
            if best_pattern.total_signals > 0:
                parts.append(f"表现最佳模式: {best_pattern.pattern_name}(胜率{best_pattern.win_rate:.1%})")

        # 情绪趋势
        if result.emotion_trend_summary:
            parts.append(f"情绪趋势: {result.emotion_trend_summary}")

        # 参数建议
        for suggestion in result.sensitivity_analysis.get('suggestions', []):
            if suggestion['direction'] != '维持':
                parts.append(f"参数建议: {suggestion['action']}")

        result.review_summary = "；".join(parts)

        # 参数调优建议
        advice_parts = []
        for suggestion in result.sensitivity_analysis.get('suggestions', []):
            advice_parts.append(f"[{suggestion['direction']}] {suggestion['reason']} → {suggestion['action']}")
        result.parameter_advice = "\n".join(advice_parts) if advice_parts else "当前参数设置合理"

    def _get_next_trade_date(self, date_str: str) -> Optional[str]:
        """获取下一个交易日"""
        try:
            from core.utils.date_utils import DateUtils
            du = DateUtils()
            return du.get_next_trade_date(date_str)
        except Exception:
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                next_dt = dt + timedelta(days=1)
                # 跳过周末
                while next_dt.weekday() >= 5:
                    next_dt += timedelta(days=1)
                return next_dt.strftime("%Y%m%d")
            except Exception:
                return None

    def to_dict(self, result: ReviewResult) -> Dict:
        """将分析结果转换为字典"""
        return {
            'trade_date': result.trade_date,
            'pattern_stats': {
                name: {
                    'total_signals': stats.total_signals,
                    'profitable_signals': stats.profitable_signals,
                    'win_rate': stats.win_rate,
                    'avg_return': stats.avg_return,
                    'max_return': stats.max_return,
                    'min_return': stats.min_return,
                    'avg_confidence': stats.avg_confidence,
                }
                for name, stats in result.pattern_stats.items()
            },
            'emotion_trends': [
                {
                    'date': t.date,
                    'cycle_name': t.cycle_name,
                    'limit_up_count': t.limit_up_count,
                    'broken_rate': t.broken_rate,
                    'premium_rate': t.premium_rate,
                    'max_board_height': t.max_board_height,
                }
                for t in result.emotion_trends
            ],
            'emotion_trend_summary': result.emotion_trend_summary,
            'sensitivity_analysis': result.sensitivity_analysis,
            'review_summary': result.review_summary,
            'parameter_advice': result.parameter_advice,
        }