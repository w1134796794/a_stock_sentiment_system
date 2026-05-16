"""
Layer 3: 个股筛选层 - 看个股（定标的）

核心职责：
  1. 情绪周期分析（规则引擎 + ML综合判断）
  2. 模式识别（首板突破、弱转强、龙头首阴等）
  3. 个股板块地位量化（空间龙头/强度龙头/中军/跟风/补涨）
  4. 信号优先级与互斥规则
  5. 多因子综合评分
  6. 资金流向与筹码结构分析

输入：涨停池、热点板块、大盘环境
输出：排序后的交易信号、综合评分、龙头池
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import loguru

logger = loguru.logger


@dataclass
class StockSelectionResult:
    """个股筛选结果"""
    trade_date: str = ""

    emotion_result: Dict = field(default_factory=dict)
    emotion_cycle: str = "震荡期"

    patterns: Dict[str, List] = field(default_factory=dict)
    ranked_signals: List = field(default_factory=list)
    composite_scores: List = field(default_factory=list)

    sector_positions: Dict = field(default_factory=dict)
    sector_leaders: Dict[str, List] = field(default_factory=dict)

    moneyflow_analysis: Dict = field(default_factory=dict)
    chip_analysis: Dict = field(default_factory=dict)

    dragon_pool_data: List[Dict] = field(default_factory=list)
    weakening_pool_data: List[Dict] = field(default_factory=list)

    selection_summary: str = ""


class StockSelectionLayer:
    """
    Layer 3: 个股筛选层

    整合情绪分析、模式识别、地位量化、信号排序、多因子评分
    """

    def __init__(self, data_manager, industry_mapper=None):
        self.dm = data_manager
        self.mapper = industry_mapper

        self._emotion_engine = None
        self._integrated_emotion_engine = None
        self._pattern_recognition = None
        self._signal_priority = None
        self._sector_position_analyzer = None
        self._multi_factor_scorer = None
        self._moneyflow_analyzer = None
        self._chip_analyzer = None

    def analyze(self, trade_date: str, prev_trade_date: str, day_before_prev: str,
                zt_pool: pd.DataFrame, limit_down_df: pd.DataFrame,
                hierarchy_df: pd.DataFrame, market_env=None,
                hot_sectors: List = None) -> StockSelectionResult:
        """
        执行个股筛选

        Args:
            trade_date: 交易日期
            prev_trade_date: 前一交易日
            day_before_prev: 前天
            zt_pool: 涨停池
            limit_down_df: 跌停池
            hierarchy_df: 行业层级数据
            market_env: 大盘环境分析结果
            hot_sectors: 热点板块列表

        Returns:
            StockSelectionResult: 个股筛选结果
        """
        result = StockSelectionResult(trade_date=trade_date)

        try:
            self._analyze_emotion_cycle(result, zt_pool, limit_down_df, day_before_prev, market_env)

            self._recognize_patterns(result, trade_date, prev_trade_date, hot_sectors)

            self._analyze_sector_positions(result, zt_pool, hierarchy_df)

            self._apply_signal_priority(result)

            self._apply_multi_factor_scoring(result, market_env)

            self._analyze_moneyflow_and_chip(result, zt_pool, trade_date)

            result.selection_summary = self._generate_summary(result)

            logger.info(f"[Layer3] 个股筛选完成: 情绪={result.emotion_cycle}, "
                       f"信号={len(result.ranked_signals)}个, 龙头板块={len(result.sector_leaders)}个")

        except Exception as e:
            logger.error(f"[Layer3] 个股筛选失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

        return result

    def _analyze_emotion_cycle(self, result: StockSelectionResult,
                                zt_pool: pd.DataFrame, limit_down_df: pd.DataFrame,
                                day_before_prev: str, market_env):
        """情绪周期分析"""
        from core.analysis.emotion_cycle_engine import EmotionCycleEngine

        if self._emotion_engine is None:
            self._emotion_engine = EmotionCycleEngine(dm=self.dm)

        prev_limit_up_df = pd.DataFrame()
        if day_before_prev:
            try:
                prev_limit_up_df = self.dm.get_limit_up_pool(day_before_prev)
            except Exception:
                pass

        result.emotion_result = self._emotion_engine.analyze_market_data(
            limit_up_df=zt_pool,
            limit_down_df=limit_down_df,
            prev_limit_up_df=prev_limit_up_df,
        )
        result.emotion_cycle = result.emotion_result.get('cycle_name', '震荡期')

        try:
            if self._integrated_emotion_engine is None:
                from core.analysis.emotion_cycle_integrated import create_integrated_engine
                self._integrated_emotion_engine = create_integrated_engine(self._emotion_engine)

            metrics = result.emotion_result.get('metrics', {})
            ml_indicators = {
                'limit_up_count': metrics.get('limit_up_count', 0),
                'max_board_height': metrics.get('max_board_height', 0),
                'broken_rate': metrics.get('broken_rate', 0),
                'continuous_rate': metrics.get('continuous_rate', 0),
            }

            integrated_result = self._integrated_emotion_engine.detect_cycle_integrated(
                market_data={'limit_up_df': zt_pool, 'limit_down_df': limit_down_df},
                indicators=ml_indicators,
                use_ml=True,
            )

            result.emotion_result['integrated_analysis'] = {
                'rule_state': integrated_result.rule_based_state,
                'ml_state': integrated_result.ml_predicted_state,
                'final_state': integrated_result.final_state,
                'confidence': integrated_result.final_confidence,
                'agreement': integrated_result.agreement,
                'analysis': integrated_result.analysis,
                'risk_level': integrated_result.risk_level,
            }

            logger.info(f"[Layer3] 情绪周期综合判断: 规则={integrated_result.rule_based_state}, "
                       f"ML={integrated_result.ml_predicted_state}, 最终={integrated_result.final_state}")
        except Exception as e:
            logger.warning(f"[Layer3] ML情绪分析失败: {e}")

    def _recognize_patterns(self, result: StockSelectionResult,
                             trade_date: str, prev_trade_date: str,
                             hot_sectors: List = None):
        """模式识别"""
        from core.analysis.pattern_recognition import PatternRecognition

        if self._pattern_recognition is None:
            self._pattern_recognition = PatternRecognition(
                self.dm, sector_engine=None, mapper=self.mapper
            )

        result.patterns = self._pattern_recognition.scan_all_patterns(
            trade_date, prev_trade_date, hot_sectors=hot_sectors or []
        )

        total_signals = sum(len(v) for v in result.patterns.values())
        logger.info(f"[Layer3] 模式识别完成: {total_signals}个信号")
        for ptype, signals in result.patterns.items():
            if signals:
                logger.info(f"[Layer3]   - {ptype}: {len(signals)}个")

        if hasattr(self._pattern_recognition, 'weak_to_strong') and self._pattern_recognition.weak_to_strong:
            try:
                pool_summary = self._pattern_recognition.weak_to_strong.get_pools_summary()
                result.dragon_pool_data = pool_summary.get('dragon_pool', [])
                result.weakening_pool_data = pool_summary.get('weakening_pool', [])
            except Exception as e:
                logger.warning(f"[Layer3] 获取龙头池数据失败: {e}")

    def _analyze_sector_positions(self, result: StockSelectionResult,
                                   zt_pool: pd.DataFrame, hierarchy_df: pd.DataFrame):
        """个股板块地位量化"""
        if zt_pool.empty:
            return

        try:
            if self._sector_position_analyzer is None:
                from core.stock_ranking.sector_position import SectorPositionAnalyzer
                self._sector_position_analyzer = SectorPositionAnalyzer()

            result.sector_positions = self._sector_position_analyzer.analyze(
                zt_pool, hierarchy_df
            )
            logger.info(f"[Layer3] 板块地位分析完成: {len(result.sector_positions)}只")

            result.sector_leaders = self._sector_position_analyzer.get_sector_leaders(
                result.sector_positions
            )
            for sector, leader_list in list(result.sector_leaders.items())[:5]:
                names = [l.stock_name for l in leader_list]
                logger.info(f"[Layer3]   板块[{sector}]龙头: {', '.join(names)}")
        except Exception as e:
            logger.warning(f"[Layer3] 板块地位分析失败: {e}")

    def _apply_signal_priority(self, result: StockSelectionResult):
        """应用信号优先级和互斥规则"""
        if self._signal_priority is None:
            from core.pattern.signal_priority import SignalPriorityManager, PriorityConfig
            self._signal_priority = SignalPriorityManager(PriorityConfig())

        result.ranked_signals = self._signal_priority.process_signals(result.patterns)

        priority_report = self._signal_priority.generate_priority_report(result.ranked_signals)
        logger.info(f"[Layer3] 信号优先级处理完成:\n{priority_report}")

    def _apply_multi_factor_scoring(self, result: StockSelectionResult, market_env):
        """多因子综合评分"""
        if self._multi_factor_scorer is None:
            from core.stock_ranking.multi_factor_scorer import MultiFactorScorer
            self._multi_factor_scorer = MultiFactorScorer()

        emotion_cycle = result.emotion_cycle

        sector_heat_map = {}
        if result.sector_positions:
            for sector_name, positions in result.sector_positions.items():
                if hasattr(positions, '__len__'):
                    sector_heat_map[sector_name] = min(100, len(positions) * 10)

        result.composite_scores = self._multi_factor_scorer.score_signals(
            result.ranked_signals,
            sector_position_results=result.sector_positions,
            emotion_cycle=emotion_cycle,
            sector_heat_map=sector_heat_map,
        )

    def _analyze_moneyflow_and_chip(self, result: StockSelectionResult,
                                     zt_pool: pd.DataFrame, trade_date: str):
        """资金流向和筹码结构分析"""
        try:
            from core.analysis.moneyflow_analyzer import create_moneyflow_analyzer
            from core.analysis.chip_structure_analyzer import create_chip_analyzer

            if self._moneyflow_analyzer is None:
                self._moneyflow_analyzer = create_moneyflow_analyzer(self.dm)
            if self._chip_analyzer is None:
                self._chip_analyzer = create_chip_analyzer(self.dm)

            if zt_pool.empty:
                return

            top_stocks = zt_pool.head(10)
            codes = top_stocks['代码'].tolist() if '代码' in top_stocks.columns else []

            for stock_code in codes:
                try:
                    mf_result = self._moneyflow_analyzer.analyze_stock_moneyflow(
                        stock_code, trade_date
                    )
                    if mf_result.net_mf_amount != 0:
                        result.moneyflow_analysis[stock_code] = {
                            'name': mf_result.name,
                            'main_net': mf_result.main_net_amount,
                            'retail_net': mf_result.retail_net_amount,
                            'direction': '流入' if mf_result.main_net_amount > 0 else '流出',
                        }

                    chip_result = self._chip_analyzer.analyze_chip_structure(
                        stock_code, trade_date
                    )
                    if chip_result.profit_pct > 0:
                        result.chip_analysis[stock_code] = {
                            'name': chip_result.name,
                            'profit_pct': chip_result.profit_pct,
                            'concentration': chip_result.concentration,
                            'avg_cost': chip_result.avg_cost,
                        }
                except Exception as e:
                    logger.debug(f"[Layer3] 资金/筹码分析失败 {stock_code}: {e}")

        except Exception as e:
            logger.warning(f"[Layer3] 资金流向/筹码分析失败: {e}")

    def _generate_summary(self, result: StockSelectionResult) -> str:
        """生成个股筛选摘要"""
        lines = []
        lines.append(f"=== 个股筛选摘要 ({result.trade_date}) ===")

        lines.append(f"\n📈 情绪周期: {result.emotion_cycle}")
        if 'integrated_analysis' in result.emotion_result:
            ia = result.emotion_result['integrated_analysis']
            lines.append(f"   综合判断: {ia.get('final_state', 'N/A')} (置信度: {ia.get('confidence', 0):.0%})")

        total_signals = sum(len(v) for v in result.patterns.values())
        lines.append(f"\n🎯 模式信号: {total_signals}个")
        for ptype, signals in result.patterns.items():
            if signals:
                lines.append(f"   - {ptype}: {len(signals)}个")

        lines.append(f"\n⭐ 排序后信号: {len(result.ranked_signals)}个")
        for i, sig in enumerate(result.ranked_signals[:5], 1):
            lines.append(f"   {i}. {sig.stock_name}({sig.stock_code}) - {sig.pattern_type} - 优先级:{sig.priority}")

        if result.sector_leaders:
            lines.append(f"\n👑 板块龙头:")
            for sector, leaders in list(result.sector_leaders.items())[:5]:
                names = [l.stock_name for l in leaders]
                lines.append(f"   [{sector}]: {', '.join(names)}")

        return "\n".join(lines)