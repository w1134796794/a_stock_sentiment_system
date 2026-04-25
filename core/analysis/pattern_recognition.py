"""
模式识别引擎 - 统一入口
整合所有模式策略：弱转强、二板定龙、分歧转一致、首板突破、卡位板、炸板回封、龙二波等

使用方式：
    pr = PatternRecognition(data_manager)
    results = pr.scan_all_patterns(today_date, yesterday_date)
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import loguru

logger = loguru.logger


@dataclass
class PatternSignal:
    """模式信号数据结构"""
    pattern_type: str
    stock_code: str
    stock_name: str
    confidence: float
    description: str
    key_metrics: Dict
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size: str = "medium"  # light/medium/heavy
    validation_rules: List[str] = None
    l2_industry: str = ""  # 二级行业

    def __post_init__(self):
        if self.validation_rules is None:
            self.validation_rules = []


class PatternRecognition:
    """模式识别引擎 - 统一调度各策略模块"""

    def __init__(self, data_manager, sector_engine=None, mapper=None):
        self.dm = data_manager
        self.se = sector_engine
        self.mapper = mapper
        self.lookback_days = 20

        # 初始化各策略模块
        self._init_strategies()
    
    def _init_strategies(self):
        """初始化所有策略模块"""
        try:
            from core.pattern.weak_to_strong import WeakToStrongStrategy
            self.weak_to_strong = WeakToStrongStrategy(self.dm, self.se)
            logger.info("✓ 弱转强策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 弱转强策略加载失败: {e}")
            self.weak_to_strong = None
        
        try:
            from core.pattern.second_board_dragon import SecondBoardDragonStrategy
            self.second_board_dragon = SecondBoardDragonStrategy(self.dm, self.se)
            logger.info("✓ 二板定龙策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 二板定龙策略加载失败: {e}")
            self.second_board_dragon = None
        
        try:
            from core.pattern.first_board_breakout import HotspotFirstBoardStrategy
            self.first_board_breakout = HotspotFirstBoardStrategy(self.dm, self.se, self.mapper)
            logger.info("✓ 首板突破策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 首板突破策略加载失败: {e}")
            self.first_board_breakout = None
        
        # 注释掉不需要的策略：分歧转一致、卡位板、炸板回封
        # try:
        #     from core.pattern.divergence_to_consensus import DivergenceToConsensusStrategy
        #     self.divergence_to_consensus = DivergenceToConsensusStrategy(self.dm, self.se)
        #     logger.info("✓ 分歧转一致策略加载成功")
        # except Exception as e:
        #     logger.warning(f"✗ 分歧转一致策略加载失败: {e}")
        #     self.divergence_to_consensus = None
        self.divergence_to_consensus = None
        
        # try:
        #     from core.pattern.position_battle import PositionBattleStrategy
        #     self.position_battle = PositionBattleStrategy(self.dm)
        #     logger.info("✓ 卡位板策略加载成功")
        # except Exception as e:
        #     logger.warning(f"✗ 卡位板策略加载失败: {e}")
        #     self.position_battle = None
        self.position_battle = None
        
        # try:
        #     from core.pattern.blast_reseal import BlastResealAnalyzer
        #     self.blast_reseal = BlastResealAnalyzer(self.dm)
        #     logger.info("✓ 炸板回封策略加载成功")
        # except Exception as e:
        #     logger.warning(f"✗ 炸板回封策略加载失败: {e}")
        #     self.blast_reseal = None
        self.blast_reseal = None
        
        try:
            from core.pattern.dragon_second_wave import DragonSecondWaveStrategyV2
            self.dragon_second_wave = DragonSecondWaveStrategyV2(self.dm, self.se)
            logger.info("✓ 龙二波策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 龙二波策略加载失败: {e}")
            self.dragon_second_wave = None
        
        # 删除弱转强V2策略初始化（已合并到V1）
        self.weak_to_strong_v2 = None
    
    # ==================== 模式识别接口 ====================
    
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                              day_before_yesterday_df: pd.DataFrame = None,
                              today_date: str = None, yest_date: str = None,
                              history_pools: Dict[str, pd.DataFrame] = None) -> List[PatternSignal]:
        """
        弱转强模式识别 - 动态龙头跟踪版本
        
        Args:
            today_df: 今日涨停池数据
            yesterday_df: 昨日涨停池数据
            day_before_yesterday_df: 前日涨停池数据
            today_date: 今日日期 (YYYYMMDD)
            yest_date: 昨日日期 (YYYYMMDD)，用于获取准确的昨日收盘价
            history_pools: 历史涨停池数据（用于趋势龙头识别）
        """
        signals = []

        logger.debug(f"[弱转强] 开始检测，今日涨停{len(today_df)}只，昨日涨停{len(yesterday_df)}只")

        if self.weak_to_strong is None:
            logger.warning("[弱转强] 策略未加载")
            return signals

        if today_df.empty:
            logger.debug(f"[弱转强] 今日数据为空")
            return signals

        try:
            # ========== 阶段1：更新龙头池（识别新龙头 + 确认走弱）==========
            logger.info("[弱转强] ========== 阶段1：更新龙头池 ==========")
            
            # 构建历史池子数据（用于趋势龙头识别）
            logger.info(f"[弱转强] 传入的history_pools: {len(history_pools) if history_pools else 'None'}天")
            if history_pools is None:
                history_pools = {}
                if yesterday_df is not None and not yesterday_df.empty:
                    history_pools[yest_date] = yesterday_df
                if day_before_yesterday_df is not None and not day_before_yesterday_df.empty:
                    # 假设前日是昨天减1天
                    try:
                        from datetime import datetime, timedelta
                        day_before_date = (datetime.strptime(yest_date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
                        history_pools[day_before_date] = day_before_yesterday_df
                    except:
                        pass
                logger.info(f"[弱转强] 构建的history_pools: {len(history_pools)}天")
            
            # 获取分时数据（用于走弱确认）
            today_tick = {}
            logger.debug(f"[弱转强] 准备获取分时数据...")
            
            # 调用新的龙头池更新方法
            logger.info(f"[弱转强] 调用update_dragon_pools，当前日期: {today_date}")
            pool_results = self.weak_to_strong.update_dragon_pools(
                today_zt=today_df,
                today_tick=today_tick,
                history_pools=history_pools,
                date_str=today_date or datetime.now().strftime("%Y%m%d")
            )
            
            # 输出池子更新结果
            logger.info(f"[弱转强] 龙头池更新完成:")
            logger.info(f"[弱转强]   - 新识别龙头: {len(pool_results.get('new_dragons', []))}只")
            for dragon in pool_results.get('new_dragons', []):
                logger.info(f"[弱转强]     ✓ {dragon.stock_name}({dragon.stock_code}) - {dragon.dragon_type.value}")
            
            logger.info(f"[弱转强]   - 确认走弱: {len(pool_results.get('weakened', []))}只")
            for weakening in pool_results.get('weakened', []):
                logger.info(f"[弱转强]     ✓ {weakening.stock_name}({weakening.stock_code}) - {weakening.weakening_type}")
            
            logger.info(f"[弱转强]   - 过期清除: {len(pool_results.get('expired', []))}只")
            
            # 输出当前池子状态
            pool_summary = self.weak_to_strong.get_pools_summary()
            logger.info(f"[弱转强] 当前池子状态:")
            logger.info(f"[弱转强]   - 龙头候选池: {pool_summary['dragon_pool_count']}只")
            for dragon in pool_summary.get('dragon_pool', []):
                logger.debug(f"[弱转强]     • {dragon['名称']}({dragon['代码']}) - {dragon['龙头类型']} - {dragon['当前状态']}")
            
            logger.info(f"[弱转强]   - 龙头走弱池: {pool_summary['weakening_pool_count']}只")
            for weakening in pool_summary.get('weakening_pool', []):
                logger.info(f"[弱转强]     • {weakening['名称']}({weakening['代码']}) - {weakening['走弱类型']} - 回调{weakening['回调幅度']}")
            
            # ========== 阶段2：从走弱池中检测转强信号 ==========
            logger.info("[弱转强] ========== 阶段2：检测转强信号 ==========")
            
            total_checked = 0
            total_passed = 0

            for _, today_row in today_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                logger.debug(f"[弱转强] 检测 {name}({code})...")

                # 检查是否在走弱池中
                if code not in self.weak_to_strong.weakening_pool:
                    logger.debug(f"[弱转强]   {name} 不在走弱池中，跳过")
                    continue
                
                weakening = self.weak_to_strong.weakening_pool[code]
                logger.info(f"[弱转强]   {name} 在走弱池中，走弱类型={weakening.weakening_type}，开始分析转强...")

                # 获取昨日数据
                yest_match = yesterday_df[yesterday_df['代码'].astype(str).str.zfill(6) == code]
                if yest_match.empty:
                    logger.debug(f"[弱转强]   {name} 昨日未涨停，无法计算高开")
                    yest_row = None
                else:
                    yest_row = yest_match.iloc[0]

                # 分析竞价数据
                auction_analysis = {}
                if today_date and self.dm:
                    try:
                        auction_data = self.dm.get_auction_data(code, today_date)
                        if auction_data:
                            auction_analysis = self._analyze_auction_data(auction_data, yest_row, today_row, yest_date)
                            logger.debug(f"[弱转强]   {name} 竞价分析: 高开{auction_analysis.get('gap', 0)*100:.1f}%, 量比{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%")
                    except Exception as e:
                        logger.debug(f"[弱转强]   {name} 获取竞价数据失败: {e}")

                # 检查转强条件
                gap = auction_analysis.get('gap', 0)
                min_gap = self.weak_to_strong.params.get('min_gap', 0.03)
                max_gap = self.weak_to_strong.params.get('max_gap', 0.08)
                
                logger.debug(f"[弱转强]   {name} 转强条件检查: 高开{gap*100:.1f}% (要求{min_gap*100:.0f}%-{max_gap*100:.0f}%)")
                
                if not (min_gap <= gap <= max_gap):
                    logger.debug(f"[弱转强]   {name} 过滤: 高开{gap*100:.1f}%不在[{min_gap*100:.0f}%,{max_gap*100:.0f}%]范围内")
                    continue
                
                # 竞价量检查
                auction_vol_ratio = auction_analysis.get('auction_vol_ratio', 0)
                min_vol_ratio = self.weak_to_strong.params.get('min_auction_vol_ratio', 0.10)
                logger.debug(f"[弱转强]   {name} 竞价量比: {auction_vol_ratio*100:.1f}% (要求>{min_vol_ratio*100:.0f}%)")
                
                if auction_vol_ratio < min_vol_ratio:
                    logger.debug(f"[弱转强]   {name} 过滤: 竞价量比{auction_vol_ratio*100:.1f}% < {min_vol_ratio*100:.0f}%")
                    continue

                limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                time_score = self._calculate_time_score(limit_up_time)
                logger.debug(f"[弱转强]   {name} 涨停时间={limit_up_time}, 时间分={time_score}")

                # 计算置信度
                confidence = self._calculate_weak_to_strong_confidence_v2(weakening, auction_analysis, time_score)
                logger.debug(f"[弱转强]   {name} 综合置信度={confidence:.2f}")

                if confidence < 0.65:
                    logger.debug(f"[弱转强]   {name} 过滤: 置信度{confidence:.2f} < 0.65")
                    continue

                # 从weakening对象获取龙头信息
                peak_height = weakening.peak_board_height if weakening.dragon_type.value == "连板龙头" else 0
                weakening_type = weakening.weakening_type
                
                signal = PatternSignal(
                    pattern_type="弱转强",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"{weakening.dragon_type.value}{peak_height}板后{weakening_type}转强，次日高开{auction_analysis.get('gap', 0)*100:.1f}%竞价量{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%",
                    key_metrics={
                        "龙头类型": weakening.dragon_type.value,
                        "最高连板": peak_height,
                        "走弱类型": weakening_type,
                        "走弱日期": weakening.weakening_date,
                        "次日高开": f"{auction_analysis.get('gap', 0)*100:.1f}%",
                        "竞价量比": f"{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%",
                        "涨停时间": limit_up_time,
                        "回调幅度": f"{weakening.max_drawdown*100:.1f}%",
                    },
                    entry_price=today_row.get('涨停价', 0),
                    stop_loss=today_row.get('涨停价', 0) * 0.93,
                    take_profit=today_row.get('涨停价', 0) * 1.15,
                    position_size="heavy" if confidence >= 0.80 else "medium",
                    validation_rules=[
                        f"{weakening.dragon_type.value}{peak_height}板（身份）",
                        f"{weakening_type}后走弱（弱）",
                        f"次日高开{auction_analysis.get('gap', 0)*100:.1f}%（超预期）",
                        f"竞价量{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%（资金抢筹）",
                        "开盘不回踩，快速上板（确认强）"
                    ],
                    l2_industry=today_row.get('所属行业', '')
                )
                
                signals.append(signal)
                total_passed += 1
                logger.debug(f"[弱转强]   {name} 生成信号 (置信度{confidence:.2f})")

            logger.info(f"[弱转强] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[弱转强] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_second_board_dragon(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                                   day_before_yesterday_df: pd.DataFrame = None,
                                   today_date: str = None, yest_date: str = None) -> List[PatternSignal]:
        """
        二板定龙模式识别 - 使用连板数字段优化
        
        Args:
            today_df: 今日涨停池数据
            yesterday_df: 昨日涨停池数据
            day_before_yesterday_df: 前日涨停池数据
            today_date: 今日日期 (YYYYMMDD)，用于获取准确的开盘价
            yest_date: 昨日日期 (YYYYMMDD)，用于获取准确的昨收价
        """
        signals = []

        logger.debug(f"[二板定龙] 开始检测，今日涨停{len(today_df)}只")

        if self.second_board_dragon is None:
            logger.warning("[二板定龙] 策略未加载")
            return signals

        if today_df.empty:
            logger.debug(f"[二板定龙] 今日数据为空")
            return signals

        # 检查昨日涨停池是否为空
        if yesterday_df.empty:
            logger.debug(f"[二板定龙] 昨日数据为空，无法确认二板")
            return signals

        # 确保昨日涨停池有代码列
        code_col = None
        for col in ['代码', 'code', 'ts_code', 'Code']:
            if col in yesterday_df.columns:
                code_col = col
                break
        if code_col is None:
            logger.warning(f"[二板定龙] 昨日涨停池缺少代码列，可用列: {list(yesterday_df.columns)}")
            return signals

        try:
            total_checked = 0
            total_passed = 0

            # 直接使用连板数=2的股票（二板）
            for _, today_row in today_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                # 检查连板数是否为2
                board_count = today_row.get('连板数', 0)
                if board_count != 2:
                    logger.debug(f"[二板定龙]   {name} 连板数={board_count}，不是二板，跳过")
                    continue

                logger.debug(f"[二板定龙] 检测 {name}({code})，确认二板...")

                # 获取昨日数据用于分析首板质量
                yest_match = yesterday_df[yesterday_df[code_col].astype(str).str.zfill(6) == code]
                if yest_match.empty:
                    logger.debug(f"[二板定龙]   {name} 昨日未涨停，跳过")
                    continue

                yest_row = yest_match.iloc[0]
                logger.debug(f"[二板定龙]   {name} 确认昨日首板，今日二板")

                first_board_quality = self._analyze_first_board_quality(yest_row)
                logger.debug(f"[二板定龙]   {name} 首板质量: {first_board_quality}")

                if first_board_quality.get('score', 0) < 60:
                    logger.debug(f"[二板定龙]   {name} 过滤: 首板质量分{first_board_quality.get('score', 0)} < 60")
                    continue

                gap_ratio = self._calculate_gap_ratio(today_row, yest_row, today_date, yest_date)
                logger.debug(f"[二板定龙]   {name} 次日高开: {gap_ratio*100:.1f}%")

                if gap_ratio < 0.02:
                    logger.debug(f"[二板定龙]   {name} 过滤: 高开{gap_ratio*100:.1f}% < 2%")
                    continue

                limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                is_fast_limit = self._is_fast_limit(limit_up_time, max_time="09:40:00")
                logger.debug(f"[二板定龙]   {name} 涨停时间={limit_up_time}, 是否快速={is_fast_limit}")

                # 获取封单金额，尝试多个可能的字段名
                seal_amount = (today_row.get('封单额', 0) or 
                              today_row.get('封板资金', 0) or 
                              today_row.get('封单金额', 0) or
                              today_row.get('封单量', 0) * today_row.get('涨停价', 0))
                float_cap = today_row.get('流通市值', 1)
                seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
                logger.debug(f"[二板定龙]   {name} 封单额={seal_amount}, 流通市值={float_cap}, 封单强度: {seal_ratio*100:.2f}%")

                confidence = self._calculate_second_board_confidence(first_board_quality, gap_ratio, is_fast_limit, seal_ratio)
                logger.debug(f"[二板定龙]   {name} 综合置信度={confidence:.2f}")

                if confidence < 0.70:
                    logger.debug(f"[二板定龙]   {name} 过滤: 置信度{confidence:.2f} < 0.70")
                    continue

                signal = PatternSignal(
                    pattern_type="二板定龙",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"首板{first_board_quality.get('type', '硬板')}后，次日高开{gap_ratio*100:.1f}%，{limit_up_time}涨停，二板定龙",
                    key_metrics={
                        "首板类型": first_board_quality.get('type', ''),
                        "首板质量分": first_board_quality.get('score', 0),
                        "次日高开": f"{gap_ratio*100:.1f}%",
                        "涨停时间": limit_up_time,
                        "封单强度": f"{seal_ratio*100:.2f}%",
                        "买点时机": "竞价" if gap_ratio >= 0.04 else "开盘"
                    },
                    entry_price=today_row.get('涨停价', 0),
                    stop_loss=today_row.get('涨停价', 0) * 0.95,
                    take_profit=today_row.get('涨停价', 0) * 1.12,
                    position_size="heavy" if confidence >= 0.85 else "medium",
                    validation_rules=[
                        f"首板{first_board_quality.get('type', '硬板')}（硬逻辑）",
                        f"次日高开{gap_ratio*100:.1f}%（资金表态）",
                        f"{limit_up_time}涨停（速度）",
                        f"封单强度{seal_ratio*100:.2f}%（质量）"
                    ],
                    l2_industry=today_row.get('所属行业', '')
                )
                
                signals.append(signal)
                total_passed += 1
                logger.debug(f"[二板定龙]   {name} 生成信号 (置信度{confidence:.2f})")

            logger.info(f"[二板定龙] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[二板定龙] 检测失败: {e}", exc_info=True)

        return signals

    def detect_divergence_to_consensus(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                                       today_date: str = None, yest_date: str = None) -> List[PatternSignal]:
        """
        分歧转一致模式识别 - 完整策略逻辑
        
        Args:
            today_df: 今日涨停池数据
            yesterday_df: 昨日涨停池数据
            today_date: 今日日期 (YYYYMMDD)，用于获取准确的开盘价
            yest_date: 昨日日期 (YYYYMMDD)，用于获取准确的昨收价
        """
        signals = []

        logger.debug(f"[分歧转一致] 开始检测，今日涨停{len(today_df)}只，昨日涨停{len(yesterday_df)}只")

        if self.divergence_to_consensus is None:
            logger.warning("[分歧转一致] 策略未加载")
            return signals

        if today_df.empty or yesterday_df.empty:
            logger.debug(f"[分歧转一致] 数据为空，今日={today_df.empty}, 昨日={yesterday_df.empty}")
            return signals

        try:
            total_checked = 0
            total_passed = 0

            for _, today_row in today_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                logger.debug(f"[分歧转一致] 检测 {name}({code})...")

                yest_match = yesterday_df[yesterday_df['代码'].astype(str).str.zfill(6) == code]
                if yest_match.empty:
                    logger.debug(f"[分歧转一致]   {name} 昨日未涨停，跳过")
                    continue

                yest_row = yest_match.iloc[0]
                logger.debug(f"[分歧转一致]   {name} 昨日已涨停，检查分歧特征...")

                # 条件1: 昨日必须有分歧（炸板）
                blast_times = yest_row.get('炸板次数', 0)
                logger.debug(f"[分歧转一致]   {name} 昨日炸板次数={blast_times}")
                
                if blast_times == 0:
                    logger.debug(f"[分歧转一致]   {name} 过滤: 昨日无炸板，无分歧")
                    continue

                if blast_times > 5:
                    logger.debug(f"[分歧转一致]   {name} 过滤: 昨日炸板次数{blast_times} > 5，烂透了")
                    continue

                # 条件2: 昨日换手检查
                turnover = yest_row.get('换手率', 0)
                logger.debug(f"[分歧转一致]   {name} 昨日换手率={turnover:.1f}%")
                
                if not (15 <= turnover <= 35):
                    logger.debug(f"[分歧转一致]   {name} 过滤: 换手率{turnover:.1f}%不在[15%,35%]范围内")
                    continue

                # 条件3: 昨日涨停时间（尾盘板质量差）
                limit_up_time = str(yest_row.get('首次封板时间', '')).strip()
                last_seal_time = str(yest_row.get('最后封板时间', '')).strip()
                logger.debug(f"[分歧转一致]   {name} 昨日首次涨停={limit_up_time}, 最后封板={last_seal_time}")
                
                if limit_up_time > "14:30:00":
                    logger.debug(f"[分歧转一致]   {name} 过滤: 昨日尾盘涨停{limit_up_time}，偷袭板")
                    continue

                # 条件4: 今日高开（弱转强信号）
                gap_ratio = self._calculate_gap_ratio(today_row, yest_row, today_date, yest_date)
                logger.debug(f"[分歧转一致]   {name} 今日高开={gap_ratio*100:.1f}%")
                
                if not (0.02 <= gap_ratio <= 0.07):
                    logger.debug(f"[分歧转一致]   {name} 过滤: 高开{gap_ratio*100:.1f}%不在[2%,7%]范围内")
                    continue

                # 条件5: 今日涨停时间（快速上板）
                today_limit_time = str(today_row.get('首次封板时间', '')).strip()
                is_fast = self._is_fast_limit(today_limit_time, max_time="09:40:00")
                logger.debug(f"[分歧转一致]   {name} 今日涨停时间={today_limit_time}, 是否快速={is_fast}")
                
                if not is_fast:
                    logger.debug(f"[分歧转一致]   {name} 过滤: 今日涨停时间{today_limit_time}太晚")
                    continue

                # 计算置信度
                confidence = self._calculate_divergence_confidence(blast_times, turnover, gap_ratio, is_fast)
                logger.debug(f"[分歧转一致]   {name} 综合置信度={confidence:.2f}")

                if confidence < 0.70:
                    logger.debug(f"[分歧转一致]   {name} 过滤: 置信度{confidence:.2f} < 0.70")
                    continue

                signal = PatternSignal(
                    pattern_type="分歧转一致",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"昨日烂板（炸板{blast_times}次，换手{turnover:.1f}%），"
                               f"今日高开{gap_ratio*100:.1f}%{today_limit_time}涨停，分歧转一致",
                    key_metrics={
                        "昨日炸板次数": blast_times,
                        "昨日换手率": f"{turnover:.1f}%",
                        "昨日涨停时间": limit_up_time,
                        "今日高开": f"{gap_ratio*100:.1f}%",
                        "今日涨停时间": today_limit_time,
                        "买点时机": "竞价" if gap_ratio >= 0.04 else "开盘"
                    },
                    entry_price=today_row.get('涨停价', 0),
                    stop_loss=yest_row.get('最低价', today_row.get('涨停价', 0) * 0.93),
                    take_profit=today_row.get('涨停价', 0) * 1.15,
                    position_size="medium",
                    validation_rules=[
                        f"昨日炸板{blast_times}次（分歧）",
                        f"昨日换手{turnover:.1f}%（充分换手）",
                        f"今日高开{gap_ratio*100:.1f}%（超预期）",
                        f"{today_limit_time}涨停（一致性强）"
                    ],
                    l2_industry=today_row.get('所属行业', '')
                )
                
                signals.append(signal)
                total_passed += 1
                logger.debug(f"[分歧转一致]   {name} 生成信号 (置信度{confidence:.2f})")

            logger.info(f"[分歧转一致] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[分歧转一致] 检测失败: {e}", exc_info=True)

        return signals

    def detect_blast_reseal(self, today_df: pd.DataFrame, today_date: str = None) -> List[PatternSignal]:
        """
        炸板回封模式识别 - 完整策略逻辑
        """
        signals = []

        logger.debug(f"[炸板回封] 开始检测，今日涨停{len(today_df)}只")

        if self.blast_reseal is None:
            logger.warning("[炸板回封] 策略未加载")
            return signals

        if today_df.empty:
            logger.debug("[炸板回封] 今日数据为空")
            return signals

        try:
            total_checked = 0
            total_blast = 0
            total_passed = 0

            for _, today_row in today_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                # 检查是否有炸板
                blast_times = today_row.get('炸板次数', 0)
                if blast_times == 0:
                    continue

                total_blast += 1

                # 条件1: 炸板次数检查
                if blast_times > 3:
                    continue

                # 条件2: 涨停时间（早盘炸板才可能是洗盘）
                first_limit_time = str(today_row.get('首次封板时间', '')).strip()
                last_limit_time = str(today_row.get('最后封板时间', '')).strip()

                # 统一时间格式为 HH:MM:SS 进行比较
                first_limit_time_formatted = self._format_time(first_limit_time)
                if first_limit_time_formatted > "10:30:00":
                    continue

                # 条件3: 回封时间分析
                reseal_duration = self._calculate_reseal_duration(first_limit_time, last_limit_time, blast_times)
                
                if reseal_duration > 20:
                    continue

                # 条件4: 换手检查（全天换手不能太高）
                turnover = today_row.get('换手率', 0)
                
                if turnover > 35:
                    continue

                # 条件5: 封单质量
                seal_amount = today_row.get('封单额', 0) or today_row.get('封板资金', 0)
                float_cap = today_row.get('流通市值', 1)
                seal_ratio = seal_amount / float_cap if float_cap > 0 else 0

                # 计算置信度
                confidence = self._calculate_blast_reseal_confidence(blast_times, reseal_duration, turnover, seal_ratio)

                if confidence < 0.60:
                    continue

                # 判断信号类型
                signal_type = self._classify_blast_reseal(blast_times, reseal_duration, turnover)

                signal = PatternSignal(
                    pattern_type=f"炸板回封-{signal_type}",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"早盘涨停后炸板{blast_times}次，{reseal_duration}分钟回封，"
                               f"换手{turnover:.1f}%，封单强度{seal_ratio*100:.1f}%",
                    key_metrics={
                        "炸板次数": blast_times,
                        "首次涨停": first_limit_time,
                        "最后封板": last_limit_time,
                        "回封用时": f"{reseal_duration}分钟",
                        "全天换手": f"{turnover:.1f}%",
                        "封单强度": f"{seal_ratio*100:.2f}%",
                        "信号类型": signal_type
                    },
                    entry_price=today_row.get('涨停价', 0),
                    stop_loss=today_row.get('涨停价', 0) * 0.95,
                    take_profit=today_row.get('涨停价', 0) * 1.10,
                    position_size="light" if signal_type == "洗盘待定" else "medium",
                    validation_rules=[
                        f"炸板{blast_times}次（分歧）",
                        f"{reseal_duration}分钟回封（速度）",
                        f"全天换手{turnover:.1f}%（量能）",
                        f"封单强度{seal_ratio*100:.1f}%（质量）"
                    ],
                    l2_industry=today_row.get('所属行业', '')
                )
                
                signals.append(signal)
                total_passed += 1

            logger.info(f"[炸板回封] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 炸板{total_blast}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[炸板回封] 检测失败: {e}", exc_info=True)

        return signals

    # ==================== 辅助方法 ====================

    def _analyze_yesterday_weak(self, yesterday: pd.Series) -> Dict:
        """分析昨日'弱'的类型和质量"""
        blast_times = yesterday.get('炸板次数', 0)
        limit_up_time = str(yesterday.get('首次封板时间', '')).strip()
        last_seal_time = str(yesterday.get('最后封板时间', '')).strip()
        turnover = yesterday.get('换手率', 0)

        weak_type = ""
        if blast_times >= 2:
            weak_type = "烂板"
        elif last_seal_time and last_seal_time > "14:30:00":
            weak_type = "尾盘板"
        elif yesterday.get('涨跌幅', 0) < 9.5:
            weak_type = "断板"
        else:
            return {'is_valid_weak': False, 'reason': '昨日不弱', 'score': 0}

        score = 60
        if 15 <= turnover <= 30:
            score += 20
        elif turnover < 10:
            score -= 20

        if 2 <= blast_times <= 4:
            score += 10
        elif blast_times > 5:
            score -= 30

        if last_seal_time and last_seal_time > "14:50:00":
            score += 10

        return {
            'is_valid_weak': score >= 60,
            'weak_type': weak_type,
            'score': score,
            'blast_times': blast_times,
            'turnover': turnover
        }

    def _analyze_auction_data(self, auction: Dict, yesterday: pd.Series, today: pd.Series,
                               yest_date: str = None) -> Dict:
        """
        分析竞价数据
        
        Args:
            auction: 竞价数据字典
            yesterday: 昨日涨停池数据（可能为None）
            today: 今日涨停池数据
            yest_date: 昨日日期 (YYYYMMDD)，用于获取准确的昨日收盘价
        """
        open_price = auction.get('开盘价', 0) if auction else 0
        
        # 从今日数据获取代码和名称
        code = str(today.get('代码', '')).zfill(6) if today is not None else ''
        name = today.get('名称', '') if today is not None else ''
        
        # 获取昨日收盘价
        yest_close = 0
        
        # 1. 优先从昨日涨停池数据获取
        if yesterday is not None and not isinstance(yesterday, type(None)):
            yest_close = yesterday.get('收盘价', 0)
            if yest_close == 0:
                yest_close = yesterday.get('最新价', 0)
        
        # 2. 如果涨停池没有，使用tushare日线数据
        if yest_close == 0 and self.dm and yest_date and code:
            try:
                daily_price = self.dm.get_stock_daily_price(code, yest_date)
                if daily_price:
                    yest_close = daily_price.get('close', 0)
                    logger.debug(f"[_analyze_auction_data] {name}({code}) 从tushare获取昨日收盘价: {yest_close}")
            except Exception as e:
                logger.debug(f"[_analyze_auction_data] {name}({code}) 从tushare获取收盘价失败: {e}")
        
        # 3. 如果仍然没有，使用pre_close从今日数据推算
        if yest_close == 0 and today is not None:
            yest_close = today.get('昨收', 0) or today.get('pre_close', 0)
            if yest_close > 0:
                logger.debug(f"[_analyze_auction_data] {name}({code}) 使用今日数据的昨收价: {yest_close}")
        
        # 计算高开幅度
        if yest_close > 0 and open_price > 0:
            gap = (open_price - yest_close) / yest_close
            logger.debug(f"[_analyze_auction_data] {name}({code}) 高开计算: ({open_price} - {yest_close}) / {yest_close} = {gap*100:.2f}%")
        else:
            gap = 0
            logger.debug(f"[_analyze_auction_data] {name}({code}) 数据不足无法计算高开: open={open_price}, yest_close={yest_close}")

        # 计算竞价量比
        auction_vol = auction.get('竞价成交量', 0) if auction else 0
        yest_vol = yesterday.get('成交量', 1) if yesterday is not None else 1
        auction_vol_ratio = auction_vol / yest_vol if yest_vol > 0 else 0

        return {
            'gap': gap,
            'auction_vol_ratio': auction_vol_ratio,
            'open_price': open_price
        }

    def _calculate_time_score(self, limit_up_time: str) -> int:
        """计算涨停时间得分"""
        if not limit_up_time:
            return 0
        try:
            time_obj = datetime.strptime(limit_up_time, "%H:%M:%S")
            if time_obj <= datetime.strptime("09:30:00", "%H:%M:%S"):
                return 100
            elif time_obj <= datetime.strptime("09:40:00", "%H:%M:%S"):
                return 90
            elif time_obj <= datetime.strptime("10:00:00", "%H:%M:%S"):
                return 80
            elif time_obj <= datetime.strptime("10:30:00", "%H:%M:%S"):
                return 70
            elif time_obj <= datetime.strptime("11:30:00", "%H:%M:%S"):
                return 60
            else:
                return 40
        except:
            return 50

    def _calculate_board_height(self, code: str, today_row: pd.Series, 
                                yesterday_df: pd.DataFrame,
                                day_before_yesterday_df: pd.DataFrame = None) -> int:
        """计算连板高度"""
        height = 1
        
        if yesterday_df is not None and not yesterday_df.empty:
            yest_match = yesterday_df[yesterday_df['代码'].astype(str).str.zfill(6) == code]
            if not yest_match.empty:
                height += 1
                
                if day_before_yesterday_df is not None and not day_before_yesterday_df.empty:
                    prev_match = day_before_yesterday_df[day_before_yesterday_df['代码'].astype(str).str.zfill(6) == code]
                    if not prev_match.empty:
                        height += 1
        
        return height

    def _analyze_first_board_quality(self, yest_row: pd.Series) -> Dict:
        """分析首板质量"""
        limit_up_time = str(yest_row.get('首次封板时间', '')).strip()
        blast_times = yest_row.get('炸板次数', 0)
        turnover = yest_row.get('换手率', 0)

        score = 60
        board_type = "硬板"

        # 统一时间格式为分钟数（从0点开始）用于比较
        time_minutes = self._time_to_minutes(limit_up_time)
        
        if time_minutes is not None:
            # 09:25:00 且 无炸板 -> 一字板
            if time_minutes == 9 * 60 + 25 and blast_times == 0:
                score += 25
                board_type = "一字板"
            elif time_minutes <= 9 * 60 + 35:  # 09:35:00 之前
                score += 20
                board_type = "秒板"
            elif time_minutes <= 10 * 60:  # 10:00:00 之前
                score += 10
                board_type = "早盘板"
            elif time_minutes < 14 * 60:  # 10:00:00 - 14:00:00
                board_type = "盘中板"
            else:  # 14:00:00 之后
                score -= 10
                board_type = "尾盘板"

        if blast_times == 0:
            score += 15
        elif blast_times <= 2:
            score += 5
        else:
            score -= 15
            board_type = "烂板"

        if 5 <= turnover <= 20:
            score += 10

        return {'score': score, 'type': board_type}

    def _time_to_minutes(self, time_str: str) -> int:
        """
        将时间字符串转换为从0点开始的分钟数
        支持格式: HH:MM:SS, HH:MM, HHMMSS, HHMM
        返回: 分钟数 (如 09:30:00 -> 570)，解析失败返回 None
        """
        if not time_str or time_str == '-':
            return None
        
        try:
            time_str = str(time_str).strip().replace(':', '')
            
            # 统一转换为 HHMM 格式
            if len(time_str) == 6:  # HHMMSS 格式
                time_str = time_str[:4]  # 取 HHMM
            elif len(time_str) == 5:  # HMMSS 格式 (如 93500)
                time_str = '0' + time_str[:3]  # 补0变成 0935
            elif len(time_str) == 4:  # HHMM 格式
                pass  # 已经是 HHMM
            elif len(time_str) == 3:  # HMM 格式 (如 935)
                time_str = '0' + time_str  # 补0变成 0935
            else:
                return None
            
            hour = int(time_str[:2])
            minute = int(time_str[2:4])
            
            # 验证时间范围
            if 0 <= hour < 24 and 0 <= minute < 60:
                return hour * 60 + minute
        except Exception:
            pass
        
        return None

    def _calculate_gap_ratio(self, today_row: pd.Series, yest_row: pd.Series,
                              today_date: str = None, yest_date: str = None) -> float:
        """
        计算高开幅度: (当日开盘价 - 昨日收盘价) / 昨日收盘价
        
        优先从涨停池数据获取，如果没有则使用tushare接口获取准确的开盘价和昨收价
        """
        code = str(today_row.get('代码', '')).zfill(6)
        name = today_row.get('名称', '')
        
        today_open = today_row.get('开盘价', 0)
        yest_close = yest_row.get('收盘价', 0)
        
        # 如果涨停池数据中没有开盘价/收盘价，尝试使用tushare接口获取
        if (today_open == 0 or yest_close == 0) and self.dm and today_date and yest_date:
            try:
                # 获取今日开盘价
                if today_open == 0:
                    today_price = self.dm.get_stock_daily_price(code, today_date)
                    if today_price:
                        today_open = today_price.get('open', 0)
                        logger.debug(f"[_calculate_gap_ratio] {name} 从tushare获取今日开盘价: {today_open}")
                
                # 获取昨日收盘价（close，不是pre_close）
                if yest_close == 0:
                    yest_price = self.dm.get_stock_daily_price(code, yest_date)
                    if yest_price:
                        yest_close = yest_price.get('close', 0)
                        logger.debug(f"[_calculate_gap_ratio] {name} 从tushare获取昨日收盘价: {yest_close}")
            except Exception as e:
                logger.debug(f"[_calculate_gap_ratio] {name} 从tushare获取价格失败: {e}")
        
        # 如果仍然没有数据，尝试用最新价和涨跌幅反推（备用方案）
        if today_open == 0:
            latest_price = today_row.get('最新价', 0)
            change_pct = today_row.get('涨跌幅', 0)
            if isinstance(change_pct, str):
                change_pct = float(change_pct.replace('%', ''))
            if latest_price > 0 and change_pct != 0:
                today_open = latest_price / (1 + change_pct / 100)
                logger.debug(f"[_calculate_gap_ratio] {name} 反推开盘价: {latest_price} / (1 + {change_pct}/100) = {today_open}")
        
        if yest_close == 0:
            yest_close = yest_row.get('最新价', 0)
            logger.debug(f"[_calculate_gap_ratio] {name} 使用昨日最新价作为收盘价: {yest_close}")
        
        logger.debug(f"[_calculate_gap_ratio] {name} 今日开盘价={today_open}, 昨日收盘价={yest_close}")
        
        if yest_close > 0 and today_open > 0:
            gap = (today_open - yest_close) / yest_close
            logger.debug(f"[_calculate_gap_ratio] {name} 计算高开: ({today_open} - {yest_close}) / {yest_close} = {gap*100:.2f}%")
            return gap
        
        logger.debug(f"[_calculate_gap_ratio] {name} 数据不足，返回0: yest_close={yest_close}, today_open={today_open}")
        return 0

    def _is_fast_limit(self, limit_up_time: str, max_time: str = "09:40:00") -> bool:
        """判断是否快速涨停"""
        if not limit_up_time:
            return False
        try:
            # 统一格式化时间
            formatted_time = self._format_time(limit_up_time)
            return formatted_time <= max_time
        except:
            return False

    def _format_time(self, time_str: str) -> str:
        """将时间字符串统一格式化为 HH:MM:SS"""
        if not time_str:
            return ""
        time_str = str(time_str).strip()
        # 如果已经是 HH:MM:SS 格式，直接返回
        if len(time_str) == 8 and ":" in time_str:
            return time_str
        # 如果是 HHMMSS 格式（如 93002），转换为 HH:MM:SS
        if len(time_str) == 5 or len(time_str) == 6:
            time_str = time_str.zfill(6)  # 补齐为 6 位
            return f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
        return time_str

    def _calculate_reseal_duration(self, first_time: str, last_time: str, blast_times: int) -> int:
        """计算回封用时（估算）"""
        if not first_time or not last_time:
            return 999
        try:
            # 统一时间格式
            first_time = self._format_time(first_time)
            last_time = self._format_time(last_time)
            fmt = "%H:%M:%S"
            start = datetime.strptime(first_time, fmt)
            end = datetime.strptime(last_time, fmt)
            duration = int((end - start).total_seconds() / 60)
            # 减去炸板次数（每次炸板约1-2分钟）
            return max(duration - blast_times * 2, 1)
        except:
            return 999

    def _calculate_weak_to_strong_confidence(self, weak_quality: Dict, auction: Dict, 
                                              time_score: int, board_height: int) -> float:
        """计算弱转强置信度"""
        confidence = 0.65
        
        if weak_quality.get('score', 0) >= 70:
            confidence += 0.10
        
        gap = auction.get('gap', 0)
        if gap >= 0.04:
            confidence += 0.10
        elif gap >= 0.02:
            confidence += 0.05
        
        vol_ratio = auction.get('auction_vol_ratio', 0)
        if vol_ratio >= 0.10:
            confidence += 0.10
        elif vol_ratio >= 0.08:
            confidence += 0.05
        
        if time_score >= 80:
            confidence += 0.10
        elif time_score >= 60:
            confidence += 0.05
        
        if board_height >= 5:
            confidence += 0.05
        
        return min(confidence, 0.95)

    def _calculate_second_board_confidence(self, quality: Dict, gap: float, 
                                            is_fast: bool, seal_ratio: float) -> float:
        """计算二板定龙置信度"""
        confidence = 0.70
        
        if quality.get('score', 0) >= 80:
            confidence += 0.10
        elif quality.get('score', 0) >= 70:
            confidence += 0.05
        
        if gap >= 0.05:
            confidence += 0.10
        elif gap >= 0.03:
            confidence += 0.05
        
        if is_fast:
            confidence += 0.10
        
        if seal_ratio >= 0.05:
            confidence += 0.10
        elif seal_ratio >= 0.02:
            confidence += 0.05
        
        return min(confidence, 0.95)

    def _calculate_divergence_confidence(self, blast_times: int, turnover: float, 
                                          gap: float, is_fast: bool) -> float:
        """计算分歧转一致置信度"""
        confidence = 0.70
        
        if 2 <= blast_times <= 3:
            confidence += 0.05
        
        if 20 <= turnover <= 30:
            confidence += 0.10
        
        if gap >= 0.04:
            confidence += 0.10
        elif gap >= 0.02:
            confidence += 0.05
        
        if is_fast:
            confidence += 0.10
        
        return min(confidence, 0.90)

    def _calculate_blast_reseal_confidence(self, blast_times: int, duration: int, 
                                            turnover: float, seal_ratio: float) -> float:
        """计算炸板回封置信度"""
        confidence = 0.60
        
        if blast_times == 1:
            confidence += 0.10
        
        if duration <= 5:
            confidence += 0.15
        elif duration <= 10:
            confidence += 0.10
        elif duration <= 15:
            confidence += 0.05
        
        if turnover <= 25:
            confidence += 0.10
        
        if seal_ratio >= 0.03:
            confidence += 0.10
        
        return min(confidence, 0.90)

    def _classify_blast_reseal(self, blast_times: int, duration: int, turnover: float) -> str:
        """分类炸板回封信号类型"""
        if blast_times == 1 and duration <= 10 and turnover <= 25:
            return "洗盘成功"
        elif blast_times <= 2 and duration <= 20 and turnover <= 30:
            return "洗盘待定"
        else:
            return "谨慎观察"

    def detect_first_board_breakout(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame = None,
                                    date_str: str = None, history_pools: Dict[str, pd.DataFrame] = None,
                                    hot_sectors: List[Dict] = None) -> List[PatternSignal]:
        """
        首板突破模式识别 - 调用HotspotFirstBoardStrategy
        
        Args:
            hot_sectors: 预计算的热点板块列表（避免重复计算板块热度）
        """
        signals = []


        if self.first_board_breakout is None:
            logger.warning("[首板突破] 策略未加载")
            return signals

        if today_df.empty:
            return signals

        try:
            # history_pools的键已经是YYYYMMDD格式，只需排除今日数据（避免重复统计）
            history_pools_filtered = {}
            if history_pools:
                for date_key, pool_df in history_pools.items():
                    # 排除今日数据，因为detect_first_board_by_sectors内部会单独处理今日数据
                    if date_key != date_str:
                        history_pools_filtered[date_key] = pool_df
            logger.debug(f"[首板突破] 过滤后历史池: {len(history_pools_filtered)}天 (排除今日)")
            trade_signals = self.first_board_breakout.detect_first_board_by_sectors(
                today_df, history_pools_filtered, date_str, hot_sectors=hot_sectors
            )

            for ts in trade_signals:
                pattern_signal = PatternSignal(
                    pattern_type="首板突破",
                    stock_code=ts.stock_code,
                    stock_name=ts.stock_name,
                    confidence=ts.confidence,
                    description=ts.reason,
                    key_metrics=ts.key_metrics,
                    entry_price=ts.entry_price,
                    stop_loss=ts.stop_loss,
                    take_profit=ts.take_profit,
                    position_size=ts.position_size,
                    validation_rules=ts.validation_rules,
                    l2_industry=getattr(ts, 'l2_industry', '')
                )
                signals.append(pattern_signal)

            logger.info(f"[首板突破] 检测完成: 共{len(signals)}个信号")

        except Exception as e:
            logger.error(f"[首板突破] 检测失败: {e}", exc_info=True)

        return signals

    def detect_position_battle(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                               sector_mapping: Dict[str, str] = None) -> List[PatternSignal]:
        """
        卡位板模式识别 - 调用PositionBattleStrategy
        """
        signals = []

        logger.debug(f"[卡位板] 开始检测，今日涨停{len(today_df)}只，昨日涨停{len(yesterday_df)}只")

        if self.position_battle is None:
            logger.warning("[卡位板] 策略未加载")
            return signals

        if today_df.empty or yesterday_df.empty:
            logger.debug(f"[卡位板] 数据为空，今日={today_df.empty}, 昨日={yesterday_df.empty}")
            return signals

        try:
            logger.debug(f"[卡位板] 调用PositionBattleStrategy.detect_position_battle...")
            battle_signals = self.position_battle.detect_position_battle(
                today_df, today_df, yesterday_df
            )

            logger.debug(f"[卡位板] 策略返回 {len(battle_signals)} 个原始信号")

            for bs in battle_signals:
                signal = PatternSignal(
                    pattern_type="卡位板",
                    stock_code=bs.low_stock_code,
                    stock_name=bs.low_stock_name,
                    confidence=bs.confidence,
                    description=f"卡位{bs.high_stock_name}，领先{bs.lead_time}分钟",
                    key_metrics={
                        "卡位类型": bs.battle_type.value if hasattr(bs.battle_type, 'value') else str(bs.battle_type),
                        "高位股": bs.high_stock_name,
                        "领先时间": f"{bs.lead_time}分钟",
                        "操作建议": bs.action
                    },
                    entry_price=None,
                    stop_loss=None,
                    take_profit=None,
                    position_size="medium",
                    validation_rules=["低位抢先涨停", "封单质量优势"],
                    l2_industry=""
                )
                signals.append(signal)
                logger.debug(f"[卡位板] 转换信号: {bs.low_stock_name} 卡位 {bs.high_stock_name} (置信度{bs.confidence:.2f})")

            logger.info(f"[卡位板] 检测完成: 共{len(signals)}个信号")

        except Exception as e:
            logger.error(f"[卡位板] 检测失败: {e}", exc_info=True)

        return signals

    def detect_dragon_second_wave(self, today_df: pd.DataFrame,
                                  recent_zt_pools: Dict[str, pd.DataFrame],
                                  today_date: str,
                                  hot_sectors: List[str] = None) -> List[PatternSignal]:
        """
        龙二波模式识别 - 调用DragonSecondWaveStrategyV2
        """
        signals = []

        if self.dragon_second_wave is None:
            logger.warning("[龙二波] 策略未加载")
            return signals

        if today_df.empty:
            return signals

        if not recent_zt_pools:
            return signals

        try:
            # 准备近15日涨停池（日期已经是YYYYMMDD格式）
            recent_15d_pools = {}

            if not today_df.empty:
                today_pool = today_df.copy()
                code_col = None
                if '代码' in today_pool.columns:
                    code_col = '代码'
                elif 'Code' in today_pool.columns:
                    code_col = 'Code'

                if code_col:
                    today_pool[code_col] = today_pool[code_col].astype(str).str.zfill(6)

                recent_15d_pools[today_date] = today_pool

            sorted_dates = sorted(recent_zt_pools.keys(), reverse=True)
            for date in sorted_dates[:14]:
                pool = recent_zt_pools[date].copy()
                code_col = None
                if '代码' in pool.columns:
                    code_col = '代码'
                elif 'Code' in pool.columns:
                    code_col = 'Code'

                if code_col and not pool.empty:
                    pool[code_col] = pool[code_col].astype(str).str.zfill(6)

                recent_15d_pools[date] = pool

            total_checked = 0
            total_passed = 0

            for _, today_row in today_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                sector_hot = False
                stock_sector = today_row.get('所属行业', '')
                if hot_sectors and stock_sector:
                    sector_hot = stock_sector in hot_sectors

                # 日期已经是YYYYMMDD格式，直接使用
                # 获取历史数据以启用双轨制判断（连板 或 涨幅）
                hist_data = None
                try:
                    if hasattr(self.dm, 'get_stock_daily') and hasattr(self.dm, 'date_utils'):
                        # 获取近20日历史数据用于涨幅统计
                        # 使用 get_last_n_trade_dates 获取最近25个交易日
                        last_n_dates = self.dm.date_utils.get_last_n_trade_dates(25, today_date)
                        if last_n_dates:
                            start_date = last_n_dates[-1]  # 最早的一个
                            hist_data = self.dm.get_stock_daily(code, start_date, today_date)
                except Exception as e:
                    logger.debug(f"[龙二波] 获取{code}历史数据失败: {e}")
                
                trade_signal = self.dragon_second_wave.detect_second_wave(
                    stock_code=code,
                    stock_name=name,
                    today_str=today_date,
                    recent_zt_pools=recent_15d_pools,
                    today_data=today_row,
                    sector_hot=sector_hot,
                    hist_data=hist_data  # 传入历史数据启用双轨制
                )

                if trade_signal:
                    total_passed += 1

                    pattern_signal = PatternSignal(
                        pattern_type="龙二波",
                        stock_code=trade_signal.stock_code,
                        stock_name=trade_signal.stock_name,
                        confidence=trade_signal.confidence,
                        description=trade_signal.reason,
                        key_metrics=trade_signal.key_metrics,
                        entry_price=trade_signal.entry_price,
                        stop_loss=trade_signal.stop_loss,
                        take_profit=trade_signal.take_profit,
                        position_size=trade_signal.position_size,
                        validation_rules=trade_signal.validation_rules,
                        l2_industry=stock_sector
                    )
                    signals.append(pattern_signal)
                else:
                    logger.debug(f"[龙二波]   {name} 未通过检测")

            logger.info(f"[龙二波] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[龙二波] 检测失败: {e}", exc_info=True)

        return signals

    # 删除detect_weak_to_strong_v2方法（已合并到V1分层策略）
    # 保留此方法占位，避免调用处报错
    def detect_weak_to_strong_v2(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                                  today_date: str = None, yest_date: str = None) -> List[PatternSignal]:
        """弱转强V2已删除，请使用detect_weak_to_strong"""
        logger.debug("[弱转强V2] 策略已删除，请使用分层弱转强策略(V1)")
        return []

    def _normalize_date_to_ymd(self, date_str: str) -> str:
        """
        标准化日期格式为 YYYYMMDD
        支持输入格式: YYYYMMDD 或 YYYY-MM-DD
        """
        if not date_str:
            return date_str
        # 如果已经是 YYYYMMDD 格式，直接返回
        if len(date_str) == 8 and date_str.isdigit():
            return date_str
        # 如果是 YYYY-MM-DD 格式，转换为 YYYYMMDD
        if len(date_str) == 10 and date_str[4] == '-' and date_str[7] == '-':
            return date_str.replace("-", "")
        return date_str

    def _get_prev_trading_day(self, date_str: str, days: int = 1) -> str:
        """
        获取前N个交易日 - 使用交易日管理器
        
        Args:
            date_str: 日期字符串 YYYYMMDD
            days: 往前推几个交易日
            
        Returns:
            str: 前一个交易日的日期 YYYYMMDD
        """
        # 使用交易日管理器获取前N个交易日
        if self.dm and hasattr(self.dm, 'trade_date_mgr'):
            return self.dm.trade_date_mgr.get_prev_trade_date(date_str, days)
        
        # 备用方案：简化处理（仅跳过周末）
        from datetime import datetime, timedelta
        dt = datetime.strptime(date_str, "%Y%m%d")
        count = 0
        while count < days:
            dt = dt - timedelta(days=1)
            if dt.weekday() < 5:  # 周一到周五
                count += 1
        return dt.strftime("%Y%m%d")

    def scan_all_patterns(self, today_date: str, yesterday_date: str = None, hot_sectors: List[Dict] = None) -> Dict[str, List[PatternSignal]]:
        """
        扫描所有模式 - 主入口方法
        
        Args:
            today_date: 今日日期 (YYYY-MM-DD 或 YYYYMMDD)
            yesterday_date: 昨日日期 (YYYY-MM-DD 或 YYYYMMDD)，如为None则自动计算
            hot_sectors: 预计算的热点板块列表（避免重复计算）
            
        Returns:
            Dict[str, List[PatternSignal]]: 各模式识别结果
        """
        # 标准化日期格式为YYYYMMDD（data_manager需要）
        today_date_ymd = self._normalize_date_to_ymd(today_date)
        yesterday_date_ymd = self._normalize_date_to_ymd(yesterday_date) if yesterday_date else None
        
        logger.info(f"=" * 60)
        logger.info(f"开始全模式扫描: 今日={today_date_ymd}, 昨日={yesterday_date_ymd}")
        logger.info(f"=" * 60)
        
        # 计算昨日日期（跳过周末）
        if yesterday_date_ymd is None:
            yesterday_date_ymd = self._get_prev_trading_day(today_date_ymd, 1)
        
        # 获取涨停池数据
        logger.info("获取涨停池数据...")
        today_df = self.dm.get_limit_up_pool(today_date_ymd)
        yesterday_df = self.dm.get_limit_up_pool(yesterday_date_ymd)
        
        # 获取前日数据（用于龙二波检测等，跳过周末）
        day_before_yesterday_ymd = self._get_prev_trading_day(yesterday_date_ymd, 1)
        day_before_yesterday_df = self.dm.get_limit_up_pool(day_before_yesterday_ymd)
        
        # 获取最近15个交易日涨停池数据（用于龙二波检测，跳过周末）
        logger.info("获取最近15个交易日涨停池数据...")
        recent_zt_pools = {}
        for i in range(1, 16):  # 从1开始，排除今日
            date_str_ymd = self._get_prev_trading_day(today_date_ymd, i)
            df = self.dm.get_limit_up_pool(date_str_ymd)
            if not df.empty:
                recent_zt_pools[date_str_ymd] = df
                logger.debug(f"  [{i}] {date_str_ymd}: {len(df)}只涨停")
            else:
                logger.debug(f"  [{i}] {date_str_ymd}: 无数据或空数据")
        
        logger.info(f"今日涨停: {len(today_df)}只, 昨日涨停: {len(yesterday_df)}只, 前日涨停: {len(day_before_yesterday_df)}只, 历史池: {len(recent_zt_pools)}天")
        
        results = {}
        
        # 1. 弱转强检测
        try:
            logger.info("-" * 40)
            results["弱转强"] = self.detect_weak_to_strong(
                today_df, yesterday_df, day_before_yesterday_df, today_date_ymd, yesterday_date_ymd,
                history_pools=recent_zt_pools
            )
        except Exception as e:
            logger.error(f"弱转强检测失败: {e}")
            results["弱转强"] = []
        
        # 注释掉弱转强V2检测（基于Tushare股东数据）
        # try:
        #     logger.info("-" * 40)
        #     logger.info("开始弱转强V2检测（基于实际换手率）...")
        #     results["弱转强V2"] = self.detect_weak_to_strong_v2(
        #         today_df, yesterday_df, today_date_ymd, yesterday_date_ymd
        #     )
        # except Exception as e:
        #     logger.error(f"弱转强V2检测失败: {e}")
        #     results["弱转强V2"] = []
        
        # 注释掉分歧转一致检测
        # try:
        #     logger.info("-" * 40)
        #     results["分歧转一致"] = self.detect_divergence_to_consensus(
        #         today_df, yesterday_df, today_date_ymd, yesterday_date_ymd
        #     )
        # except Exception as e:
        #     logger.error(f"分歧转一致检测失败: {e}")
        #     results["分歧转一致"] = []
        
        # 3. 二板定龙检测
        try:
            logger.info("-" * 40)
            results["二板定龙"] = self.detect_second_board_dragon(
                today_df, yesterday_df, day_before_yesterday_df,
                today_date_ymd, yesterday_date_ymd
            )
        except Exception as e:
            logger.error(f"二板定龙检测失败: {e}")
            results["二板定龙"] = []
        
        # 4. 龙二波检测
        try:
            logger.info("-" * 40)
            # 获取热点板块
            hot_sectors = []
            if self.se:
                try:
                    hot_sectors = self.se.get_hot_sectors(today_date_ymd, top_n=10)
                except Exception as e:
                    logger.warning(f"获取热点板块失败: {e}")
            results["龙二波"] = self.detect_dragon_second_wave(
                today_df, recent_zt_pools, today_date_ymd, hot_sectors
            )
        except Exception as e:
            logger.error(f"龙二波检测失败: {e}")
            results["龙二波"] = []
        
        # 5. 首板突破检测
        try:
            logger.info("-" * 40)
            results["首板突破"] = self.detect_first_board_breakout(
                today_df, yesterday_df, today_date_ymd, recent_zt_pools
            )
        except Exception as e:
            logger.error(f"首板突破检测失败: {e}")
            results["首板突破"] = []
        
        # 注释掉卡位板检测
        # try:
        #     logger.info("-" * 40)
        #     results["卡位板"] = self.detect_position_battle(
        #         today_df, yesterday_df, today_date_ymd
        #     )
        # except Exception as e:
        #     logger.error(f"卡位板检测失败: {e}")
        #     results["卡位板"] = []
        
        # 注释掉炸板回封检测（用于次日观察）
        # try:
        #     logger.info("-" * 40)
        #     results["炸板回封"] = self.detect_blast_reseal(
        #         today_df, today_date_ymd
        #     )
        # except Exception as e:
        #     logger.error(f"炸板回封检测失败: {e}")
        #     results["炸板回封"] = []
        
        # 汇总统计
        logger.info("=" * 60)
        logger.info("扫描完成，结果汇总:")
        total_signals = 0
        for pattern_type, signals in results.items():
            count = len(signals)
            total_signals += count
            logger.info(f"  {pattern_type}: {count}个信号")
        logger.info(f"  总计: {total_signals}个信号")
        logger.info("=" * 60)
        
        return results
    
    def get_top_signals(self, results: Dict[str, List[PatternSignal]], 
                        min_confidence: float = 0.70,
                        top_n: int = 20) -> List[PatternSignal]:
        """
        获取综合排名靠前的信号
        
        Args:
            results: scan_all_patterns返回的结果
            min_confidence: 最低置信度
            top_n: 返回前N个
            
        Returns:
            List[PatternSignal]: 排序后的信号列表
        """
        all_signals = []
        
        for pattern_type, signals in results.items():
            for signal in signals:
                if signal.confidence >= min_confidence:
                    all_signals.append(signal)
        
        # 按置信度降序排序
        all_signals.sort(key=lambda x: x.confidence, reverse=True)
        
        return all_signals[:top_n]
    
    def filter_by_sector(self, signals: List[PatternSignal], 
                         hot_sectors: List[str]) -> List[PatternSignal]:
        """
        按热点板块过滤信号
        
        Args:
            signals: 信号列表
            hot_sectors: 热点板块列表
            
        Returns:
            List[PatternSignal]: 属于热点板块的信号
        """
        filtered = []
        for signal in signals:
            if signal.l2_industry and signal.l2_industry in hot_sectors:
                filtered.append(signal)
        return filtered

    def _calculate_weak_to_strong_confidence_v2(self, weakening, auction: Dict, 
                                                 time_score: int) -> float:
        """
        计算弱转强置信度（V2版本 - 基于龙头池）
        
        Args:
            weakening: WeakeningDragon对象
            auction: 竞价分析数据
            time_score: 涨停时间得分
            
        Returns:
            float: 置信度 0-1
        """
        confidence = 0.65
        
        # 基于走弱类型调整基础分
        if weakening.weakening_type == "断板":
            confidence += 0.05  # 断板后转强更有力
        elif "烂板" in weakening.weakening_type:
            confidence += 0.03
        
        # 基于回调幅度调整（回调适中更好）
        if 0.03 <= weakening.max_drawdown <= 0.10:
            confidence += 0.05
        
        # 高开幅度
        gap = auction.get('gap', 0)
        if gap >= 0.05:
            confidence += 0.10
        elif gap >= 0.03:
            confidence += 0.05
        
        # 竞价量
        vol_ratio = auction.get('auction_vol_ratio', 0)
        if vol_ratio >= 0.15:
            confidence += 0.10
        elif vol_ratio >= 0.10:
            confidence += 0.05
        
        # 涨停时间
        if time_score >= 80:
            confidence += 0.10
        elif time_score >= 60:
            confidence += 0.05
        
        # 龙头类型加成
        if weakening.dragon_type.value == "连板龙头":
            if weakening.peak_board_height >= 5:
                confidence += 0.05
        
        return min(confidence, 0.95)
