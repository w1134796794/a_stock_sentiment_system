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
    is_dual_resonance: bool = False  # 是否双热点共振

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
        self.divergence_to_consensus = None
        self.position_battle = None
        self.blast_reseal = None
        
        try:
            from core.pattern.dragon_second_wave import DragonSecondWaveStrategyV2
            self.dragon_second_wave = DragonSecondWaveStrategyV2(self.dm, self.se)
            logger.info("✓ 龙二波策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 龙二波策略加载失败: {e}")
            self.dragon_second_wave = None
    
    # ==================== 模式识别接口 ====================
    
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                              day_before_yesterday_df: pd.DataFrame = None,
                              today_date: str = None, yest_date: str = None,
                              history_pools: Dict[str, pd.DataFrame] = None,
                              today_daily: pd.DataFrame = None,
                              stock_to_ths_industry: Dict[str, str] = None,
                              stock_to_ths_concept: Dict[str, str] = None,
                              all_hot_member_codes: set = None,
                              stock_to_hot_sectors: Dict[str, list] = None) -> List[PatternSignal]:
        """
        弱转强模式识别 - 动态龙头跟踪版本
        
        Args:
            today_df: 今日涨停池数据
            yesterday_df: 昨日涨停池数据
            day_before_yesterday_df: 前日涨停池数据
            today_date: 今日日期 (YYYYMMDD)
            yest_date: 昨日日期 (YYYYMMDD)，用于获取准确的昨日收盘价
            history_pools: 历史涨停池数据（用于趋势龙头识别）
            today_daily: 今日全市场日线数据（用于更新走弱池价格）
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
                date_str=today_date or datetime.now().strftime("%Y%m%d"),
                today_daily=today_daily,
                stock_to_ths_industry=stock_to_ths_industry or {},
                stock_to_ths_concept=stock_to_ths_concept or {}
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
            
            # 筛选符合条件的走弱池股票（排除当天入池和不符合条件的）
            valid_weakening_codes = self._filter_weakening_pool_for_detection(
                today_date or datetime.now().strftime("%Y%m%d"), today_daily
            )
            logger.info(f"[弱转强] 走弱池筛选: 共{len(self.weak_to_strong.weakening_pool)}只，历史走弱纳入观察{len(valid_weakening_codes)}只")
            
            if not valid_weakening_codes:
                logger.info("[弱转强] 无历史走弱股票需要检测，跳过阶段2")
                return signals
            
            # 构建今日涨停池代码集合（用于快速查找）
            today_zt_codes = set()
            today_zt_dict = {}  # code -> row
            for _, row in today_df.iterrows():
                code = str(row.get('代码', '')).zfill(6)
                today_zt_codes.add(code)
                today_zt_dict[code] = row
            
            logger.info(f"[弱转强] 今日涨停池共{len(today_zt_codes)}只")
            
            total_checked = 0
            total_passed = 0

            # 遍历走弱池中的股票，检查是否今日转强（大涨或涨停）
            for code in valid_weakening_codes:
                weakening = self.weak_to_strong.weakening_pool[code]
                name = weakening.stock_name
                total_checked += 1
                
                logger.info(f"[弱转强] 检测 {name}({code})，入池日期={weakening.weakening_date}...")
                
                # 检查今日是否转强（涨停或大涨>=7%）
                today_row = None
                is_limit_up = False
                price_change = 0
                
                # 1. 先检查是否在涨停池中
                if code in today_zt_codes:
                    today_row = today_zt_dict[code]
                    is_limit_up = True
                    price_change = 9.9  # 涨停默认9.9%+
                    logger.info(f"[弱转强]   {name} 今日涨停！开始分析转强信号...")
                # 2. 再检查日线数据中的涨幅
                elif today_daily is not None and not today_daily.empty:
                    stock_daily = today_daily[today_daily['ts_code'].str.contains(code)]
                    if not stock_daily.empty:
                        latest = stock_daily.iloc[-1]
                        price_change = latest.get('pct_chg', 0)
                        # 大涨>=7%也算转强
                        if price_change >= 7.0:
                            today_row = latest
                            logger.info(f"[弱转强]   {name} 今日大涨{price_change:.1f}%！开始分析转强信号...")
                
                # 未转强的跳过
                if today_row is None:
                    logger.debug(f"[弱转强]   {name} 今日未大涨/涨停（涨幅{price_change:.1f}%），未转强，跳过")
                    continue

                # 获取昨日数据（用于计算高开）
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

                # ========== 多维度转强信号检测 ==========
                
                # 1. 竞价量价维度评分（30分）
                gap = auction_analysis.get('gap', 0)
                auction_vol_ratio = auction_analysis.get('auction_vol_ratio', 0)
                auction_amount = auction_analysis.get('auction_amount', 0)
                
                auction_score = 0
                if gap >= 0.05:  # 高开>=5%
                    auction_score += 20
                elif gap >= 0.03:  # 高开>=3%
                    auction_score += 15
                
                if auction_vol_ratio >= 0.15:  # 竞价量比>=15%
                    auction_score += 10
                elif auction_vol_ratio >= 0.10:  # 竞价量比>=10%
                    auction_score += 5
                
                if auction_amount >= 10_000_000:  # 竞价金额>=1000万
                    auction_score += 5
                
                logger.debug(f"[弱转强]   {name} 竞价维度: 高开{gap*100:.1f}%, 量比{auction_vol_ratio*100:.1f}%, 金额{auction_amount/10000:.0f}万, 得分{auction_score}/30")
                
                # 2. 技术形态维度评分（25分）
                technical_score = self._calculate_technical_score(code, today_date, today_daily)
                logger.debug(f"[弱转强]   {name} 技术维度得分{technical_score}/25")
                
                # 3. 资金流入维度评分（25分）
                capital_score = self._calculate_capital_score(code, today_date)
                logger.debug(f"[弱转强]   {name} 资金维度得分{capital_score}/25")
                
                # 4. 市场情绪维度评分（20分）
                # 统一获取行业名称（优先THS行业，其次THS概念，最后回退东财行业）
                sector_name = (stock_to_ths_industry or {}).get(code, '') or (stock_to_ths_concept or {}).get(code, '') or today_row.get('所属行业', '') or today_row.get('industry', '')
                sector_score = self._calculate_sector_score(
                    sector_name, today_date, today_df,
                    stock_to_ths_industry=stock_to_ths_industry,
                    stock_to_ths_concept=stock_to_ths_concept
                )
                logger.debug(f"[弱转强]   {name} 情绪维度得分{sector_score}/20")
                
                # 计算总评分
                total_score = auction_score + technical_score + capital_score + sector_score
                logger.info(f"[弱转强]   {name} 总评分={total_score} (竞价{auction_score}+技术{technical_score}+资金{capital_score}+情绪{sector_score})")
                
                # 信号触发阈值判断
                if total_score < 60:
                    logger.debug(f"[弱转强]   {name} 过滤: 总评分{total_score} < 60（基础信号阈值）")
                    continue
                
                # 确定信号级别和置信度
                if total_score >= 85:
                    signal_level = "强烈信号"
                    confidence = 0.90
                elif total_score >= 75:
                    signal_level = "确认信号"
                    confidence = 0.75
                else:
                    signal_level = "基础信号"
                    confidence = 0.60
                
                logger.debug(f"[弱转强]   {name} 信号级别={signal_level}, 置信度={confidence:.2f}")
                
                # 涨停时间加分（只有涨停的股票才有封板时间）
                if is_limit_up:
                    limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                    time_score = self._calculate_time_score(limit_up_time)
                else:
                    limit_up_time = "N/A（大涨未涨停）"
                    time_score = 50  # 大涨未涨停给中等分数
                logger.debug(f"[弱转强]   {name} 涨停时间={limit_up_time}, 时间分={time_score}")

                # 从weakening对象获取龙头信息
                peak_height = weakening.peak_board_height if weakening.dragon_type.value == "连板龙头" else 0
                weakening_type = weakening.weakening_type
                
                # 确定仓位建议
                if total_score >= 85:
                    position_size = "heavy"  # 强烈信号重仓
                elif total_score >= 75:
                    position_size = "medium"  # 确认信号中等仓位
                else:
                    position_size = "light"  # 基础信号轻仓试错
                
                signal = PatternSignal(
                    pattern_type="弱转强",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"{weakening.dragon_type.value}{peak_height}板后{weakening_type}转强，{signal_level}，总评分{total_score}分",
                    key_metrics={
                        "龙头类型": weakening.dragon_type.value,
                        "最高连板": peak_height,
                        "走弱类型": weakening_type,
                        "走弱日期": weakening.weakening_date,
                        "信号级别": signal_level,
                        "总评分": total_score,
                        "竞价评分": auction_score,
                        "技术评分": technical_score,
                        "资金评分": capital_score,
                        "情绪评分": sector_score,
                        "次日高开": f"{auction_analysis.get('gap', 0)*100:.1f}%",
                        "竞价量比": f"{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%",
                        "涨停时间": limit_up_time,
                        "回调幅度": f"{weakening.max_drawdown*100:.1f}%",
                    },
                    # 统一获取价格（涨停池用'涨停价'，日线数据用'close'）
                    entry_price=today_row.get('涨停价', 0) or today_row.get('close', 0),
                    stop_loss=(today_row.get('涨停价', 0) or today_row.get('close', 0)) * 0.93,
                    take_profit=(today_row.get('涨停价', 0) or today_row.get('close', 0)) * 1.15,
                    position_size=position_size,
                    validation_rules=[
                        f"{weakening.dragon_type.value}{peak_height}板（身份）",
                        f"{weakening_type}后走弱（弱）",
                        f"总评分{total_score}分（{signal_level}）",
                        f"竞价{auction_score}分/30分（高开{auction_analysis.get('gap', 0)*100:.1f}%）",
                        f"技术{technical_score}分/25分",
                        f"资金{capital_score}分/25分",
                        f"情绪{sector_score}分/20分",
                        "开盘不回踩，快速上板（确认强）"
                    ],
                    l2_industry=sector_name
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
                                   today_date: str = None, yest_date: str = None,
                                   stock_to_ths_industry: Dict[str, str] = None,
                                   stock_to_ths_concept: Dict[str, str] = None,
                                   all_hot_member_codes: set = None,
                                   stock_to_hot_sectors: Dict[str, list] = None) -> List[PatternSignal]:
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

            # 预过滤：只保留连板数=2的股票
            board_col = '连板数' if '连板数' in today_df.columns else 'limit_times'
            if board_col not in today_df.columns:
                logger.debug("[二板定龙] 涨停池缺少连板数列")
                return signals

            second_board_df = today_df[today_df[board_col].astype(float) == 2.0]
            skipped = len(today_df) - len(second_board_df)
            logger.debug(f"[二板定龙] 今日涨停{len(today_df)}只，连板数=2的{len(second_board_df)}只，跳过{skipped}只")

            for _, today_row in second_board_df.iterrows():
                code = str(today_row.get('代码', '')).zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

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
                    l2_industry=(stock_to_ths_industry or {}).get(code, '') or (stock_to_ths_concept or {}).get(code, '') or today_row.get('所属行业', '')
                )
                
                signals.append(signal)
                total_passed += 1
                logger.debug(f"[二板定龙]   {name} 生成信号 (置信度{confidence:.2f})")

            logger.info(f"[二板定龙] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[二板定龙] 检测失败: {e}", exc_info=True)

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
            # 过滤：只保留首板股票（连板数=1）
            first_board_df = today_df[today_df['连板数'] == 1].copy()
            logger.info(f"[首板突破] 今日涨停{len(today_df)}只，首板{len(first_board_df)}只")

            if first_board_df.empty:
                logger.info("[首板突破] 没有首板股票，跳过检测")
                return signals

            # history_pools的键已经是YYYYMMDD格式，只需排除今日数据（避免重复统计）
            history_pools_filtered = {}
            if history_pools:
                for date_key, pool_df in history_pools.items():
                    # 排除今日数据，因为detect_first_board_by_sectors内部会单独处理今日数据
                    if date_key != date_str:
                        history_pools_filtered[date_key] = pool_df
            logger.debug(f"[首板突破] 过滤后历史池: {len(history_pools_filtered)}天 (排除今日)")
            trade_signals = self.first_board_breakout.detect_first_board_by_sectors(
                first_board_df, history_pools_filtered, date_str, hot_sectors=hot_sectors
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
                    l2_industry=getattr(ts, 'l2_industry', ''),
                    is_dual_resonance=getattr(ts, 'is_dual_resonance', False)
                )
                signals.append(pattern_signal)

            logger.info(f"[首板突破] 检测完成: 共{len(signals)}个信号")

        except Exception as e:
            logger.error(f"[首板突破] 检测失败: {e}", exc_info=True)

        return signals

    def detect_dragon_second_wave(self, today_df: pd.DataFrame,
                                  recent_zt_pools: Dict[str, pd.DataFrame],
                                  today_date: str,
                                  stock_to_ths_industry: Dict[str, str] = None,
                                  stock_to_ths_concept: Dict[str, str] = None,
                                  all_hot_member_codes: set = None,
                                  stock_to_hot_sectors: Dict[str, list] = None) -> List[PatternSignal]:
        """
        龙二波模式识别 - 调用DragonSecondWaveStrategyV2

        Args:
            all_hot_member_codes: 所有热点板块成分股代码集合（用于快速判定板块热度）
            stock_to_hot_sectors: 股票→所属热点板块名称列表（用于日志输出）
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
                code = str(today_row.get('代码', '')).split('.')[0].zfill(6)
                name = today_row.get('名称', '')
                total_checked += 1

                sector_hot = False
                stock_code_clean = code
                stock_sector = ''

                if all_hot_member_codes:
                    sector_hot = stock_code_clean in all_hot_member_codes

                if sector_hot:
                    hot_sector_list = (stock_to_hot_sectors or {}).get(stock_code_clean, [])
                    stock_sector = hot_sector_list[0] if hot_sector_list else ''
                    logger.debug(f"[龙二波]   {name}({code}) 命中热点: {', '.join(hot_sector_list[:5])}")
                else:
                    ths_industry = (stock_to_ths_industry or {}).get(stock_code_clean, '')
                    ths_concept = (stock_to_ths_concept or {}).get(stock_code_clean, '')
                    stock_sector = ths_industry or ths_concept or str(today_row.get('所属行业', ''))
                    logger.debug(f"[龙二波]   {name}({code}) 板块热度=False, 所属行业={stock_sector}")

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
        获取前N个交易日 - 使用交易日历

        Args:
            date_str: 日期字符串 YYYYMMDD
            days: 往前推几个交易日

        Returns:
            str: 前N个交易日的日期 YYYYMMDD
        """
        from core.utils.date_utils import get_last_n_trade_dates

        trade_dates = get_last_n_trade_dates(days + 1, date_str)
        if len(trade_dates) > days:
            return trade_dates[days]
        return trade_dates[-1] if trade_dates else date_str

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
        
        # 获取最近15个交易日涨停池数据（用于龙二波检测，基于交易日历）
        logger.info("获取最近15个交易日涨停池数据...")
        from core.utils.date_utils import get_last_n_trade_dates
        recent_trade_dates = get_last_n_trade_dates(16, today_date_ymd)
        recent_trade_dates = [d for d in recent_trade_dates if d != today_date_ymd][:15]

        recent_zt_pools = {}
        for i, date_str_ymd in enumerate(recent_trade_dates, 1):
            df = self.dm.get_limit_up_pool(date_str_ymd)
            if not df.empty:
                recent_zt_pools[date_str_ymd] = df
                logger.debug(f"  [{i}] {date_str_ymd}: {len(df)}只涨停")
            else:
                logger.debug(f"  [{i}] {date_str_ymd}: 无数据或空数据")
        
        logger.info(f"今日涨停: {len(today_df)}只, 昨日涨停: {len(yesterday_df)}只, 前日涨停: {len(day_before_yesterday_df)}只, 历史池: {len(recent_zt_pools)}天")
        
        # 获取今日全市场日线数据（用于更新走弱池价格）
        logger.info("获取今日全市场日线数据...")
        try:
            today_daily = self.dm.get_daily_basic(today_date_ymd)
            logger.info(f"获取到全市场日线数据: {len(today_daily)}只股票")
        except Exception as e:
            logger.warning(f"获取全市场日线数据失败: {e}")
            today_daily = None
        
        results = {}
        
        # 构建 stock→THS行业/概念 反向映射（统一替换东财行业数据）
        # 同时构建 all_hot_member_codes（所有热点板块成分股集合）
        # 和 stock_to_hot_sectors（股票→所属热点板块列表）
        stock_to_ths_industry = {}    # stock_code → THS行业名称
        stock_to_ths_concept = {}     # stock_code → THS概念名称
        all_hot_member_codes = set()  # 所有热点板块成分股代码集合
        stock_to_hot_sectors = {}     # stock_code → [热点板块名称列表]
        
        if hot_sectors:
            for hs in hot_sectors:
                sector_type = hs.get('sector_type', '')
                sector_name = hs.get('sector_name', '')
                member_codes = hs.get('member_codes', set())
                
                if not member_codes:
                    continue
                
                all_hot_member_codes.update(member_codes)
                
                for code in member_codes:
                    if code not in stock_to_hot_sectors:
                        stock_to_hot_sectors[code] = []
                    stock_to_hot_sectors[code].append(sector_name)
                
                if sector_type == '行业':
                    for code in member_codes:
                        if code not in stock_to_ths_industry:
                            stock_to_ths_industry[code] = sector_name
                elif sector_type == '概念':
                    for code in member_codes:
                        if code not in stock_to_ths_concept:
                            stock_to_ths_concept[code] = sector_name
            
            logger.info(f"[模式识别] THS行业映射: {len(stock_to_ths_industry)}只, "
                       f"THS概念映射: {len(stock_to_ths_concept)}只, "
                       f"热点成分股总计: {len(all_hot_member_codes)}只, "
                       f"有热点归属的股票: {len(stock_to_hot_sectors)}只")
        else:
            logger.warning("[模式识别] 未收到热点板块数据，将回退使用东财行业")

        # 1. 弱转强检测
        try:
            logger.info("-" * 40)
            results["弱转强"] = self.detect_weak_to_strong(
                today_df, yesterday_df, day_before_yesterday_df, today_date_ymd, yesterday_date_ymd,
                history_pools=recent_zt_pools,
                today_daily=today_daily,
                stock_to_ths_industry=stock_to_ths_industry,
                stock_to_ths_concept=stock_to_ths_concept,
                all_hot_member_codes=all_hot_member_codes,
                stock_to_hot_sectors=stock_to_hot_sectors
            )
        except Exception as e:
            logger.error(f"弱转强检测失败: {e}")
            results["弱转强"] = []
        
        
        # 3. 二板定龙检测
        try:
            logger.info("-" * 40)
            results["二板定龙"] = self.detect_second_board_dragon(
                today_df, yesterday_df, day_before_yesterday_df,
                today_date_ymd, yesterday_date_ymd,
                stock_to_ths_industry=stock_to_ths_industry,
                stock_to_ths_concept=stock_to_ths_concept,
                all_hot_member_codes=all_hot_member_codes,
                stock_to_hot_sectors=stock_to_hot_sectors
            )
        except Exception as e:
            logger.error(f"二板定龙检测失败: {e}")
            results["二板定龙"] = []
        
        # 4. 龙二波检测
        try:
            logger.info("-" * 40)
            results["龙二波"] = self.detect_dragon_second_wave(
                today_df, recent_zt_pools, today_date_ymd,
                stock_to_ths_industry=stock_to_ths_industry,
                stock_to_ths_concept=stock_to_ths_concept,
                all_hot_member_codes=all_hot_member_codes,
                stock_to_hot_sectors=stock_to_hot_sectors
            )
        except Exception as e:
            logger.error(f"龙二波检测失败: {e}")
            results["龙二波"] = []
        
        # 5. 首板突破检测
        try:
            logger.info("-" * 40)
            results["首板突破"] = self.detect_first_board_breakout(
                today_df, yesterday_df, today_date_ymd, recent_zt_pools,
                hot_sectors=hot_sectors
            )
        except Exception as e:
            logger.error(f"首板突破检测失败: {e}")
            results["首板突破"] = []
        
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
    
    def _filter_weakening_pool_for_detection(self, date_str: str, today_daily: pd.DataFrame = None) -> List[str]:
        """
        筛选符合检测条件的走弱池股票
        
        核心逻辑：
        - 入池日期早于当天的股票（历史走弱）才纳入转强观察池
        - 入池日期为当天的股票（当天刚走弱）不检测转强
        
        排除条件：
        1. 当天入池的股票（刚走弱，需观察）
        2. 观察期已过期（超过5天）
        3. 回调幅度过大（>25%，A杀风险）或过小（<5%，刚走弱）
        4. 成交量连续萎缩（<5日均量50%）
        
        Returns:
            符合条件的股票代码列表
        """
        valid_codes = []
        current_date = datetime.strptime(date_str, "%Y%m%d")
        
        for code, weakening in self.weak_to_strong.weakening_pool.items():
            # 1. 检查入池日期 - 当天入池的不检测
            try:
                entry_date_str = weakening.weakening_date
                entry_date = datetime.strptime(entry_date_str, "%Y%m%d")
                
                # 当天入池的股票跳过（刚走弱，不检测转强）
                if entry_date_str == date_str:
                    logger.debug(f"[走弱池筛选] {weakening.stock_name} 当天入池({entry_date_str})，不检测转强")
                    continue
                
                # 计算观察天数（从入池日到当前日）
                observation_days = (current_date - entry_date).days
                
                # 观察期超过5天的排除
                if observation_days > 5:
                    logger.debug(f"[走弱池筛选] {weakening.stock_name} 观察期过期({observation_days}天 > 5天)")
                    continue
                    
                logger.debug(f"[走弱池筛选] {weakening.stock_name} 入池{observation_days}天，纳入观察")
            except Exception as e:
                logger.debug(f"[走弱池筛选] {weakening.stock_name} 日期解析失败: {e}")
                continue
            
            # 2. 检查回调幅度
            drawdown = weakening.max_drawdown
            if drawdown > 0.25:
                logger.debug(f"[走弱池筛选] {weakening.stock_name} 回调过大({drawdown*100:.1f}% > 25%)")
                continue
            if drawdown < 0.05:
                logger.debug(f"[走弱池筛选] {weakening.stock_name} 回调过小({drawdown*100:.1f}% < 5%)")
                continue
            
            # 3. 检查成交量（如果提供了日线数据）
            if today_daily is not None and not today_daily.empty:
                stock_daily = today_daily[today_daily['ts_code'].str.contains(code)]
                if not stock_daily.empty and len(stock_daily) >= 5:
                    avg_volume_5d = stock_daily['vol'].tail(5).mean()
                    today_volume = stock_daily.iloc[-1]['vol']
                    if today_volume < avg_volume_5d * 0.5:
                        logger.debug(f"[走弱池筛选] {weakening.stock_name} 成交量萎缩(<50%)")
                        continue
            
            valid_codes.append(code)
            logger.info(f"[走弱池筛选] ✓ {weakening.stock_name}({code}) 符合条件，入池{observation_days}天，回调{drawdown*100:.1f}%")
        
        logger.info(f"[走弱池筛选] 共{len(self.weak_to_strong.weakening_pool)}只，历史走弱{len(valid_codes)}只纳入观察")
        return valid_codes
    
    def _calculate_technical_score(self, stock_code: str, date_str: str, today_daily: pd.DataFrame = None) -> int:
        """
        计算技术形态维度评分（满分25分）
        
        评分标准：
        - 收盘价站上5日均线: +10分
        - 5日均线拐头向上: +5分
        - 成交量突破5日均量1.5倍: +5分
        - 阳线吞没形态: +5分
        """
        score = 0
        
        try:
            # 获取日线数据
            if today_daily is not None and not today_daily.empty:
                stock_data = today_daily[today_daily['ts_code'].str.contains(stock_code)]
                if not stock_data.empty and len(stock_data) >= 5:
                    df = stock_data.copy()
                    df['ma5'] = df['close'].rolling(window=5).mean()
                    
                    latest = df.iloc[-1]
                    prev = df.iloc[-2] if len(df) >= 2 else latest
                    
                    # 收盘价站上5日均线
                    if latest['close'] > latest['ma5']:
                        score += 10
                    
                    # 5日均线拐头向上
                    if latest['ma5'] > prev['ma5']:
                        score += 5
                    
                    # 成交量突破
                    avg_volume_5d = df['vol'].tail(5).mean()
                    if latest['vol'] > avg_volume_5d * 1.5:
                        score += 5
                    
                    # 阳线吞没形态
                    today_body = latest['close'] - latest['open']
                    prev_body = abs(prev['close'] - prev['open'])
                    if today_body > 0 and prev_body > 0 and today_body > prev_body * 1.5:
                        score += 5
        except Exception as e:
            logger.debug(f"[技术评分] {stock_code} 计算失败: {e}")
        
        return score
    
    def _calculate_capital_score(self, stock_code: str, date_str: str) -> int:
        """
        计算资金流入维度评分（满分25分）
        
        评分标准：
        - 主力净流入为正: +10分
        - 大单净流入占比>=20%: +10分
        - 资金连续流入: +5分
        """
        score = 0
        
        try:
            # 获取资金流向数据
            if self.dm and hasattr(self.dm, 'get_moneyflow_data'):
                moneyflow = self.dm.get_moneyflow_data(stock_code, date_str)
                if moneyflow:
                    # 主力净流入为正
                    if moneyflow.get('main_inflow', 0) > 0:
                        score += 10
                    
                    # 大单占比
                    big_order_ratio = moneyflow.get('big_order_ratio', 0)
                    if big_order_ratio >= 0.20:
                        score += 10
                    
                    # 持续流入
                    if moneyflow.get('inflow_days', 0) >= 2:
                        score += 5
        except Exception as e:
            logger.debug(f"[资金评分] {stock_code} 计算失败: {e}")
        
        return score
    
    def _calculate_sector_score(self, sector_name: str, date_str: str, today_zt: pd.DataFrame = None,
                               stock_to_ths_industry: Dict[str, str] = None,
                               stock_to_ths_concept: Dict[str, str] = None) -> int:
        """
        计算市场情绪维度评分（满分20分）
        
        评分标准：
        - 所属板块涨幅>=2%: +10分
        - 板块内涨停家数>=3家: +10分
        """
        score = 0
        
        try:
            # 从涨停池统计板块涨停家数（优先用THS映射，回退东财行业列）
            if today_zt is not None and not today_zt.empty and sector_name:
                sector_limit_up_count = 0
                ths_industry = stock_to_ths_industry or {}
                ths_concept = stock_to_ths_concept or {}
                
                for _, zt_row in today_zt.iterrows():
                    zt_code = str(zt_row.get('代码', '')).zfill(6)
                    zt_ths_ind = ths_industry.get(zt_code, '')
                    zt_ths_con = ths_concept.get(zt_code, '')
                    if zt_ths_ind == sector_name or zt_ths_con == sector_name:
                        sector_limit_up_count += 1
                
                # 如果THS映射没匹配到，回退用东财行业列
                if sector_limit_up_count == 0 and '所属行业' in today_zt.columns:
                    sector_zt = today_zt[today_zt['所属行业'] == sector_name]
                    sector_limit_up_count = len(sector_zt)
                
                if sector_limit_up_count >= 3:
                    score += 10
                elif sector_limit_up_count >= 1:
                    score += 5
            
            # 获取板块涨幅（如果有板块数据）
            if self.dm and hasattr(self.dm, 'get_sector_daily'):
                sector_data = self.dm.get_sector_daily(sector_name, date_str)
                if sector_data:
                    sector_change = sector_data.get('change_pct', 0)
                    if sector_change >= 2.0:
                        score += 10
                    elif sector_change >= 1.0:
                        score += 5
        except Exception as e:
            logger.debug(f"[情绪评分] {sector_name} 计算失败: {e}")
        
        return score
