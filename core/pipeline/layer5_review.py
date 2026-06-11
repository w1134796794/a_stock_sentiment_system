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
    """单个信号的表现。

    Sprint D-1：在原 T+1 字段基础上扩展 ``multi_window_returns``，
    记录该信号在多个时间窗口（T+1/T+2/T+3/T+5）的累计涨跌幅。
    用于"持有 N 日"的统计——同一个信号在不同持有期下胜率往往差异巨大。
    """
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

    # Sprint D-1：多窗口累计涨跌幅
    # key 例如 "T+1"/"T+2"/"T+3"/"T+5"，value = 从信号日**收盘**买入到 T+N 日**收盘**的累计涨跌幅(%)
    multi_window_returns: Dict[str, float] = field(default_factory=dict)


@dataclass
class PatternStats:
    """模式统计。

    Sprint D-1：``multi_window_stats`` 给每个时间窗口存一组（胜率/平均收益）。
    """
    pattern_name: str
    total_signals: int = 0
    profitable_signals: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0              # 平均收益率
    max_return: float = 0.0              # 最大收益率
    min_return: float = 0.0              # 最小收益率
    avg_confidence: float = 0.0          # 平均置信度

    # Sprint D-1：多窗口统计 ``{"T+1": {"win_rate": .., "avg_return": .., "n": 18}, ...}``
    multi_window_stats: Dict[str, Dict[str, float]] = field(default_factory=dict)


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

    # 复盘统计来源："today" (今日 T+1) / "history" (回溯过去 N 天) / "pending" (T+1 尚未到来且无历史)
    stats_source: str = "today"
    # 历史复盘窗口（仅当 stats_source == 'history' 时有意义）
    stats_window: Tuple[str, str] = ("", "")
    # 待确认信号数（统计来源为今日时，无 T+1 数据的信号个数）
    pending_signal_count: int = 0


