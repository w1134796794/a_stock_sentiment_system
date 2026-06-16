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
from core.exceptions import (
    LayerExecutionError,
    EmptyResultError,
    PipelineError,
    StockSentimentError,
)

logger = loguru.logger


@dataclass
class LayerTiming:
    """单层耗时记录（P3-7）"""
    layer: str = ""
    stage: str = ""
    elapsed_ms: float = 0.0
    success: bool = True


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
    # 完整 TradePlanResult（含 plans 列表），供 Layer4.5 风控闸门消费
    # 类型：``core.pipeline.layer4_trade_plan.TradePlanResult`` 或 None
    trade_plan_result: Optional[Any] = None

    # Sprint R-2/R-3：Layer4.5 风控闸门
    # 虚拟账户状态（实盘与回测共用）：``risk.portfolio_state.PortfolioState`` 或 None
    portfolio_state: Optional[Any] = None
    # 风控闸门结果：``core.pipeline.layer4_5_risk_gate.RiskGateResult`` 或 None
    risk_gate_result: Optional[Any] = None

    # 因子数据（跨层收集）
    stock_tech_factors: Dict[str, Dict] = field(default_factory=dict)
    moneyflow_factors: Dict[str, Dict] = field(default_factory=dict)
    # Phase 2：本次生效的因子 profile 与启用因子清单（复盘归因留痕）
    factor_profile: str = ""
    enabled_factors: List[str] = field(default_factory=list)
    # 因子收集器输出的 JSON 路径（pipeline.execute 末尾写入）
    factor_results_path: str = ""

    # ETL Phase 3/4：Gold 表筛选结果与轻量分析摘要（旁路接入，不替换旧 L3/L4）
    etl_screening: Dict[str, Any] = field(default_factory=dict)
    etl_gold_summary: Dict[str, Any] = field(default_factory=dict)

    # Sprint F：龙虎榜 / 游资信誉（LHBResult）
    # 类型：``core.analysis.lhb_analyzer.LHBResult`` 或 None
    # available=False 表示积分不足 / 无 token，下游需降级跳过
    lhb_result: Optional[Any] = None

    # Sprint F-7：游资信誉对信号/评分的调整记录（list[LHBAdjustment]）
    # 供报告层与日志展示"因 XX 黑名单游资接盘 → 降权/降仓"
    lhb_adjustments: List[Any] = field(default_factory=list)

    # Sprint F-8：板块游资共识度对主线评分的加权记录（list[LHBSectorBoost]）
    lhb_sector_boosts: List[Any] = field(default_factory=list)

    # Sprint D-2：周期 × 模式胜率矩阵（基于近 N 天 factor_results JSON）
    # 类型：``core.analysis.cycle_pattern_matrix.CyclePatternMatrix`` 或 None
    cycle_pattern_matrix: Optional[Any] = None

    # Sprint E-1：情绪相位 / 转换预警（前瞻分析）
    # 类型：``core.analysis.emotion_phase.EmotionPhaseResult`` 或 None
    emotion_phase: Optional[Any] = None

    # Sprint E-2：历史相似日 Top-K KNN 匹配结果
    # 类型：``core.analysis.similar_day_finder.SimilarDayResult`` 或 None
    similar_days: Optional[Any] = None

    # Layer 5 输出
    review_result: Optional[ReviewResult] = None

    # Phase 1 数据解耦：当日只读数据集 + 只读仓库门面
    # 类型：``core.data.market_dataset.MarketDataset`` / ``core.data.repository.StockRepository``
    dataset: Optional[Any] = None
    repo: Optional[Any] = None

    # 元数据
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    execution_time: datetime = field(default_factory=datetime.now)

    # P3-7：每层耗时记录
    timings: List[LayerTiming] = field(default_factory=list)


