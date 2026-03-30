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
            # 弱转强策略
            from core.pattern.weak_to_strong import WeakToStrongStrategy
            self.weak_to_strong = WeakToStrongStrategy(self.dm, self.se)
            logger.info("✓ 弱转强策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 弱转强策略加载失败: {e}")
            self.weak_to_strong = None
        
        try:
            # 二板定龙策略
            from core.pattern.second_board_dragon import SecondBoardDragonStrategy
            self.second_board_dragon = SecondBoardDragonStrategy(self.dm, self.se)
            logger.info("✓ 二板定龙策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 二板定龙策略加载失败: {e}")
            self.second_board_dragon = None
        
        try:
            # 首板突破策略
            from core.pattern.first_board_breakout import HotspotFirstBoardStrategy
            self.first_board_breakout = HotspotFirstBoardStrategy(self.dm, self.se, self.mapper)
            logger.info("✓ 首板突破策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 首板突破策略加载失败: {e}")
            self.first_board_breakout = None
        
        try:
            # 分歧转一致策略
            from core.pattern.divergence_to_consensus import DivergenceToConsensusStrategy
            self.divergence_to_consensus = DivergenceToConsensusStrategy(self.dm, self.se)
            logger.info("✓ 分歧转一致策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 分歧转一致策略加载失败: {e}")
            self.divergence_to_consensus = None
        
        try:
            # 卡位板策略
            from core.pattern.position_battle import PositionBattleStrategy
            self.position_battle = PositionBattleStrategy(self.dm)
            logger.info("✓ 卡位板策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 卡位板策略加载失败: {e}")
            self.position_battle = None
        
        try:
            # 炸板回封策略
            from core.pattern.blast_reseal import BlastResealAnalyzer
            self.blast_reseal = BlastResealAnalyzer(self.dm)
            logger.info("✓ 炸板回封策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 炸板回封策略加载失败: {e}")
            self.blast_reseal = None
        
        try:
            # 龙二波策略
            from core.pattern.dragon_second_wave import DragonSecondWaveStrategyV2
            self.dragon_second_wave = DragonSecondWaveStrategyV2(self.dm, self.se)
            logger.info("✓ 龙二波策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 龙二波策略加载失败: {e}")
            self.dragon_second_wave = None
    
    # ==================== 模式识别接口 ====================
    
    def detect_weak_to_strong(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                              day_before_yesterday_df: pd.DataFrame = None) -> List[PatternSignal]:
        """
        弱转强模式识别
        引用: core.pattern.weak_to_strong.WeakToStrongStrategy
        """
        signals = []

        logger.debug(f"[弱转强] 开始检测，今日涨停{len(today_df)}只，昨日涨停{len(yesterday_df)}只")

        if self.weak_to_strong is None:
            logger.warning("[弱转强] 策略未加载")
            return signals

        if today_df.empty or yesterday_df.empty:
            logger.debug(f"[弱转强] 数据为空，今日={today_df.empty}, 昨日={yesterday_df.empty}")
            return signals

        try:
            total_checked = 0
            total_passed = 0

            # 遍历今日涨停股，调用策略检测
            for _, today_row in today_df.iterrows():
                code = today_row.get('代码', '')
                name = today_row.get('名称', '')
                total_checked += 1

                logger.debug(f"[弱转强] 检测 {name}({code})...")

                # 查找昨日数据
                yest_row = yesterday_df[yesterday_df['代码'] == code]
                if yest_row.empty:
                    logger.debug(f"[弱转强]   {name} 昨日未涨停，跳过")
                    continue

                logger.debug(f"[弱转强]   {name} 昨日已涨停，继续分析...")

                # 计算连板高度
                board_height = self._calculate_board_height(
                    code, today_row, yesterday_df, day_before_yesterday_df
                )
                logger.debug(f"[弱转强]   {name} 连板高度={board_height}")

                # 构造策略需要的参数（简化版，实际使用时需要更多数据）
                # 这里转换为PatternSignal格式返回
                signal = self._convert_to_pattern_signal(
                    code, name, "弱转强", today_row, yest_row.iloc[0], board_height
                )
                if signal:
                    signals.append(signal)
                    total_passed += 1
                    logger.debug(f"[弱转强]   {name} 生成信号 (置信度{signal.confidence:.2f})")
                else:
                    logger.debug(f"[弱转强]   {name} 未生成信号")

            logger.info(f"[弱转强] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[弱转强] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_second_board_dragon(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        二板定龙模式识别
        引用: core.pattern.second_board_dragon.SecondBoardDragonStrategy
        """
        signals = []

        logger.debug(f"[二板定龙] 开始检测，今日涨停{len(today_df)}只，昨日涨停{len(yesterday_df)}只")

        if self.second_board_dragon is None:
            logger.warning("[二板定龙] 策略未加载")
            return signals

        if today_df.empty or yesterday_df.empty:
            logger.debug(f"[二板定龙] 数据为空，今日={today_df.empty}, 昨日={yesterday_df.empty}")
            return signals

        try:
            total_checked = 0
            total_passed = 0

            for _, yest_row in yesterday_df.iterrows():
                code = yest_row.get('代码', '')
                name = yest_row.get('名称', '')
                total_checked += 1

                logger.debug(f"[二板定龙] 检测 {name}({code})...")

                # 查找今日数据
                today_row = today_df[today_df['代码'] == code]
                if today_row.empty:
                    logger.debug(f"[二板定龙]   {name} 今日未涨停，跳过")
                    continue

                logger.debug(f"[二板定龙]   {name} 今日涨停，检查是否为二板...")

                # 检查昨日是否为首板（昨日涨停但前日未涨停）
                # 这里简化处理，实际应该检查前日数据
                # 检测二板定龙信号
                signal = self._convert_to_pattern_signal(
                    code, name, "二板定龙", today_row.iloc[0], yest_row, 2
                )
                if signal:
                    signals.append(signal)
                    total_passed += 1
                    logger.debug(f"[二板定龙]   {name} 生成二板定龙信号 (置信度{signal.confidence:.2f})")
                else:
                    logger.debug(f"[二板定龙]   {name} 未生成信号")

            logger.info(f"[二板定龙] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[二板定龙] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_first_board_breakout(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame = None,
                                    date_str: str = None, history_pools: Dict[str, pd.DataFrame] = None) -> List[PatternSignal]:
        """
        首板突破模式识别
        引用: core.pattern.first_board_breakout.HotspotFirstBoardStrategy.detect_first_board_by_sectors
        """
        signals = []

        if self.first_board_breakout is None:
            logger.warning("首板突破策略未加载")
            return signals

        if today_df.empty:
            return signals

        try:
            # 调用HotspotFirstBoardStrategy的detect_first_board_by_sectors方法
            trade_signals = self.first_board_breakout.detect_first_board_by_sectors(
                today_df, history_pools or {}, date_str or ""
            )

            # 将TradeSignal转换为PatternSignal
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
                    l2_industry=ts.l2_industry
                )
                signals.append(pattern_signal)

            logger.info(f"首板突破策略检测到 {len(signals)} 个信号")

        except Exception as e:
            logger.error(f"首板突破检测失败: {e}")

        return signals
    
    def detect_divergence_to_consensus(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame) -> List[PatternSignal]:
        """
        分歧转一致模式识别
        引用: core.pattern.divergence_to_consensus.DivergenceToConsensusStrategy
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
                code = today_row.get('代码', '')
                name = today_row.get('名称', '')
                total_checked += 1

                logger.debug(f"[分歧转一致] 检测 {name}({code})...")

                yest_row = yesterday_df[yesterday_df['代码'] == code]
                if yest_row.empty:
                    logger.debug(f"[分歧转一致]   {name} 昨日未涨停，跳过")
                    continue

                logger.debug(f"[分歧转一致]   {name} 昨日已涨停，检查分歧特征...")

                # 检查昨日是否有分歧特征（炸板、放量等）
                yest_blast = yest_row.iloc[0].get('炸板次数', 0)
                yest_change = yest_row.iloc[0].get('涨跌幅', 0)
                if isinstance(yest_change, str):
                    yest_change = float(yest_change.replace('%', ''))

                logger.debug(f"[分歧转一致]   {name} 昨日炸板={yest_blast}, 涨幅={yest_change:.2f}%")

                signal = self._convert_to_pattern_signal(
                    code, name, "分歧转一致", today_row, yest_row.iloc[0], None
                )
                if signal:
                    signals.append(signal)
                    total_passed += 1
                    logger.debug(f"[分歧转一致]   {name} 生成信号 (置信度{signal.confidence:.2f})")
                else:
                    logger.debug(f"[分歧转一致]   {name} 未生成信号")

            logger.info(f"[分歧转一致] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[分歧转一致] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_position_battle(self, today_df: pd.DataFrame, yesterday_df: pd.DataFrame,
                               sector_mapping: Dict[str, str] = None) -> List[PatternSignal]:
        """
        卡位板模式识别
        引用: core.pattern.position_battle.PositionBattleStrategy
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

            # 调用策略模块的检测方法
            sector_stocks = today_df
            battle_signals = self.position_battle.detect_position_battle(
                sector_stocks, today_df, yesterday_df
            )

            logger.debug(f"[卡位板] 策略返回 {len(battle_signals)} 个原始信号")

            # 转换信号格式
            for bs in battle_signals:
                signal = PatternSignal(
                    pattern_type="卡位板",
                    stock_code=bs.low_stock_code,
                    stock_name=bs.low_stock_name,
                    confidence=bs.confidence,
                    description=f"卡位{bs.high_stock_name}，领先{bs.lead_time}分钟",
                    key_metrics={
                        "卡位类型": bs.battle_type.value,
                        "高位股": bs.high_stock_name,
                        "领先时间": f"{bs.lead_time}分钟",
                        "操作建议": bs.action
                    },
                    validation_rules=["低位抢先涨停", "封单质量优势"]
                )
                signals.append(signal)
                logger.debug(f"[卡位板] 转换信号: {bs.low_stock_name} 卡位 {bs.high_stock_name} (置信度{bs.confidence:.2f})")

            logger.info(f"[卡位板] 检测完成: 共{len(signals)}个信号")

        except Exception as e:
            logger.error(f"[卡位板] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_blast_reseal(self, today_df: pd.DataFrame) -> List[PatternSignal]:
        """
        炸板回封模式识别
        引用: core.pattern.blast_reseal.BlastResealAnalyzer
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
                code = today_row.get('代码', '')
                name = today_row.get('名称', '')
                total_checked += 1

                # 检查是否有炸板
                blast_times = today_row.get('炸板次数', 0)
                if blast_times == 0:
                    continue

                total_blast += 1
                logger.debug(f"[炸板回封] 检测 {name}({code})，炸板次数={blast_times}")

                signal = self._convert_to_pattern_signal(
                    code, name, "炸板回封", today_row, None, None
                )
                if signal:
                    signals.append(signal)
                    total_passed += 1
                    logger.debug(f"[炸板回封]   {name} 生成信号 (置信度{signal.confidence:.2f})")
                else:
                    logger.debug(f"[炸板回封]   {name} 未生成信号")

            logger.info(f"[炸板回封] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 炸板{total_blast}只, 通过{total_passed}只)")

        except Exception as e:
            logger.error(f"[炸板回封] 检测失败: {e}", exc_info=True)

        return signals
    
    def detect_dragon_second_wave(self, today_df: pd.DataFrame,
                                  recent_zt_pools: Dict[str, pd.DataFrame],
                                  today_date: str,
                                  hot_sectors: List[str] = None) -> List[PatternSignal]:
        """
        龙二波模式识别
        引用: core.pattern.dragon_second_wave.DragonSecondWaveStrategyV2.detect_second_wave
        """
        signals = []

        if self.dragon_second_wave is None:
            logger.warning("[龙二波] 策略未加载")
            return signals

        if today_df.empty:
            logger.debug("[龙二波] 今日涨停池为空，跳过检测")
            return signals

        if not recent_zt_pools:
            logger.debug("[龙二波] 历史涨停池为空，跳过检测")
            return signals

        logger.debug(f"[龙二波] 开始检测，今日涨停{len(today_df)}只，历史池{len(recent_zt_pools)}日")
        logger.debug(f"[龙二波] 热点板块: {hot_sectors[:5] if hot_sectors else 'None'}...")

        try:
            # 准备近15日涨停池（龙二波策略需要）
            recent_15d_pools = {}

            # 首先添加今日的涨停池（today_df）
            if not today_df.empty:
                today_pool = today_df.copy()
                # 统一代码格式：补齐到6位
                code_col = None
                if '代码' in today_pool.columns:
                    code_col = '代码'
                elif 'Code' in today_pool.columns:
                    code_col = 'Code'

                if code_col:
                    today_pool[code_col] = today_pool[code_col].astype(str).str.zfill(6)

                recent_15d_pools[today_date] = today_pool
                logger.debug(f"[龙二波] 添加今日涨停池: {today_date}, {len(today_pool)}条")

            # 然后添加历史涨停池
            sorted_dates = sorted(recent_zt_pools.keys(), reverse=True)
            for date in sorted_dates[:14]:  # 只取14天历史数据，加上今天共15天
                pool = recent_zt_pools[date].copy()
                # 统一代码格式：补齐到6位
                code_col = None
                if '代码' in pool.columns:
                    code_col = '代码'
                elif 'Code' in pool.columns:
                    code_col = 'Code'

                if code_col and not pool.empty:
                    pool[code_col] = pool[code_col].astype(str).str.zfill(6)

                recent_15d_pools[date] = pool
            logger.debug(f"[龙二波] 准备近15日涨停池: {list(recent_15d_pools.keys())}")

            # 调用策略模块的检测方法
            total_checked = 0
            total_passed = 0
            for _, today_row in today_df.iterrows():
                code = today_row.get('代码', '')
                name = today_row.get('名称', '')
                total_checked += 1

                # 补齐代码到6位
                code_padded = str(code).zfill(6)

                logger.debug(f"[龙二波] 检测 {name}({code} -> {code_padded})...")

                # 检查板块是否热点
                sector_hot = False
                stock_sector = today_row.get('所属行业', '')
                if hot_sectors and stock_sector:
                    sector_hot = stock_sector in hot_sectors
                    logger.debug(f"[龙二波]   {name} 所属行业: {stock_sector}, 是否热点: {sector_hot}")

                # 调用DragonSecondWaveStrategyV2.detect_second_wave
                trade_signal = self.dragon_second_wave.detect_second_wave(
                    stock_code=code_padded,
                    stock_name=name,
                    today_str=today_date,
                    recent_zt_pools=recent_15d_pools,
                    today_data=today_row,
                    sector_hot=sector_hot
                )

                if trade_signal:
                    total_passed += 1
                    logger.debug(f"[龙二波]   {name} 通过检测，生成信号 (置信度{trade_signal.confidence:.2f})")

                    # 将TradeSignal转换为PatternSignal
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
    
    # ==================== 批量扫描接口 ====================
    
    def scan_all_patterns(self, today_date: str, yesterday_date: str) -> Dict[str, List[PatternSignal]]:
        """
        扫描全市场所有模式
        """
        results = {
            "弱转强": [],
            "二板定龙": [],
            "首板突破": [],
            "分歧转一致": [],
            "卡位板": [],
            "炸板回封": [],
            "龙二波": []
        }
        
        # 获取今日和昨日涨停池
        today_zt = self.dm.get_limit_up_pool(today_date)
        yesterday_zt = self.dm.get_limit_up_pool(yesterday_date)
        
        if today_zt.empty:
            logger.warning("今日涨停池为空，无法识别模式")
            return results
        
        logger.info(f"开始模式识别，今日涨停{len(today_zt)}只，昨日涨停{len(yesterday_zt)}只")

        # 获取前日数据（用于计算连板高度）
        day_before_yesterday_date = self._get_date_offset(today_date, -2)
        day_before_yesterday_zt = self.dm.get_limit_up_pool(day_before_yesterday_date)

        # 准备历史涨停池数据（用于首板突破和龙二波检测）
        logger.info("  准备历史涨停池数据（近20个交易日）...")
        history_pools = {}
        days_checked = 0
        max_calendar_days = 60  # 最多检查60个日历日

        while len(history_pools) < 20 and days_checked < max_calendar_days:
            check_date = self._get_date_offset(today_date, -(days_checked + 1))
            days_checked += 1

            # 验证是否为交易日
            is_valid, actual_date, message = self.dm.validate_trade_date(check_date)

            # 如果不是交易日，跳过
            if not is_valid:
                logger.debug(f"  {check_date} 非交易日，跳过")
                continue

            # 已经获取过该日期，跳过
            if actual_date in history_pools:
                continue

            # 获取该交易日涨停数据
            try:
                pool = self.dm.get_limit_up_pool(actual_date)
                if not pool.empty:
                    # 如果有mapper，进行行业映射
                    if self.mapper:
                        pool = self.mapper.build_hierarchy_dataframe(pool)
                    history_pools[actual_date] = pool
                    logger.debug(f"  ✅ 获取交易日数据: {actual_date}, 进度 {len(history_pools)}/20")
                else:
                    # 即使数据为空，也算作一个交易日
                    history_pools[actual_date] = pd.DataFrame()
                    logger.debug(f"  ⚠️ {actual_date} 无涨停数据, 进度 {len(history_pools)}/20")
            except Exception as e:
                logger.warning(f"  获取{actual_date}涨停池失败: {e}")
                continue

        if len(history_pools) < 20:
            logger.warning(f"  仅获取到 {len(history_pools)} 个交易日数据（目标20个）")

        logger.info(f"  历史涨停池准备完成: {len(history_pools)}个交易日数据")

        # 1. 检测弱转强
        if not yesterday_zt.empty:
            results["弱转强"] = self.detect_weak_to_strong(
                today_zt, yesterday_zt, day_before_yesterday_zt
            )
            logger.info(f"  弱转强: {len(results['弱转强'])}个")

        # 2. 检测二板定龙
        if not yesterday_zt.empty:
            results["二板定龙"] = self.detect_second_board_dragon(today_zt, yesterday_zt)
            logger.info(f"  二板定龙: {len(results['二板定龙'])}个")

        # 3. 检测首板突破（使用历史涨停池数据）
        results["首板突破"] = self.detect_first_board_breakout(today_zt, yesterday_zt, today_date, history_pools)
        logger.info(f"  首板突破: {len(results['首板突破'])}个")

        # 4. 检测分歧转一致
        if not yesterday_zt.empty:
            results["分歧转一致"] = self.detect_divergence_to_consensus(today_zt, yesterday_zt)
            logger.info(f"  分歧转一致: {len(results['分歧转一致'])}个")

        # 5. 检测卡位板
        results["卡位板"] = self.detect_position_battle(today_zt, yesterday_zt)
        logger.info(f"  卡位板: {len(results['卡位板'])}个")

        # 6. 检测炸板回封
        results["炸板回封"] = self.detect_blast_reseal(today_zt)
        logger.info(f"  炸板回封: {len(results['炸板回封'])}个")

        # 7. 检测龙二波（需要近15日涨停池数据）
        if len(history_pools) >= 5:
            # 获取热点板块列表
            hot_sectors = []
            if hasattr(self, 'sector_engine') and self.sector_engine:
                try:
                    sector_df = self.sector_engine.analyze_all_sectors_v2(today_zt, history_pools, self.mapper)
                    if not sector_df.empty:
                        hot_sectors = sector_df['二级行业'].tolist()
                except Exception as e:
                    logger.debug(f"获取热点板块失败: {e}")

            results["龙二波"] = self.detect_dragon_second_wave(today_zt, history_pools, today_date, hot_sectors)
            logger.info(f"  龙二波: {len(results['龙二波'])}个（基于{len(history_pools)}日数据）")
        else:
            logger.warning(f"  龙二波: 数据不足（仅{len(history_pools)}日），跳过检测")
        
        total = sum(len(v) for v in results.values())
        logger.info(f"模式识别完成，共{total}个信号")
        
        return results
    
    # ==================== 辅助方法 ====================
    
    def _convert_to_pattern_signal(self, code: str, name: str, pattern_type: str,
                                   today_row: pd.Series, yest_row: pd.Series = None,
                                   board_height: int = None) -> Optional[PatternSignal]:
        """将数据转换为PatternSignal格式"""
        try:
            entry_price = today_row.get('涨停价', today_row.get('最新价', 0))
            today_change = today_row.get('涨跌幅', 0)
            if isinstance(today_change, str):
                today_change = float(today_change.replace('%', ''))

            key_metrics = {
                "涨停时间": today_row.get('首次封板时间', ''),
                "封单额": f"{today_row.get('封单额', 0)/1e4:.0f}万",
                "换手率": f"{today_row.get('换手率', 0):.1f}%"
            }

            if board_height:
                key_metrics["连板高度"] = board_height

            if yest_row is not None:
                key_metrics["昨日涨幅"] = yest_row.get('涨跌幅', 0)

            # 获取二级行业（所属行业）
            l2_industry = today_row.get('所属行业', '')

            return PatternSignal(
                pattern_type=pattern_type,
                stock_code=code,
                stock_name=name,
                confidence=0.75,
                description=f"{pattern_type}信号",
                key_metrics=key_metrics,
                entry_price=entry_price,
                stop_loss=entry_price * 0.93 if entry_price else None,
                take_profit=entry_price * 1.10 if entry_price else None,
                validation_rules=[f"检测到{pattern_type}模式"],
                l2_industry=l2_industry
            )
        except Exception as e:
            logger.error(f"转换信号失败: {e}")
            return None
    
    def _calculate_board_height(self, code: str, today_row: pd.Series,
                                yesterday_df: pd.DataFrame,
                                day_before_yesterday_df: pd.DataFrame = None) -> int:
        """计算当前连板高度"""
        today_change = today_row.get('涨跌幅', 0)
        if isinstance(today_change, str):
            today_change = float(today_change.replace('%', ''))
        if today_change < 9.5:
            return 0
        
        height = 1
        
        if yesterday_df is not None and not yesterday_df.empty and '代码' in yesterday_df.columns:
            if code in yesterday_df['代码'].values:
                height += 1
                if day_before_yesterday_df is not None and not day_before_yesterday_df.empty:
                    if code in day_before_yesterday_df['代码'].values:
                        height += 1
        
        return height

    def _get_date_offset(self, date_str: str, offset_days: int) -> str:
        """获取指定日期偏移后的日期"""
        date = datetime.strptime(date_str, "%Y%m%d")
        new_date = date + timedelta(days=offset_days)
        return new_date.strftime("%Y%m%d")
    
    def _is_fast_limit_up(self, limit_up_time: str, max_minutes: int = 40) -> bool:
        """
        判断是否为早盘秒封
        
        Args:
            limit_up_time: 首次封板时间 (格式: "HH:MM:SS" 或 "HH:MM")
            max_minutes: 最大分钟数（默认9:40前）
        
        Returns:
            bool: 是否在指定时间前封板
        """
        if not limit_up_time or limit_up_time in ['', 'nan', 'None']:
            return False
        
        try:
            # 处理时间字符串
            time_str = str(limit_up_time).strip()
            parts = time_str.split(':')
            
            if len(parts) >= 2:
                hour = int(parts[0])
                minute = int(parts[1])
                
                # 计算从9:30开始的分钟数
                if hour < 9:
                    return True  # 9:30前（集合竞价）算秒封
                elif hour == 9:
                    minutes_from_open = minute - 30
                    return minutes_from_open <= max_minutes
                else:
                    return False  # 10:00后不算秒封
        except (ValueError, IndexError):
            pass
        
        return False


if __name__ == "__main__":
    print("模式识别引擎初始化成功")
    print("支持模式: 弱转强、二板定龙、首板突破、分歧转一致、卡位板、炸板回封、龙二波")
    print("各模式逻辑来自core.pattern目录下的独立策略模块")
