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

    # 新增个股技术因子 (D1-D5)
    stock_tech_factors: Dict[str, Dict] = field(default_factory=dict)

    # 新增资金流向因子 (E1-E4)
    moneyflow_factors: Dict[str, Dict] = field(default_factory=dict)


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

        # P2-5: 入口轻量校验，发现脏数据时记日志（非 strict，不阻断 pipeline）
        try:
            from core.utils.schema_validator import assert_schema, LIMIT_UP_POOL
            assert_schema(zt_pool, LIMIT_UP_POOL, strict=False)
        except Exception as e:
            logger.debug(f"[Layer3] zt_pool 契约校验异常: {e}")

        try:
            self._analyze_emotion_cycle(result, zt_pool, limit_down_df, day_before_prev, market_env)

            self._recognize_patterns(result, trade_date, prev_trade_date, hot_sectors)

            self._analyze_sector_positions(result, zt_pool, hierarchy_df)

            self._apply_signal_priority(result)

            self._apply_multi_factor_scoring(result, market_env)

            self._analyze_moneyflow_and_chip(result, zt_pool, trade_date)

            # D1-D5: 个股技术因子
            self._compute_stock_tech_factors(result, zt_pool, trade_date)

            # E1-E4: 资金流向因子
            self._compute_moneyflow_factors(result, zt_pool, trade_date)

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
        from core.pattern.pattern_recognition import PatternRecognition

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

    def _compute_stock_tech_factors(self, result: StockSelectionResult,
                                     zt_pool: pd.DataFrame, trade_date: str):
        """
        D1-D5: 个股技术因子计算

        D1: N日高低位 - 当前价在N日最高最低之间的位置
        D2: 量价配合度 - 涨幅与量比的匹配程度
        D3: 封板强度 - 封板时间+封单量综合评分
        D4: 换手率健康度 - 换手率是否在合理区间
        D5: 均线多头排列度 - MA5>MA10>MA20>MA60的程度
        """
        try:
            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in zt_pool.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None or zt_pool.empty:
                return

            codes = zt_pool[ts_code_col].astype(str).tolist()[:30]

            # P2-1: 单次批量拉取所有候选股票的历史日线，避免 N+1
            from datetime import datetime, timedelta
            lookback_start = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
            hist_map = self.dm.get_stocks_daily_batch(codes, lookback_start, trade_date)

            for code in codes:
                try:
                    hist = hist_map.get(code)
                    if hist is None or hist.empty:
                        continue

                    # 历史 DataFrame 末行作为当日行情；旧实现额外拉了一次单日数据，去除以减少一半 API 调用
                    row = hist.iloc[-1]
                    close = float(row.get('close', 0))
                    pre_close = float(row.get('pre_close', 0))
                    vol = float(row.get('vol', 0))
                    amount = float(row.get('amount', 0))
                    pct_chg = float(row.get('pct_chg', 0))

                    factors = {}

                    # D1: N日高低位 (默认20日)
                    if hist is not None and not hist.empty and len(hist) >= 5:
                        n_high = float(hist['high'].max())
                        n_low = float(hist['low'].min())
                        if n_high > n_low:
                            factors['D1_n_day_high_low'] = round(
                                (close - n_low) / (n_high - n_low) * 100, 1
                            )
                        else:
                            factors['D1_n_day_high_low'] = 50.0

                        # D5: 均线多头排列度
                        if 'close' in hist.columns:
                            closes = hist['close'].astype(float)
                            if len(closes) >= 60:
                                ma5 = closes.tail(5).mean()
                                ma10 = closes.tail(10).mean()
                                ma20 = closes.tail(20).mean()
                                ma60 = closes.tail(60).mean()
                                align_score = 0
                                if ma5 > ma10:
                                    align_score += 25
                                if ma10 > ma20:
                                    align_score += 25
                                if ma20 > ma60:
                                    align_score += 25
                                if close > ma5:
                                    align_score += 25
                                factors['D5_ma_bull_align'] = align_score
                            else:
                                factors['D5_ma_bull_align'] = 50.0
                    else:
                        factors['D1_n_day_high_low'] = 50.0
                        factors['D5_ma_bull_align'] = 50.0

                    # D2: 量价配合度
                    if vol > 0 and pre_close > 0:
                        vol_ratio = vol / max(float(hist['vol'].tail(5).mean()) if hist is not None and not hist.empty else vol, 1)
                        if pct_chg > 0 and vol_ratio > 1.0:
                            factors['D2_vol_price_coord'] = min(100, vol_ratio * 50 + 30)
                        elif pct_chg > 0 and vol_ratio <= 1.0:
                            factors['D2_vol_price_coord'] = max(0, vol_ratio * 40)
                        elif pct_chg < 0 and vol_ratio < 1.0:
                            factors['D2_vol_price_coord'] = 50.0
                        else:
                            factors['D2_vol_price_coord'] = max(0, 50 - vol_ratio * 20)
                    else:
                        factors['D2_vol_price_coord'] = 50.0

                    # D3: 封板强度 (从涨停池获取封板时间和封单)
                    zt_row = zt_pool[zt_pool[ts_code_col].astype(str) == code]
                    seal_score = 50.0
                    if not zt_row.empty:
                        zt_r = zt_row.iloc[0]
                        time_col = None
                        for tc in ['first_time', '首次封板时间', '封板时间']:
                            if tc in zt_pool.columns:
                                time_col = tc
                                break
                        if time_col:
                            ft = str(zt_r.get(time_col, '')).strip()
                            if ft <= '09:35:00':
                                seal_score = 90.0
                            elif ft <= '10:00:00':
                                seal_score = 75.0
                            elif ft <= '10:30:00':
                                seal_score = 60.0
                            elif ft <= '11:30:00':
                                seal_score = 45.0
                            elif ft <= '14:00:00':
                                seal_score = 30.0
                            else:
                                seal_score = 15.0
                    factors['D3_seal_strength'] = seal_score

                    # D4: 换手率健康度
                    turnover = float(row.get('turnover_rate', row.get('turnover', 0)))
                    if turnover <= 0 and amount > 0:
                        turnover = amount / 1e8
                    if 3 <= turnover <= 15:
                        factors['D4_turnover_health'] = 80.0
                    elif 1 <= turnover < 3:
                        factors['D4_turnover_health'] = 60.0
                    elif 15 < turnover <= 25:
                        factors['D4_turnover_health'] = 50.0
                    elif turnover > 25:
                        factors['D4_turnover_health'] = 30.0
                    else:
                        factors['D4_turnover_health'] = 40.0

                    result.stock_tech_factors[code] = factors

                except Exception as e:
                    logger.debug(f"[Layer3] D因子计算失败 {code}: {e}")

            logger.info(f"[Layer3] D1-D5个股技术因子计算完成: {len(result.stock_tech_factors)}只")
        except Exception as e:
            logger.warning(f"[Layer3] D因子批量计算失败: {e}")

    def _compute_moneyflow_factors(self, result: StockSelectionResult,
                                    zt_pool: pd.DataFrame, trade_date: str):
        """
        E1-E4: 资金流向因子计算

        E1: 主力净流入占比 - 主力净额/成交额
        E2: 散户净流入占比 - 散户净额/成交额
        E3: 大单买入占比 - 大单买入/总成交
        E4: 资金流向趋势 - 近N日主力净流入方向
        """
        try:
            ts_code_col = None
            for col in ['ts_code', '代码', 'code']:
                if col in zt_pool.columns:
                    ts_code_col = col
                    break
            if ts_code_col is None or zt_pool.empty:
                return

            codes = zt_pool[ts_code_col].astype(str).tolist()[:30]

            # P2-1: 单次 moneyflow_summary 拉全市场当日资金流，并按 ts_code 索引
            summary_df = pd.DataFrame()
            try:
                summary_df = self.dm.get_moneyflow_summary(trade_date)
            except Exception as e:
                logger.debug(f"[Layer3] 资金流汇总拉取失败: {e}")
            summary_map = {}
            if not summary_df.empty and 'ts_code' in summary_df.columns:
                summary_map = {row['ts_code']: row for _, row in summary_df.iterrows()}

            # P2-1: 近5日趋势用 N 次全市场汇总（5 次），替代 N x 5 次单股调用
            hist_dates = []
            hist_maps: List[dict] = []
            try:
                date_list = self.dm.date_utils.get_last_n_trade_dates(5, trade_date)
            except Exception:
                date_list = []
            for d in date_list:
                try:
                    df_d = self.dm.get_moneyflow_summary(d)
                    if not df_d.empty and 'ts_code' in df_d.columns:
                        hist_dates.append(d)
                        hist_maps.append({row['ts_code']: row for _, row in df_d.iterrows()})
                except Exception:
                    continue

            for code in codes:
                try:
                    factors = {}

                    row = summary_map.get(code)
                    if row is not None:
                        buy_elg = float(row.get('buy_elg_amount', 0) or 0)
                        sell_elg = float(row.get('sell_elg_amount', 0) or 0)
                        buy_lg = float(row.get('buy_lg_amount', 0) or 0)
                        sell_lg = float(row.get('sell_lg_amount', 0) or 0)
                        buy_md = float(row.get('buy_md_amount', 0) or 0)
                        sell_md = float(row.get('sell_md_amount', 0) or 0)
                        buy_sm = float(row.get('buy_sm_amount', 0) or 0)
                        sell_sm = float(row.get('sell_sm_amount', 0) or 0)

                        total_amount = buy_elg + sell_elg + buy_lg + sell_lg + buy_md + sell_md + buy_sm + sell_sm

                        if total_amount > 0:
                            main_net = (buy_elg + buy_lg) - (sell_elg + sell_lg)
                            retail_net = (buy_md + buy_sm) - (sell_md + sell_sm)

                            factors['E1_main_net_ratio'] = round(main_net / total_amount * 100, 2)
                            factors['E2_retail_net_ratio'] = round(retail_net / total_amount * 100, 2)
                            factors['E3_large_buy_ratio'] = round(
                                (buy_elg + buy_lg) / total_amount * 100, 2
                            )

                            # E4: 近 5 日主力净流入趋势（基于全市场汇总）
                            net_flows = []
                            for hist_map in hist_maps:
                                hr = hist_map.get(code)
                                if hr is None:
                                    continue
                                b_elg = float(hr.get('buy_elg_amount', 0) or 0)
                                s_elg = float(hr.get('sell_elg_amount', 0) or 0)
                                b_lg = float(hr.get('buy_lg_amount', 0) or 0)
                                s_lg = float(hr.get('sell_lg_amount', 0) or 0)
                                net_flows.append((b_elg + b_lg) - (s_elg + s_lg))

                            if net_flows:
                                positive_days = sum(1 for nf in net_flows if nf > 0)
                                factors['E4_moneyflow_trend'] = round(
                                    positive_days / len(net_flows) * 100, 1
                                )
                            else:
                                factors['E4_moneyflow_trend'] = 50.0
                        else:
                            factors['E1_main_net_ratio'] = 0.0
                            factors['E2_retail_net_ratio'] = 0.0
                            factors['E3_large_buy_ratio'] = 0.0
                            factors['E4_moneyflow_trend'] = 50.0
                    else:
                        factors['E1_main_net_ratio'] = 0.0
                        factors['E2_retail_net_ratio'] = 0.0
                        factors['E3_large_buy_ratio'] = 0.0
                        factors['E4_moneyflow_trend'] = 50.0

                    result.moneyflow_factors[code] = factors

                except Exception as e:
                    logger.debug(f"[Layer3] E因子计算失败 {code}: {e}")

            logger.info(f"[Layer3] E1-E4资金流向因子计算完成: {len(result.moneyflow_factors)}只")
        except Exception as e:
            logger.warning(f"[Layer3] E因子批量计算失败: {e}")