class ReviewAnalyzer:
    """
    盘后总结分析器 - Layer 5

    回顾信号表现，分析情绪趋势，提供参数调优建议
    """

    def __init__(self, data_manager):
        self.dm = data_manager
        # Phase 1：只读仓库（流水线在 run 前注入；缺省 None → analyze 内懒构造透传）
        self.repo = None

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

        if self.repo is None:
            from core.data.repository import StockRepository
            self.repo = StockRepository.passthrough(self.dm)

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
        """
        分析信号表现。

        统计策略（按优先级，自动降级）：
        - **今日 T+1 路径**：若 trade_date 是历史日期且 T+1 行情已存在，
          就用今日的信号 + T+1 涨跌幅算胜率（最准确）。
        - **历史 N 天回溯**：若 T+1 尚未发生（trade_date = 最新交易日，
          所谓"今日复盘"），fallback 到读 `output/factor_results/` 下过去
          N 天的 JSON，把那些 T+1 已经发生过的历史信号纳入统计。
        - **pending 状态**：若以上两条路径都没数据，把信号标记为待确认，
          报告里显示"等待 T+1 数据"而不是误导性的 0。
        """
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

        # ---- 路径 1：今日 T+1 ----
        next_date = self._get_next_trade_date(trade_date)
        next_day_map: Dict[str, pd.Series] = {}
        if next_date and next_date != trade_date:
            try:
                all_daily = self.repo.get_all_stocks_daily(next_date)
                if all_daily is not None and not all_daily.empty and 'ts_code' in all_daily.columns:
                    next_day_map = {row['ts_code']: row for _, row in all_daily.iterrows()}
            except Exception as e:
                logger.debug(f"[Layer5] 全市场次日数据拉取失败: {e}")

        if next_day_map:
            self._mark_performance(all_signals, next_day_map)
            # Sprint D-1：在 T+1 之上额外抓 T+2/T+3/T+5，多窗口胜率
            self._mark_multi_window_performance(
                all_signals, trade_date,
                windows=("T+1", "T+2", "T+3", "T+5"),
            )
            result.signal_performances = all_signals
            self._aggregate_pattern_stats(all_signals, result)
            result.stats_source = "today"
            result.pending_signal_count = sum(
                1 for p in all_signals if p.outcome == SignalOutcome.UNKNOWN
            )
            logger.info(f"[Layer5] 信号统计源=今日 T+1，{result.pending_signal_count} 个信号无 T+1 数据")
            return

        # ---- 路径 2：历史 N 天回溯 ----
        result.signal_performances = all_signals
        history_stats, window = self._compute_history_pattern_stats(trade_date, lookback_days=10)
        if history_stats:
            result.pattern_stats = history_stats
            result.stats_source = "history"
            result.stats_window = window
            result.pending_signal_count = len(all_signals)
            logger.info(
                f"[Layer5] 信号统计源=历史 {window[0]}~{window[1]} "
                f"（{sum(s.total_signals for s in history_stats.values())} 个历史信号）"
            )
            return

        # ---- 路径 3：完全没有数据，pending ----
        result.stats_source = "pending"
        result.pending_signal_count = len(all_signals)
        # 仍写空 stats 让下游知道哪些 pattern 有信号
        for pattern_name in patterns:
            stats = PatternStats(pattern_name=pattern_name)
            stats.total_signals = len(patterns[pattern_name])
            confidences = [getattr(s, 'confidence', 0) for s in patterns[pattern_name]]
            confidences = [c for c in confidences if c > 0]
            if confidences:
                stats.avg_confidence = float(np.mean(confidences))
            result.pattern_stats[pattern_name] = stats
        logger.info(f"[Layer5] 信号统计源=pending：{result.pending_signal_count} 个信号等待 T+1 确认")

    # ------------------------------------------------------------------
    # 信号表现/统计辅助
    # ------------------------------------------------------------------

    def _mark_performance(self, perfs: List[SignalPerformance],
                          next_day_map: Dict[str, pd.Series]) -> None:
        """把 next_day_map 中的 T+1 行情写到 perf 上并判定 outcome。"""
        for perf in perfs:
            try:
                row = next_day_map.get(perf.stock_code)
                if row is None:
                    continue

                perf.next_day_open_change = float(row.get('open', 0))
                perf.next_day_close_change = float(row.get('pct_chg', 0))
                perf.next_day_high_change = float(row.get('high', 0))

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

    def _aggregate_pattern_stats(self, perfs: List[SignalPerformance],
                                  result: ReviewResult) -> None:
        """把 perfs 按 pattern_type 聚合到 result.pattern_stats。

        Sprint D-1：除了 T+1 主统计外，按 ``multi_window_returns`` 顺带聚合
        T+2/T+3/T+5 的胜率/平均收益，挂到 ``stats.multi_window_stats``。
        """
        pattern_groups: Dict[str, List[SignalPerformance]] = {}
        for perf in perfs:
            pattern_groups.setdefault(perf.pattern_type, []).append(perf)

        for pattern_name, ps in pattern_groups.items():
            stats = PatternStats(pattern_name=pattern_name)
            stats.total_signals = len(ps)
            evaluated = [p for p in ps if p.outcome != SignalOutcome.UNKNOWN]
            if evaluated:
                stats.profitable_signals = sum(1 for p in evaluated if p.is_profitable)
                stats.win_rate = stats.profitable_signals / len(evaluated)
                returns = [p.next_day_close_change for p in evaluated]
                stats.avg_return = float(np.mean(returns))
                stats.max_return = float(max(returns))
                stats.min_return = float(min(returns))

            confidences = [p.confidence for p in ps if p.confidence > 0]
            if confidences:
                stats.avg_confidence = float(np.mean(confidences))

            # Sprint D-1：多窗口聚合
            stats.multi_window_stats = self._compute_multi_window_stats(ps)

            result.pattern_stats[pattern_name] = stats
            mw_msg = ""
            if stats.multi_window_stats:
                mw_msg = " | " + ", ".join(
                    f"{w}={s['win_rate']:.0%}/{s['avg_return']:+.2f}%"
                    for w, s in stats.multi_window_stats.items()
                )
            logger.info(
                f"[Layer5] {pattern_name}: 信号{stats.total_signals}个, "
                f"已验证{len(evaluated)}个, 盈利{stats.profitable_signals}个, "
                f"胜率{stats.win_rate:.1%}, 平均收益{stats.avg_return:.2f}%{mw_msg}"
            )

    # ------------------------------------------------------------------
    # Sprint D-1：多窗口表现采集 + 聚合
    # ------------------------------------------------------------------

    def _mark_multi_window_performance(
        self,
        perfs: List[SignalPerformance],
        signal_date: str,
        windows: tuple = ("T+1", "T+2", "T+3", "T+5"),
    ) -> None:
        """为每个 perf 抓 T+1/T+2/T+3/T+5 的收盘价，算累计涨跌幅。

        实现：
          1. 解析每个窗口 N → 第 N 个交易日 ``target_date``
          2. 一次性拉每个 target_date 的全市场日线（``dm.get_all_stocks_daily``）
          3. 用信号日**收盘价**作为基准 close_0，再算 ``(close_N / close_0 - 1) * 100``
          4. 没数据的窗口跳过，不写 0（避免污染统计）

        基准 close_0 取信号日的收盘价；若信号日数据也拿不到，跳过这只票的多窗口。
        """
        # 1) 取信号日全市场日线（用于 close_0）
        try:
            base_daily = self.repo.get_all_stocks_daily(signal_date)
        except Exception as e:
            logger.debug(f"[Layer5-MW] 取信号日日线失败 {signal_date}: {e}")
            return
        if base_daily is None or base_daily.empty:
            return
        base_close_map: Dict[str, float] = {}
        if 'ts_code' in base_daily.columns and 'close' in base_daily.columns:
            for _, r in base_daily.iterrows():
                base_close_map[r['ts_code']] = float(r.get('close', 0) or 0)
        if not base_close_map:
            return

        # 2) 为每个窗口拉 target_date 的日线
        window_close_maps: Dict[str, Dict[str, float]] = {}
        for w in windows:
            try:
                n = int(w.split("+")[1])
            except (IndexError, ValueError):
                continue
            target_date = self._get_n_th_trade_date(signal_date, n)
            if not target_date:
                continue
            try:
                df = self.repo.get_all_stocks_daily(target_date)
            except Exception:
                continue
            if df is None or df.empty or 'ts_code' not in df.columns:
                continue
            window_close_maps[w] = {
                row['ts_code']: float(row.get('close', 0) or 0)
                for _, row in df.iterrows()
            }

        if not window_close_maps:
            return

        # 3) 给每个 perf 填多窗口收益
        for perf in perfs:
            close_0 = base_close_map.get(perf.stock_code, 0)
            if close_0 <= 0:
                continue
            for w, close_map in window_close_maps.items():
                c = close_map.get(perf.stock_code, 0)
                if c <= 0:
                    continue
                ret = (c / close_0 - 1) * 100
                perf.multi_window_returns[w] = float(ret)

    def _compute_multi_window_stats(
        self,
        perfs: List[SignalPerformance],
    ) -> Dict[str, Dict[str, float]]:
        """对一组同 pattern 的 perfs 聚合各窗口胜率/平均收益。

        Returns:
            ``{"T+1": {"win_rate": 0.6, "avg_return": 1.2, "n": 10}, ...}``
            n = 该窗口有数据的样本数
        """
        # 收集每个窗口的全部 returns
        bucket: Dict[str, List[float]] = {}
        for p in perfs:
            for w, ret in (p.multi_window_returns or {}).items():
                bucket.setdefault(w, []).append(float(ret))

        out: Dict[str, Dict[str, float]] = {}
        for w, rets in bucket.items():
            if not rets:
                continue
            n = len(rets)
            wins = sum(1 for r in rets if r > 0)
            out[w] = {
                "win_rate": wins / n,
                "avg_return": float(np.mean(rets)),
                "max_return": float(max(rets)),
                "min_return": float(min(rets)),
                "n": n,
            }
        return out

    def _get_n_th_trade_date(self, start: str, n: int) -> Optional[str]:
        """从 ``start`` 起向后第 N 个交易日（不含 start）。"""
        if n <= 0:
            return None
        try:
            from core.utils.date_utils import DateUtils
            du = DateUtils()
            cur = start
            for _ in range(n):
                cur = du.get_next_trade_date(cur)
                if not cur:
                    return None
            return cur
        except Exception:
            return None

    def _compute_history_pattern_stats(
        self, end_date: str, lookback_days: int = 10
    ) -> Tuple[Dict[str, PatternStats], Tuple[str, str]]:
        """
        当 T+1 数据不可用时，回溯过去 N 个交易日的 factor_results JSON，
        把那些已经有 T+1 数据可验证的历史信号纳入统计。

        Returns:
            (pattern_stats_dict, (start_date, end_date)) —— 找不到任何历史
            信号时返回 ({}, ("","")).
        """
        from pathlib import Path
        import json

        try:
            from core.utils.date_utils import DateUtils
            du = DateUtils()
            recent = du.get_last_n_trade_dates(lookback_days, end_date=end_date)
            # 排除 end_date 本身（它的 T+1 就是我们走到这里的原因）
            recent = [d for d in recent if d != end_date]
        except Exception:
            recent = []

        if not recent:
            return {}, ("", "")

        # factor_results JSON 目录
        json_dir = Path(__file__).parent.parent.parent / "output" / "factor_results"
        if not json_dir.exists():
            return {}, ("", "")

        all_perfs: List[SignalPerformance] = []
        used_dates: List[str] = []

        for signal_date in sorted(recent):
            fpath = json_dir / f"factor_results_{signal_date}.json"
            if not fpath.exists():
                continue

            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
            except Exception:
                continue

            layer3 = raw.get('layer3_stock_selection', {}) or {}
            mode_signals = layer3.get('模式信号', {}) or {}
            if not mode_signals:
                continue

            # 拿该信号日的 T+1 数据
            try:
                tplus1 = du.get_next_trade_date(signal_date)
            except Exception:
                tplus1 = None
            if not tplus1 or tplus1 == signal_date:
                continue

            try:
                all_daily = self.repo.get_all_stocks_daily(tplus1)
            except Exception:
                continue
            if all_daily is None or all_daily.empty or 'ts_code' not in all_daily.columns:
                continue

            next_day_map = {row['ts_code']: row for _, row in all_daily.iterrows()}
            if not next_day_map:
                continue

            for pattern_name, signals in mode_signals.items():
                for sig in signals or []:
                    perf = SignalPerformance(
                        pattern_type=pattern_name,
                        stock_code=str(sig.get('股票代码', '')),
                        stock_name=str(sig.get('股票名称', '')),
                        signal_date=signal_date,
                        confidence=float(sig.get('置信度', 0) or 0),
                    )
                    all_perfs.append(perf)

            self._mark_performance(all_perfs, next_day_map)
            used_dates.append(signal_date)

        if not all_perfs:
            return {}, ("", "")

        # 用通用聚合
        tmp_result = ReviewResult(trade_date=end_date)
        self._aggregate_pattern_stats(all_perfs, tmp_result)
        window = (used_dates[0], used_dates[-1]) if used_dates else ("", "")
        return tmp_result.pattern_stats, window

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
            # 复盘统计来源元数据，供报告层渲染"今日 T+1 / 历史 N 天 / pending"标签
            'stats_source': result.stats_source,
            'stats_window': list(result.stats_window),
            'pending_signal_count': result.pending_signal_count,
        }