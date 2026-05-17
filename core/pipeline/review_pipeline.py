"""
复盘流水线编排器 - Review Pipeline Orchestrator

核心职责：
  1. 替代 main.py 中 400+ 行的 run_daily_analysis 方法
  2. 按五层架构编排复盘流程
  3. 通过 SharedContext 在各层之间传递数据
  4. 支持单层独立执行（便于调试）

五层流水线：
  Layer 1: 看大盘（定仓位）- MarketEnvAnalyzer
  Layer 2: 看板块（定方向）- SectorAnalysisOrchestrator
  Layer 3: 看个股（定标的）- StockSelectionEngine
  Layer 4: 定计划（定执行）- TradePlanGenerator
  Layer 5: 盘后总结 - ReviewAnalyzer
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import loguru

from core.pipeline.layer1_market_env import MarketEnvAnalyzer, MarketEnvResult
from core.pipeline.layer2_sector_analysis import SectorAnalysisLayer, SectorAnalysisResult
from core.pipeline.layer3_stock_selection import StockSelectionLayer, StockSelectionResult
from core.pipeline.layer4_trade_plan import TradePlanLayer, TradePlanResult
from core.pipeline.layer5_review import ReviewAnalyzer, ReviewResult
from core.pattern.signal_priority import RankedSignal
from core.stock_ranking.multi_factor_scorer import CompositeScore

logger = loguru.logger


@dataclass
class SharedContext:
    """
    流水线共享上下文

    每个 Layer 从此读取上游结果，写入本层结果
    避免重复数据获取，便于单步调试
    """
    trade_date: str = ""
    prev_trade_date: str = ""
    day_before_prev: str = ""

    # Layer 1 输出
    market_env: Optional[MarketEnvResult] = None

    # Layer 2 输出
    sector_result: Any = None
    hot_concepts_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    hot_industries_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    hot_concepts: List[str] = field(default_factory=list)
    hot_industries: List[str] = field(default_factory=list)
    concept_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    industry_persistence_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    main_themes_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    concept_hierarchy: Dict = field(default_factory=dict)
    concept_hierarchy_report: str = ""

    # Layer 3 输出
    zt_pool: pd.DataFrame = field(default_factory=pd.DataFrame)
    limit_down_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    hierarchy_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    emotion_result: Dict = field(default_factory=dict)
    patterns: Dict[str, List] = field(default_factory=dict)
    ranked_signals: List[RankedSignal] = field(default_factory=list)
    composite_scores: List[CompositeScore] = field(default_factory=list)
    sector_positions: Dict = field(default_factory=dict)

    # 资金流向和筹码结构
    moneyflow_analysis: Dict = field(default_factory=dict)
    chip_analysis: Dict = field(default_factory=dict)

    # 龙头池和走弱池
    dragon_pool_data: List[Dict] = field(default_factory=list)
    weakening_pool_data: List[Dict] = field(default_factory=list)

    # Layer 4 输出
    trade_plans_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    trade_plan_report: str = ""

    # 因子数据（跨层收集）
    stock_tech_factors: Dict[str, Dict] = field(default_factory=dict)
    moneyflow_factors: Dict[str, Dict] = field(default_factory=dict)

    # Layer 5 输出
    review_result: Optional[ReviewResult] = None

    # 元数据
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    execution_time: datetime = field(default_factory=datetime.now)


class ReviewPipeline:
    """
    复盘流水线编排器

    按五层架构编排每日复盘流程，替代 main.py 中的大方法
    """

    def __init__(self, data_manager, industry_mapper=None):
        self.dm = data_manager
        self.mapper = industry_mapper

        self.layer1 = MarketEnvAnalyzer(self.dm)
        self.layer2 = SectorAnalysisLayer(self.dm)
        self.layer3 = StockSelectionLayer(self.dm, industry_mapper)
        self.layer4 = TradePlanLayer(self.dm)
        self.layer5 = ReviewAnalyzer(self.dm)

        logger.info("[ReviewPipeline] 初始化完成")

    def execute(self, trade_date: str) -> SharedContext:
        """
        执行完整五层复盘流水线

        Args:
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            SharedContext: 包含所有层分析结果的共享上下文
        """
        ctx = SharedContext(trade_date=trade_date)

        # 解析日期
        self._resolve_dates(ctx)

        logger.info("=" * 80)
        logger.info(f"[ReviewPipeline] 开始执行五层复盘流水线: {ctx.trade_date}")
        logger.info(f"[ReviewPipeline] 前一交易日: {ctx.prev_trade_date}")
        logger.info("=" * 80)

        try:
            # ========== Layer 1: 看大盘（定仓位）==========
            logger.info("[Pipeline] >>> Layer 1: 看大盘（定仓位）")
            ctx.market_env = self.layer1.analyze(ctx.trade_date)
            logger.info(f"[Pipeline] <<< Layer 1 完成: 综合评分={ctx.market_env.composite_score:.0f}, "
                       f"建议仓位={ctx.market_env.suggested_position}")

            # ========== 数据获取（供后续层使用）==========
            logger.info("[Pipeline] >>> 数据获取")
            self._fetch_base_data(ctx)
            logger.info(f"[Pipeline] <<< 数据获取完成: 涨停{len(ctx.zt_pool)}只, 跌停{len(ctx.limit_down_df)}只")

            # ========== Layer 2: 看板块（定方向）==========
            logger.info("[Pipeline] >>> Layer 2: 看板块（定方向）")
            self._execute_layer2(ctx)
            logger.info(f"[Pipeline] <<< Layer 2 完成: 热点概念{len(ctx.hot_concepts)}个, "
                       f"热点行业{len(ctx.hot_industries)}个, 主线{len(ctx.main_themes_df)}条")

            # ========== Layer 3: 看个股（定标的）==========
            logger.info("[Pipeline] >>> Layer 3: 看个股（定标的）")
            self._execute_layer3(ctx)
            total_signals = sum(len(v) for v in ctx.patterns.values())
            logger.info(f"[Pipeline] <<< Layer 3 完成: 原始信号{total_signals}个, "
                       f"排序后{len(ctx.ranked_signals)}个")

            # ========== Layer 4: 定计划（定执行）==========
            logger.info("[Pipeline] >>> Layer 4: 定计划（定执行）")
            self._execute_layer4(ctx)
            logger.info(f"[Pipeline] <<< Layer 4 完成: 交易计划{len(ctx.trade_plans_df)}条")

            # ========== Layer 5: 盘后总结 ==========
            logger.info("[Pipeline] >>> Layer 5: 盘后总结")
            ctx.review_result = self.layer5.analyze(
                ctx.trade_date,
                ctx.patterns,
                ctx.emotion_result,
                ctx.market_env,
            )
            logger.info(f"[Pipeline] <<< Layer 5 完成: {ctx.review_result.review_summary}")

        except Exception as e:
            logger.error(f"[Pipeline] 流水线执行异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            ctx.errors.append(str(e))

        ctx.execution_time = datetime.now()
        logger.info("=" * 80)
        logger.info(f"[ReviewPipeline] 五层流水线执行完成")
        logger.info("=" * 80)

        # ========== 因子结果收集与保存 ==========
        try:
            from core.factors.factor_collector import FactorCollector
            collector = FactorCollector()
            collector.collect_and_save(ctx, ctx.trade_date)
        except Exception as e:
            logger.warning(f"[Pipeline] 因子结果收集失败: {e}")

        return ctx

    def execute_layer(self, layer_num: int, ctx: SharedContext) -> SharedContext:
        """
        单独执行某一层（用于调试）

        Args:
            layer_num: 层编号 (1-5)
            ctx: 共享上下文（需包含前置层数据）

        Returns:
            更新后的 SharedContext
        """
        if layer_num == 1:
            ctx.market_env = self.layer1.analyze(ctx.trade_date)
        elif layer_num == 2:
            self._execute_layer2(ctx)
        elif layer_num == 3:
            self._execute_layer3(ctx)
        elif layer_num == 4:
            self._execute_layer4(ctx)
        elif layer_num == 5:
            ctx.review_result = self.layer5.analyze(
                ctx.trade_date, ctx.patterns, ctx.emotion_result, ctx.market_env
            )
        else:
            raise ValueError(f"无效的层编号: {layer_num}，有效范围 1-5")

        return ctx

    def _resolve_dates(self, ctx: SharedContext):
        """解析交易日日期"""
        try:
            from core.utils.date_utils import DateUtils
            du = DateUtils()
            ctx.trade_date = du.get_nearest_trade_date(ctx.trade_date)
            ctx.prev_trade_date = du.get_prev_trade_date(ctx.trade_date)
            ctx.day_before_prev = du.get_prev_trade_date(ctx.prev_trade_date)
        except Exception:
            dt = datetime.strptime(ctx.trade_date, "%Y%m%d")
            ctx.prev_trade_date = (dt - timedelta(days=1)).strftime("%Y%m%d")
            ctx.day_before_prev = (dt - timedelta(days=2)).strftime("%Y%m%d")

    def _fetch_base_data(self, ctx: SharedContext):
        """获取基础数据"""
        # 涨停池
        ctx.zt_pool = self.dm.get_limit_up_pool(ctx.trade_date)
        if ctx.zt_pool.empty:
            logger.warning(f"未获取到 {ctx.trade_date} 的涨停数据")
            return

        # 跌停池
        try:
            ctx.limit_down_df = self.dm.get_limit_down_pool(ctx.trade_date)
        except Exception:
            ctx.limit_down_df = pd.DataFrame()

        # 行业层级映射
        if self.mapper and not ctx.zt_pool.empty:
            try:
                ctx.hierarchy_df = self.mapper.build_hierarchy_dataframe(ctx.zt_pool)
            except Exception:
                ctx.hierarchy_df = pd.DataFrame()

    def _execute_layer2(self, ctx: SharedContext):
        """执行 Layer 2: 板块分析"""
        result = self.layer2.analyze(ctx.trade_date, ctx.zt_pool, ctx.prev_trade_date)

        ctx.sector_result = result
        ctx.hot_concepts_df = result.hot_concepts_df
        ctx.hot_industries_df = result.hot_industries_df
        ctx.hot_concepts = result.hot_concepts
        ctx.hot_industries = result.hot_industries
        ctx.concept_persistence_df = result.concept_persistence_df
        ctx.industry_persistence_df = result.industry_persistence_df
        ctx.main_themes_df = result.main_themes_df
        ctx.concept_hierarchy = result.concept_hierarchy
        ctx.concept_hierarchy_report = result.concept_hierarchy_report

    def _execute_layer3(self, ctx: SharedContext):
        """执行 Layer 3: 个股筛选"""
        hot_sectors = []
        if self.layer2.orchestrator:
            try:
                hot_sectors = self.layer2.orchestrator.get_cached_hot_sectors_for_pattern(
                    ctx.trade_date
                )
                logger.info(f"[Pipeline] 从Layer2缓存获取热点板块: {len(hot_sectors)}个, trade_date={ctx.trade_date}")
            except Exception as e:
                logger.warning(f"[Pipeline] 获取缓存热点板块失败: {e}")
                import traceback
                logger.warning(traceback.format_exc())
        else:
            logger.warning("[Pipeline] Layer2 orchestrator 不可用")

        if not hot_sectors:
            logger.warning(f"[Pipeline] 热点板块列表为空，将触发首板突破策略的兜底计算")
            hot_sectors = None

        result = self.layer3.analyze(
            ctx.trade_date, ctx.prev_trade_date, ctx.day_before_prev,
            ctx.zt_pool, ctx.limit_down_df, ctx.hierarchy_df,
            market_env=ctx.market_env, hot_sectors=hot_sectors
        )

        ctx.emotion_result = result.emotion_result
        ctx.patterns = result.patterns
        ctx.ranked_signals = result.ranked_signals
        ctx.composite_scores = result.composite_scores
        ctx.sector_positions = result.sector_positions
        ctx.moneyflow_analysis = result.moneyflow_analysis
        ctx.chip_analysis = result.chip_analysis
        ctx.dragon_pool_data = result.dragon_pool_data
        ctx.weakening_pool_data = result.weakening_pool_data
        ctx.stock_tech_factors = result.stock_tech_factors
        ctx.moneyflow_factors = result.moneyflow_factors

        if ctx.market_env:
            ctx.market_env.cross_judgment = self.layer1.cross_analyze_with_emotion(
                ctx.market_env,
                result.emotion_cycle,
            )
            logger.info(f"[Layer3] 大盘+情绪交叉判断: {ctx.market_env.cross_judgment}")

    def _execute_layer4(self, ctx: SharedContext):
        """执行 Layer 4: 交易计划生成"""
        emotion_cycle = ctx.emotion_result.get('cycle_name', '震荡期')

        result = self.layer4.analyze(
            ctx.trade_date, ctx.ranked_signals, ctx.composite_scores,
            market_env=ctx.market_env, emotion_cycle=emotion_cycle,
            sector_positions=ctx.sector_positions
        )

        ctx.trade_plans_df = result.plans_df
        ctx.trade_plan_report = result.plan_summary

        if not ctx.trade_plans_df.empty:
            logger.info(f"[Layer4] 交易计划生成完成: {len(ctx.trade_plans_df)}条")
        else:
            logger.info("[Layer4] 当日无交易计划生成")

    def print_summary(self, ctx: SharedContext):
        """打印流水线执行摘要"""
        print("\n" + "=" * 70)
        print("【短线复盘流水线 - 执行摘要】")
        print("=" * 70)

        # Layer 1
        if ctx.market_env:
            print(f"\n[Layer 1 - 大盘环境]")
            print(f"  综合评分: {ctx.market_env.composite_score:.0f}/100")
            print(f"  风险等级: {ctx.market_env.risk_level}")
            print(f"  建议仓位: {ctx.market_env.suggested_position}")
            print(f"  {ctx.market_env.analysis_summary}")
            if ctx.market_env.cross_judgment:
                print(f"  交叉判断: {ctx.market_env.cross_judgment}")

        # Layer 2
        print(f"\n[Layer 2 - 板块分析]")
        print(f"  热点概念: {len(ctx.hot_concepts)}个")
        print(f"  热点行业: {len(ctx.hot_industries)}个")
        print(f"  市场主线: {len(ctx.main_themes_df)}条")
        if not ctx.concept_persistence_df.empty:
            for _, row in ctx.concept_persistence_df.head(3).iterrows():
                print(f"  {row['板块名称']}: 10天{row['热点天数']}次, 评分{row['持续性评分']}, [{row['所处阶段']}]")

        # Layer 3
        print(f"\n[Layer 3 - 个股筛选]")
        emotion = ctx.emotion_result.get('cycle_name', '未知')
        total_signals = sum(len(v) for v in ctx.patterns.values())
        print(f"  情绪周期: {emotion}")
        print(f"  原始信号: {total_signals}个")
        print(f"  排序后信号: {len(ctx.ranked_signals)}个")
        if ctx.composite_scores:
            print(f"  综合评分Top 3:")
            for score in ctx.composite_scores[:3]:
                print(f"    #{score.rank} {score.stock_name} [{score.pattern_type}] "
                     f"总分{score.total_score:.0f} → {score.suggested_action}")

        # Layer 4
        print(f"\n[Layer 4 - 交易计划]")
        print(f"  计划数量: {len(ctx.trade_plans_df)}条")
        if not ctx.trade_plans_df.empty:
            for _, row in ctx.trade_plans_df.head(3).iterrows():
                print(f"  {row.get('名称', '')} [{row.get('模式', '')}] "
                     f"目标{row.get('目标价', 0):.2f} 止损{row.get('止损价', 0):.2f}")

        # Layer 5
        if ctx.review_result:
            print(f"\n[Layer 5 - 盘后总结]")
            print(f"  {ctx.review_result.review_summary}")

        print("=" * 70)

    def get_context_dict(self, ctx: SharedContext) -> Dict:
        """将SharedContext转换为字典（用于报告生成）"""
        return {
            'date': ctx.trade_date,
            'market_env': self.layer1.to_dict(ctx.market_env) if ctx.market_env else {},
            'emotion_result': ctx.emotion_result,
            'mainline_df': ctx.main_themes_df,
            'patterns': ctx.patterns,
            'hierarchy_df': ctx.hierarchy_df,
            'zt_pool': ctx.zt_pool,
            'dragon_pool': ctx.dragon_pool_data,
            'weakening_pool': ctx.weakening_pool_data,
            'moneyflow_analysis': ctx.moneyflow_analysis,
            'chip_analysis': ctx.chip_analysis,
            'concept_hierarchy': ctx.concept_hierarchy,
            'concept_hierarchy_report': ctx.concept_hierarchy_report,
            'hot_concepts_df': ctx.hot_concepts_df,
            'hot_industries_df': ctx.hot_industries_df,
            'concept_persistence_df': ctx.concept_persistence_df,
            'industry_persistence_df': ctx.industry_persistence_df,
            'sector_result': ctx.sector_result,
            'ranked_signals': ctx.ranked_signals,
            'composite_scores': ctx.composite_scores,
            'sector_positions': ctx.sector_positions,
            'review_result': self.layer5.to_dict(ctx.review_result) if ctx.review_result else {},
        }