class ReviewPipeline:
    """
    复盘流水线编排器

    按五层架构编排每日复盘流程，替代 main.py 中的大方法
    """

    def __init__(self, data_manager, industry_mapper=None):
        self.dm = data_manager
        self.mapper = industry_mapper

        from core.data.data_prep import DataPrep
        self.data_prep = DataPrep(self.dm)

        self.layer1 = MarketEnvAnalyzer(self.dm)
        self.layer2 = SectorAnalysisLayer(self.dm)
        self.layer3 = StockSelectionLayer(self.dm, industry_mapper)
        self.layer4 = TradePlanLayer(self.dm)
        self.layer5 = ReviewAnalyzer(self.dm)

        logger.info("[ReviewPipeline] 初始化完成")

    def execute(self, trade_date: str, *,
                from_layer: int = 1, ctx: Optional[SharedContext] = None) -> SharedContext:
        """
        执行完整五层复盘流水线

        Args:
            trade_date: 交易日期（YYYYMMDD）
            from_layer: 起始层（1-5）；指定 N>1 时会跳过前面 N-1 层，需要外部预填 ctx（P3-8）
            ctx:        预填的 SharedContext（与 from_layer 配合使用）

        Returns:
            SharedContext: 包含所有层分析结果的共享上下文
        """
        if ctx is None:
            ctx = SharedContext(trade_date=trade_date)
        else:
            ctx.trade_date = trade_date or ctx.trade_date

        # 解析日期
        self._resolve_dates(ctx)

        # Phase 1：Layer1 早于基础数据层。先把 universe 无关域（all_daily/limit_up/板块）预取成
        # 数据集仓库，供 Layer1 直接命中（消除其 all_daily/limit_up 回退告警）；daily 等 universe
        # 相关域待 _fetch_base_data 拿到涨停池后补进同一数据集。预取失败则降级纯透传，绝不致命。
        from core.data.repository import StockRepository
        ctx.repo = StockRepository.passthrough(self.dm)
        # 仅整流程（from_layer<=1，会真正跑 Layer1+基础数据层）才提前预取；按层 resume 保持原透传行为。
        if from_layer <= 1:
            try:
                ctx.dataset = self.data_prep.build(
                    ctx.trade_date, ctx.prev_trade_date,
                    index_codes=list(self.layer1.index_codes.values()))
                ctx.repo = StockRepository(ctx.dataset, dm=self.dm, strict=False)
            except Exception as e:  # noqa: BLE001 —— 预取永不致命
                logger.debug(f"[Pipeline] Layer1 前市场域预取失败，将回退 dm：{e}")

        logger.info("=" * 80)
        logger.info(f"[ReviewPipeline] 开始执行五层复盘流水线: {ctx.trade_date}")
        logger.info(f"[ReviewPipeline] 前一交易日: {ctx.prev_trade_date}")
        logger.info("=" * 80)

        try:
            # P3-8：from_layer 跳过前置层（要求 ctx 已包含前置数据）
            if from_layer <= 1:
                self._run_layer(ctx, "L1", "看大盘",
                                lambda: setattr(ctx, "market_env", self._execute_layer1(ctx)),
                                done_msg=lambda: f"综合评分={ctx.market_env.composite_score:.0f}, 建议仓位={ctx.market_env.suggested_position}")
                self._run_layer(ctx, "L1.5", "基础数据",
                                lambda: self._fetch_base_data(ctx),
                                done_msg=lambda: f"涨停{len(ctx.zt_pool)}只, 跌停{len(ctx.limit_down_df)}只")

            if from_layer <= 2:
                self._run_layer(ctx, "L2", "看板块",
                                lambda: self._execute_layer2(ctx),
                                done_msg=lambda: (f"热点概念{len(ctx.hot_concepts)}个, "
                                                  f"热点行业{len(ctx.hot_industries)}个, "
                                                  f"主线{len(ctx.main_themes_df)}条"))

            if from_layer <= 3:
                self._run_layer(ctx, "L3", "看个股",
                                lambda: self._execute_layer3(ctx),
                                done_msg=lambda: (f"原始信号{sum(len(v) for v in ctx.patterns.values())}个, "
                                                  f"排序后{len(ctx.ranked_signals)}个"))
                # Sprint F：龙虎榜 / 游资信誉（F-7 信号降权降仓 + F-8 板块共识加权主线）
                self._run_layer(ctx, "L3.45", "龙虎榜",
                                lambda: self._execute_lhb(ctx),
                                done_msg=lambda: (
                                    f"上榜股{len(ctx.lhb_result.stock_profiles)}只, "
                                    f"板块{len(ctx.lhb_result.sector_profiles)}个"
                                    if ctx.lhb_result and ctx.lhb_result.available
                                    else "(无游资明细/降级)"
                                ))
                # Sprint E-1：情绪相位 / 转换预警（依赖 emotion_result，L3 之后）
                self._run_layer(ctx, "L3.6", "情绪相位",
                                lambda: self._execute_emotion_phase(ctx),
                                done_msg=lambda: (
                                    f"{ctx.emotion_phase.cycle_name}-{ctx.emotion_phase.phase_label}, "
                                    f"进度{ctx.emotion_phase.phase_progress:.0%}, "
                                    f"{ctx.emotion_phase.transition_warning}"
                                    if ctx.emotion_phase else "(无)"
                                ))

            if from_layer <= 4:
                self._run_layer(ctx, "L4", "定计划",
                                lambda: self._execute_layer4(ctx),
                                done_msg=lambda: f"交易计划{len(ctx.trade_plans_df)}条")
                # Sprint R-2/R-3：风控闸门——交易计划逐条过组合层硬约束 + 账户级熔断
                self._run_layer(ctx, "L4.5", "风控闸门",
                                lambda: self._execute_risk_gate(ctx),
                                done_msg=lambda: (
                                    f"通过{ctx.risk_gate_result.passed} "
                                    f"降级{ctx.risk_gate_result.downgraded} "
                                    f"拒绝{ctx.risk_gate_result.rejected}, "
                                    f"熔断={ctx.risk_gate_result.cb_status.level}"
                                    if ctx.risk_gate_result and ctx.risk_gate_result.cb_status
                                    else "(跳过)"
                                ))

            if from_layer <= 5:
                self._run_layer(ctx, "L5", "盘后总结",
                                lambda: setattr(ctx, "review_result", self._execute_layer5(ctx)),
                                done_msg=lambda: ctx.review_result.review_summary if ctx.review_result else "(空)")
                # Sprint D-2：周期 × 模式胜率矩阵（基于历史 factor_results JSON）
                self._run_layer(ctx, "L5.5", "周期模式",
                                lambda: self._execute_cycle_pattern_matrix(ctx),
                                done_msg=lambda: (
                                    f"样本 {ctx.cycle_pattern_matrix.sample_count_total} 个, "
                                    f"周期×模式 = "
                                    f"{len(ctx.cycle_pattern_matrix.cycles)}×{len(ctx.cycle_pattern_matrix.patterns)}"
                                    if ctx.cycle_pattern_matrix else "(无数据)"
                                ))

        except StockSentimentError as e:
            # 项目自定义异常 —— 已经携带 layer / stage / context，无需重复堆栈
            logger.error(f"[Pipeline] 业务异常: {e}")
            ctx.errors.append(str(e))
        except Exception as e:
            logger.error(f"[Pipeline] 流水线执行异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            ctx.errors.append(str(e))

        ctx.execution_time = datetime.now()
        logger.info("=" * 80)
        logger.info("[ReviewPipeline] 五层流水线执行完成")
        # P3-7：分层耗时摘要
        if ctx.timings:
            total = sum(t.elapsed_ms for t in ctx.timings)
            logger.info(f"[ReviewPipeline] 总耗时 {total:.0f}ms，分层明细：")
            for t in ctx.timings:
                status = "OK " if t.success else "ERR"
                logger.info(f"  [{status}] {t.layer:5s} {t.stage:8s} {t.elapsed_ms:>8.0f}ms")
        logger.info("=" * 80)

        # ========== 因子结果收集与保存 ==========
        # P2-2：不再静默吞异常 —— 打印堆栈，并把 JSON 路径挂到 ctx 上，
        # 供下游报告生成器（_write_factor_raw / _write_factor_dashboard）直读。
        try:
            from core.factors.factor_collector import FactorCollector
            collector = FactorCollector()
            ctx.factor_results_path = collector.collect_and_save(ctx, ctx.trade_date)
        except Exception as e:
            import traceback
            logger.warning(f"[Pipeline] 因子结果收集失败: {e}")
            logger.debug(traceback.format_exc())
            ctx.factor_results_path = ""

        # Sprint E-2：历史相似日 KNN（必须在 factor_collector 之后，否则今日 JSON 未落盘）
        # 这里"今日"在 factor_results 池里也会存在，但 find_similar_days 会自动按
        # trade_date 严格 < today 过滤掉，所以是安全的。
        try:
            self._execute_similar_days(ctx)
        except Exception as e:
            import traceback
            logger.warning(f"[Pipeline] 相似日匹配失败: {e}")
            logger.debug(traceback.format_exc())
            ctx.similar_days = None

        return ctx

    def _run_layer(self, ctx: SharedContext, layer: str, stage: str,
                   action, done_msg=None) -> None:
        """
        统一执行单个 Layer：自动日志 + 计时 + 异常包装为 LayerExecutionError。

        action 抛出 StockSentimentError 时透传，其它异常被包装为 LayerExecutionError。
        日志通过 loguru.bind() 携带结构化字段 (P3-7)。
        """
        import time as _t
        bound = logger.bind(layer=layer, stage=stage, trade_date=ctx.trade_date)
        bound.info(f"[Pipeline] >>> {layer}: {stage}")
        t0 = _t.perf_counter()
        try:
            action()
        except StockSentimentError:
            elapsed_ms = (_t.perf_counter() - t0) * 1000
            ctx.timings.append(LayerTiming(layer=layer, stage=stage,
                                           elapsed_ms=elapsed_ms, success=False))
            raise
        except Exception as e:
            elapsed_ms = (_t.perf_counter() - t0) * 1000
            ctx.timings.append(LayerTiming(layer=layer, stage=stage,
                                           elapsed_ms=elapsed_ms, success=False))
            raise LayerExecutionError(
                f"{layer} {stage} 执行失败: {e}",
                layer=layer, stage=stage, cause=e,
            ) from e
        elapsed_ms = (_t.perf_counter() - t0) * 1000
        ctx.timings.append(LayerTiming(layer=layer, stage=stage,
                                       elapsed_ms=elapsed_ms, success=True))
        msg = done_msg() if callable(done_msg) else ""
        bound.bind(elapsed_ms=round(elapsed_ms, 1)).info(
            f"[Pipeline] <<< {layer} 完成 ({elapsed_ms:.0f}ms): {msg}"
        )

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
            ctx.review_result = self._execute_layer5(ctx)
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

    def _execute_layer1(self, ctx: SharedContext):
        """执行 Layer 1: 大盘环境（注入只读仓库后分析）"""
        self.layer1.repo = ctx.repo
        return self.layer1.analyze(ctx.trade_date)

    def _fetch_base_data(self, ctx: SharedContext):
        """获取基础数据"""
        from core.data.repository import StockRepository

        # 涨停池
        ctx.zt_pool = self.dm.get_limit_up_pool(ctx.trade_date)
        if ctx.zt_pool.empty:
            logger.warning(f"未获取到 {ctx.trade_date} 的涨停数据")
            # 仍提供透传仓库，保证下游业务层有 repo 可用（行为=直连 dm）
            ctx.repo = StockRepository.passthrough(self.dm)
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

        # Phase 1 数据解耦：装配只读仓库（非严格：未命中回退 dm）。
        # universe 无关域（all_daily/limit_up/板块）已在 Layer1 前预取进 ctx.dataset；这里只需把
        # universe 相关的 daily 补进**同一数据集**（复用，避免重复预取），再重建仓库即可。
        try:
            if getattr(ctx, "dataset", None) is not None:
                # daily universe 扩展：除今日涨停外，纳入昨日/前日涨停股（其涨停池已在数据集里），
                # 覆盖 Layer3 情绪引擎对「前日涨停股次日表现」的逐股 daily 查询。
                extra_pools = []
                for d in (ctx.prev_trade_date, ctx.day_before_prev):
                    pool = ctx.dataset.get_limit_up(d) if d else None
                    if pool is not None and not pool.empty:
                        extra_pools.append(pool)
                self.data_prep.prefetch_universe_daily(
                    ctx.dataset, zt_pool=ctx.zt_pool, trade_date=ctx.trade_date,
                    extra_pools=extra_pools)
            else:
                ctx.dataset = self.data_prep.build(
                    ctx.trade_date, ctx.prev_trade_date, zt_pool=ctx.zt_pool,
                    index_codes=list(self.layer1.index_codes.values()))
            ctx.repo = StockRepository(ctx.dataset, dm=self.dm, strict=False)
        except Exception as e:  # 预取/装配失败不致命：回退纯透传
            logger.warning(f"[Pipeline] 数据集预取失败，回退透传仓库：{e}")
            ctx.repo = StockRepository.passthrough(self.dm)

    def _execute_layer2(self, ctx: SharedContext):
        """执行 Layer 2: 板块分析"""
        # Phase 1：下发当日只读仓库（经 orchestrator 透传给板块/概念子分析器）
        self.layer2.repo = ctx.repo
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

        # Phase 1：把当日只读仓库下发给 Layer3（供模式识别经 repo 取数）
        self.layer3.repo = ctx.repo

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
        ctx.factor_profile = result.factor_profile
        ctx.enabled_factors = result.enabled_factors

        if ctx.market_env:
            ctx.market_env.cross_judgment = self.layer1.cross_analyze_with_emotion(
                ctx.market_env,
                result.emotion_cycle,
            )
            logger.info(f"[Layer3] 大盘+情绪交叉判断: {ctx.market_env.cross_judgment}")

        self._execute_etl_screening_sidecar(ctx)

    def _execute_etl_screening_sidecar(self, ctx: SharedContext):
        """Phase 3/4：从 Gold 指标表跑配置化筛选，作为旧 L3 的并行对照结果。"""
        try:
            from config.settings import FACTOR_DB_PATH

            if not Path(FACTOR_DB_PATH).exists():
                return
            from core.screening.gold_analysis import build_gold_analysis_summary
            from core.screening.screening_engine import ScreeningEngine

            screening = ScreeningEngine().run(ctx.trade_date, profile="default", persist=True)
            ctx.etl_screening = screening.to_dict()
            ctx.etl_gold_summary = build_gold_analysis_summary(ctx.trade_date)
            logger.info(
                f"[ETL Screening] profile={screening.profile}, "
                f"输入{screening.input_count}只, 输出{len(screening.final)}只"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ETL Screening] 旁路筛选失败，旧流程继续: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    def _execute_cycle_pattern_matrix(self, ctx: SharedContext):
        """Sprint D-2：算周期 × 模式胜率矩阵。

        - 数据源：``output/factor_results/*.json`` 近 30 天
        - 失败降级：矩阵是统计型功能，没数据时填空对象，不阻塞报告
        """
        try:
            from core.analysis.cycle_pattern_matrix import compute_cycle_pattern_matrix
            ctx.cycle_pattern_matrix = compute_cycle_pattern_matrix(
                end_date=ctx.trade_date,
                lookback_days=30,
                data_manager=self.dm,
            )
        except Exception as e:
            logger.warning(f"[L5.5] 周期模式矩阵计算失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            from core.analysis.cycle_pattern_matrix import CyclePatternMatrix
            ctx.cycle_pattern_matrix = CyclePatternMatrix()

    def _execute_emotion_phase(self, ctx: SharedContext):
        """Sprint E-1：情绪相位 / 转换预警分析。

        失败降级：这是增量前瞻分析，不应拖垮主流水线。
        """
        try:
            from core.analysis.emotion_phase import analyze_emotion_phase
            ctx.emotion_phase = analyze_emotion_phase(ctx.emotion_result)
        except Exception as e:
            logger.warning(f"[L3.6] 情绪相位分析失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            ctx.emotion_phase = None

    def _execute_similar_days(self, ctx: SharedContext):
        """Sprint E-2：从 ``output/factor_results`` 中找出 Top-3 历史相似日。

        - 触发时机：``factor_collector`` 保存今日 JSON 之后
        - 依赖：emotion_result.metrics + market_env.composite_score
        - 排除：今日 + 最近 3 个交易日（避免"自己最像自己附近"）
        """
        try:
            from core.analysis.similar_day_finder import (
                build_today_snapshot_from_ctx,
                find_similar_days,
            )
            today_snap = build_today_snapshot_from_ctx(ctx)
            if today_snap is None:
                ctx.similar_days = None
                return
            ctx.similar_days = find_similar_days(
                today_snap,
                top_k=3,
                exclude_recent_days=3,
                lookback_max=120,
            )
            n = len(ctx.similar_days.similar_days) if ctx.similar_days else 0
            pool = ctx.similar_days.sample_pool_size if ctx.similar_days else 0
            logger.info(f"[L-Post] 相似日匹配完成，候选池={pool}，命中 Top {n}")
        except Exception as e:
            logger.warning(f"[L-Post] 相似日匹配失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            ctx.similar_days = None

    def _execute_lhb(self, ctx: SharedContext):
        """Sprint F：龙虎榜 / 游资信誉分析。

        - 用当日 zt_pool 构造 ``code_to_sector``，让板块共识度可算。
        - 失败 / 无积分 → ctx.lhb_result.available=False，下游静默降级。
        """
        try:
            from core.analysis.lhb_analyzer import analyze_lhb

            code_to_sector = self._build_code_to_sector(ctx)
            ctx.lhb_result = analyze_lhb(
                self.dm, ctx.trade_date, code_to_sector=code_to_sector
            )
        except Exception as e:
            logger.warning(f"[L3.45] 龙虎榜分析失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            ctx.lhb_result = None

        # Sprint F-7：用游资信誉调整信号置信度 / 综合评分（坏游资降权降仓、好游资加权）
        # 必须在 Layer4 交易计划之前完成，调整后的 composite_score 直接影响仓位档位。
        self._apply_lhb_signal_adjust(ctx)

        # Sprint F-8：用板块游资共识度加权 Layer2 主线评分（多游资共买→真主线↑，派发→降温↓）
        self._apply_lhb_sector_boost(ctx)

    def _apply_lhb_sector_boost(self, ctx: SharedContext):
        """Sprint F-8：板块游资共识度 → 主线评分加权，并重排主线榜。失败静默降级。"""
        if ctx.lhb_result is None or not getattr(ctx.lhb_result, "available", False):
            return
        sector_profiles = getattr(ctx.lhb_result, "sector_profiles", None)
        if not sector_profiles:
            return
        try:
            from core.analysis.lhb_sector_booster import boost_main_themes

            boosted_df, boosts = boost_main_themes(ctx.main_themes_df, sector_profiles)
            ctx.main_themes_df = boosted_df
            ctx.lhb_sector_boosts = boosts
        except Exception as e:
            logger.warning(f"[L3.45] 板块游资共识度加权失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    def _apply_lhb_signal_adjust(self, ctx: SharedContext):
        """Sprint F-7：游资信誉 → 信号/评分调整，并重排序。失败静默降级。"""
        if ctx.lhb_result is None or not getattr(ctx.lhb_result, "available", False):
            return
        try:
            from core.analysis.lhb_signal_adjuster import adjust_signals

            adjustments = adjust_signals(
                ctx.ranked_signals, ctx.composite_scores, ctx.lhb_result
            )
            ctx.lhb_adjustments = adjustments
            if not adjustments:
                return

            # 调整改了 confidence / total_score，需重排序保证下游消费顺序正确
            ctx.ranked_signals.sort(
                key=lambda s: (getattr(s, "priority", 0), getattr(s, "confidence", 0.0)),
                reverse=True,
            )
            ctx.composite_scores.sort(
                key=lambda c: getattr(c, "total_score", 0.0), reverse=True
            )
            for i, cs in enumerate(ctx.composite_scores, start=1):
                cs.rank = i
        except Exception as e:
            logger.warning(f"[L3.45] 游资信誉信号调整失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    def _build_code_to_sector(self, ctx: SharedContext) -> Dict[str, str]:
        """从涨停池构造 {ts_code: 板块} 映射，供龙虎榜板块共识度聚合。"""
        mapping: Dict[str, str] = {}
        zt = ctx.zt_pool
        if zt is None or zt.empty:
            return mapping
        for _, row in zt.iterrows():
            code = ""
            for key in ("代码", "code", "ts_code"):
                if key in row.index and pd.notna(row[key]):
                    code = str(row[key]).strip()
                    break
            if not code:
                continue
            if "." not in code and len(code) == 6:
                code = f"{code}.{'SH' if code.startswith(('6', '9')) else 'SZ'}"
            sector = ""
            for key in ("所属行业", "industry", "industry_l1", "主导概念", "concept"):
                if key in row.index and pd.notna(row[key]) and str(row[key]).strip():
                    sector = str(row[key]).strip()
                    break
            if sector:
                mapping[code] = sector
        return mapping

    def _execute_layer5(self, ctx: SharedContext):
        """执行 Layer 5: 盘后总结（注入只读仓库后分析）"""
        self.layer5.repo = ctx.repo
        return self.layer5.analyze(
            ctx.trade_date, ctx.patterns, ctx.emotion_result, ctx.market_env
        )

    def _execute_layer4(self, ctx: SharedContext):
        """执行 Layer 4: 交易计划生成"""
        emotion_cycle = ctx.emotion_result.get('cycle_name', '震荡期')

        self.layer4.repo = ctx.repo
        result = self.layer4.analyze(
            ctx.trade_date, ctx.ranked_signals, ctx.composite_scores,
            market_env=ctx.market_env, emotion_cycle=emotion_cycle,
            sector_positions=ctx.sector_positions
        )

        ctx.trade_plans_df = result.plans_df
        ctx.trade_plan_report = result.plan_summary
        ctx.trade_plan_result = result

        if not ctx.trade_plans_df.empty:
            logger.info(f"[Layer4] 交易计划生成完成: {len(ctx.trade_plans_df)}条")
            # 落盘：写入 backtest 兼容的 CSV
            try:
                from config.settings import OUTPUT_DIR
                self.layer4.save_to_disk(result, Path(OUTPUT_DIR) / "trade_plans")
            except Exception as e:
                logger.warning(f"[Layer4] 交易计划落盘失败: {e}")
        else:
            logger.info("[Layer4] 当日无交易计划生成")

    def _execute_risk_gate(self, ctx: SharedContext):
        """Sprint R-2/R-3：Layer4.5 风控闸门。

        - 载入虚拟账户（``data/cache/portfolio_state.json``，缺失则按配置初始资金新建）。
        - 先跑账户级熔断（单日亏损/回撤/情绪冰点），再逐条校验组合层硬约束。
        - 调整后的仓位写回 ``ctx.trade_plans_df``（新增 风控动作/风控后仓位/风控提示 列）。
        - 失败静默降级：风控闸门是增量防线，不应拖垮主流水线。
        """
        try:
            from pathlib import Path
            from risk.risk_config import RiskConfig
            from risk.portfolio_state import PortfolioState
            from core.pipeline.layer4_5_risk_gate import RiskGateLayer
            from config.settings import CACHE_DIR

            cfg = RiskConfig.load()
            state_path = Path(CACHE_DIR) / "portfolio_state.json"
            ctx.portfolio_state = PortfolioState.load(
                state_path, initial_capital=cfg.initial_capital
            )

            if ctx.trade_plan_result is None:
                logger.info("[L4.5] 无交易计划结果，风控闸门跳过")
                return

            emotion_cycle = (
                ctx.emotion_result.get("cycle_name", "震荡期")
                if ctx.emotion_result else "震荡期"
            )
            gate = RiskGateLayer(cfg)
            ctx.risk_gate_result = gate.gate(
                ctx.trade_plan_result,
                ctx.portfolio_state,
                emotion_cycle=emotion_cycle,
                trade_date=ctx.trade_date,
            )

            # 把风控调整透明地落到报告用的交易计划表
            if ctx.trade_plans_df is not None and not ctx.trade_plans_df.empty:
                ctx.trade_plans_df = gate.apply_to_dataframe(
                    ctx.trade_plans_df, ctx.risk_gate_result
                )
        except Exception as e:
            logger.warning(f"[L4.5] 风控闸门执行失败（降级跳过）: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            ctx.risk_gate_result = None

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
            # P0-1：把 Layer3 per-stock 因子、Layer4 交易计划、Layer5 复盘
            # 全部透传给报告层。下游 `_write_factor_dashboard` / `_write_trade_plans`
            # / `_write_review` 直接读这些键。
            'stock_tech_factors': ctx.stock_tech_factors,
            'moneyflow_factors': ctx.moneyflow_factors,
            # Phase 2：因子 profile + 启用因子清单（留痕）
            'factor_profile': ctx.factor_profile,
            'enabled_factors': ctx.enabled_factors,
            'trade_plans_df': ctx.trade_plans_df,
            'factor_results_path': ctx.factor_results_path,
            # ETL Phase 3/4：Gold 指标筛选旁路结果
            'etl_screening': ctx.etl_screening,
            'etl_gold_summary': ctx.etl_gold_summary,
            # Sprint D-2：周期 × 模式胜率矩阵
            'cycle_pattern_matrix': ctx.cycle_pattern_matrix,
            # Sprint E：情绪相位 + 历史相似日
            'emotion_phase': ctx.emotion_phase,
            'similar_days': ctx.similar_days,
            # Sprint F：龙虎榜 / 游资信誉
            'lhb_result': ctx.lhb_result,
            # Sprint F-7：游资信誉对信号/评分的调整明细
            'lhb_adjustments': ctx.lhb_adjustments,
            # Sprint F-8：板块游资共识度对主线评分的加权明细
            'lhb_sector_boosts': ctx.lhb_sector_boosts,
            # Sprint R-2/R-3：Layer4.5 风控闸门结果
            'risk_gate_result': ctx.risk_gate_result,
        }
