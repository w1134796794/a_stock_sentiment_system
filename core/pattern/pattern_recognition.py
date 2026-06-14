"""
模式识别引擎 - 统一入口
整合所有模式策略：弱转强、二板定龙、分歧转一致、首板突破、卡位板、炸板回封、龙二波等

使用方式：
    pr = PatternRecognition(data_manager)
    results = pr.scan_all_patterns(today_date, yesterday_date)

注：``PatternSignal`` 已统一到 ``core.pattern.base``，本模块仅做向后兼容重导出。
"""
import pandas as pd
import numpy as np
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import loguru

# PatternSignal 统一契约位于 core.pattern.base
from core.pattern.base import PatternSignal  # noqa: F401  re-export for backward compat
from core.pattern.dragon_lifecycle import DragonLifecycleRegistry, DragonPhase
from config.pattern_params import get_params

logger = loguru.logger


class PatternRecognition:
    """模式识别引擎 - 统一调度各策略模块"""

    def __init__(self, data_manager, sector_engine=None, mapper=None, repo=None):
        self.dm = data_manager
        self.se = sector_engine
        self.mapper = mapper
        self.lookback_days = 20

        # Phase 1 数据解耦：业务层经只读仓库取数。未显式注入时，从 dm 懒构造
        # 「透传仓库」——行为与改造前完全一致，便于逐调用点平滑迁移 + 回归对齐。
        if repo is None:
            from core.data.repository import StockRepository
            repo = StockRepository.passthrough(data_manager)
        self.repo = repo

        # 弱转强 Layer4 多维评分阈值（可经 webdata/config_overrides.json 覆盖）
        self.wts_scoring = get_params("weak_to_strong_scoring")

        # 龙头生命周期视图（每次 detect_weak_to_strong 重建；见 dragon_lifecycle.py）
        self._lifecycle: Optional[DragonLifecycleRegistry] = None

        # 初始化各策略模块
        self._init_strategies()
    
    def set_repo(self, repo):
        """刷新只读仓库并传播给各策略（多日复用时保证用当日仓库）。"""
        if repo is None:
            return
        self.repo = repo
        for strat in (getattr(self, "weak_to_strong", None),
                      getattr(self, "first_board_breakout", None),
                      getattr(self, "dragon_second_wave", None)):
            if strat is not None and hasattr(strat, "repo"):
                strat.repo = repo

    def _init_strategies(self):
        """初始化所有策略模块"""
        try:
            from core.pattern.weak_to_strong import WeakToStrongStrategy
            self.weak_to_strong = WeakToStrongStrategy(self.dm, self.se, repo=self.repo)
            logger.info("✓ 弱转强策略加载成功")
        except Exception as e:
            logger.warning(f"✗ 弱转强策略加载失败: {e}")
            self.weak_to_strong = None
        
        # 二板定龙：检测逻辑已内联到本类 detect_second_board_dragon()（旧的
        # SecondBoardDragonStrategy 独立类为重复实现，已删除）。此处保留启用标记。
        self.second_board_dragon = True
        
        try:
            from core.pattern.first_board_breakout import HotspotFirstBoardStrategy
            self.first_board_breakout = HotspotFirstBoardStrategy(self.dm, self.se, self.mapper, repo=self.repo)
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
            self.dragon_second_wave = DragonSecondWaveStrategyV2(self.dm, self.se, repo=self.repo)
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
            
            # 构建龙头生命周期视图（单一事实来源；Phase 1 地基接入实时路径）
            # 当前仅驱动 FLASH_RECOVERY（日内反转）路径，主路径仍走既有筛选，行为不变。
            self._lifecycle = DragonLifecycleRegistry().from_weak_to_strong(
                self.weak_to_strong, today_date or datetime.now().strftime("%Y%m%d")
            )
            logger.debug(f"[弱转强] 生命周期阶段分布: {self._lifecycle.summary()}")

            # ========== 阶段2：转强信号检测（四层优先级管线：L0→L1→L2→L3→L4）==========
            logger.info("[弱转强] ========== 阶段2：转强信号检测 ==========")

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

            # ══════════════════════════════════════════════════════════
            # 阶段2a: Layer 0→Layer 2 前置筛选（批量标注候选，再逐个深入）
            # ══════════════════════════════════════════════════════════
            candidates = []  # (code, today_row, is_limit_up, price_change, auction_only_recovery)
            l0_skipped = 0
            l0_not_strong = 0

            for code in valid_weakening_codes:
                weakening = self.weak_to_strong.weakening_pool[code]
                name = weakening.stock_name

                # ── Layer 0: 硬性排除（零成本 — 仅用today_df/today_daily自带字段）──
                today_row = None
                is_limit_up = False
                price_change = 0
                auction_only_recovery = False

                # L0a: 今日涨停
                if code in today_zt_codes:
                    today_row = today_zt_dict[code]
                    is_limit_up = True
                    price_change = 9.9
                # L0b: 大涨≥5%
                elif today_daily is not None and not today_daily.empty:
                    stock_daily = today_daily[today_daily['ts_code'].str.contains(code)]
                    if not stock_daily.empty:
                        latest = stock_daily.iloc[-1]
                        price_change = latest.get('pct_chg', 0)
                        if price_change >= 5.0:
                            today_row = latest
                        elif price_change >= 2.0:
                            today_row = latest
                            auction_only_recovery = True
                        elif 'open' in latest.index and 'pre_close' in latest.index:
                            try:
                                today_open_v = float(latest['open'])
                                today_pre_close_v = float(latest['pre_close'])
                                if today_open_v > 0 and today_pre_close_v > 0:
                                    gap_from_daily = (today_open_v - today_pre_close_v) / today_pre_close_v
                                    if gap_from_daily >= 0.03:
                                        today_row = latest
                                        auction_only_recovery = True
                                        price_change = gap_from_daily * 100
                            except Exception:
                                pass

                if today_row is None:
                    l0_not_strong += 1
                    logger.debug(f"[弱转强-L0] ✗ {name}({code}) 过滤: 今日未转强(涨幅{price_change:.1f}%)")
                    continue

                # L0b-guard: 高开低走/冲高回落排除（仅非涨停的"大涨/竞价转强"候选）。
                # 开得高却收盘走弱属接力失败，不算转强；涨停票收在板上不受此约束。
                if not is_limit_up and self.wts_scoring.get("intraday_fade_guard_enabled", True):
                    try:
                        _o = float(today_row.get('open', 0) or 0)
                        _c = float(today_row.get('close', 0) or 0)
                        _h = float(today_row.get('high', 0) or 0)
                        fade_open = (_o - _c) / _o if _o > 0 else 0.0   # >0 收盘低于开盘（低走）
                        fade_high = (_h - _c) / _h if _h > 0 else 0.0   # 收盘距当日最高的回落
                        max_from_open = float(self.wts_scoring.get("intraday_fade_max_from_open", 0.03))
                        max_from_high = float(self.wts_scoring.get("intraday_fade_max_from_high", 0.06))
                        if (_c < _o and fade_open > max_from_open) or (_h > 0 and fade_high > max_from_high):
                            l0_not_strong += 1
                            logger.info(f"[弱转强-L0] ✗ {name}({code}) 过滤: 高开低走/冲高回落"
                                        f"(较开盘回落{fade_open*100:.1f}%, 较最高回落{fade_high*100:.1f}%)")
                            continue
                    except Exception:
                        pass

                # L0c: 一字板排除
                if is_limit_up:
                    first_time = str(today_row.get('首次封板时间', '')).strip()
                    if first_time == '09:25:00' or first_time == '09:25':
                        l0_skipped += 1
                        logger.info(f"[弱转强-L0] ✗ {name}({code}) 过滤: 一字板，无转强过程")
                        continue

                # L0d: 尾盘板排除
                if is_limit_up:
                    first_time = str(today_row.get('首次封板时间', '')).strip()
                    if first_time and self.weak_to_strong._is_valid_limit_time and \
                       not self.weak_to_strong._is_valid_limit_time(first_time, '14:30'):
                        l0_skipped += 1
                        logger.info(f"[弱转强-L0] ✗ {name}({code}) 过滤: 尾盘板({first_time})")
                        continue

                candidates.append((code, today_row, is_limit_up, price_change, auction_only_recovery))

            logger.info(f"[弱转强-L0] 硬性排除完成: {len(candidates)}只候选, "
                        f"未转强{l0_not_strong}只, 一字板/尾盘板{l0_skipped}只")
            if not candidates:
                return signals

            # ══════════════════════════════════════════════════════════
            # 阶段2b: Layer 1→Layer 4 逐只深入检测
            # ══════════════════════════════════════════════════════════
            total_checked = 0
            total_passed = 0
            l1_skipped = 0
            l2_skipped = 0
            l3_skipped = 0
            l4_skipped = 0
            total_detected = 0

            for (code, today_row, is_limit_up, price_change, auction_only_recovery) in candidates:
                weakening = self.weak_to_strong.weakening_pool[code]
                name = weakening.stock_name
                total_checked += 1
                l2_penalty = 0.0

                logger.info(f"[弱转强-L1] ── 检测 {name}({code}), 入池={weakening.weakening_date}, "
                            f"类型={weakening.dragon_type.value} ——")

                # ═══════════ Layer 1: 前身验证（龙头身份+走弱确认 — 已由update_dragon_pools完成）═══════════
                peak_height = weakening.peak_board_height if weakening.dragon_type.value == "连板龙头" else 0
                weakening_type = weakening.weakening_type
                max_drawdown = weakening.max_drawdown

                # L1a: 防A杀 — 回调超过20%排除
                if max_drawdown > self.weak_to_strong.params.get('max_drawdown_for_recovery', 0.20):
                    l1_skipped += 1
                    logger.info(f"[弱转强-L1] ✗ {name}({code}) 过滤: 回调过深{max_drawdown*100:.1f}%>20%, 疑似A杀")
                    continue

                logger.info(f"[弱转强-L1] ✓ {name}({code}) 通过前身验证 "
                            f"({weakening.dragon_type.value}{peak_height}板, {weakening_type}, 回调{max_drawdown*100:.1f}%)")

                # ═══════════ Layer 2: 转强技术确认（高开gap + 竞价量 + 日线量能）═══════════
                yest_match = yesterday_df[yesterday_df['代码'].astype(str).str.zfill(6) == code]
                yest_row = yest_match.iloc[0] if not yest_match.empty else None

                # L2a: 高开gap + 竞价分析
                auction_analysis = {}
                if today_date and self.repo:
                    try:
                        auction_data = self.repo.get_auction_data(code, today_date)
                        if auction_data:
                            auction_analysis = self._analyze_auction_data(auction_data, yest_row, today_row, yest_date)
                    except Exception as e:
                        logger.debug(f"[弱转强-L2] {name} 竞价数据获取失败: {e}")

                if not auction_analysis and today_row is not None:
                    try:
                        today_open_v = float(today_row.get('open', 0))
                        today_pre_close_v = float(today_row.get('pre_close', 0))
                        if today_open_v > 0 and today_pre_close_v > 0:
                            gap = (today_open_v - today_pre_close_v) / today_pre_close_v
                            auction_analysis = {
                                'gap': gap,
                                'auction_vol_ratio': 0,
                                'auction_amount': float(today_row.get('amount', 0)),
                            }
                    except Exception:
                        pass

                # L2b: 竞价/高开维度前置判定（涨幅不足但高开显著）
                if auction_only_recovery:
                    gap = auction_analysis.get('gap', 0)
                    auction_vol = auction_analysis.get('auction_vol_ratio', 0)
                    if auction_vol > 0:
                        if gap >= 0.02 and auction_vol >= 0.08:
                            logger.info(f"[弱转强-L2] ✓ {name} 竞价维度转强确认: 高开{gap*100:.1f}%+量比{auction_vol*100:.1f}%")
                            auction_only_recovery = False
                        else:
                            l2_skipped += 1
                            logger.info(f"[弱转强-L2] ✗ {name}({code}) 过滤: 竞价维度不足(高开{gap*100:.1f}%,量比{auction_vol*100:.1f}%)")
                            continue
                    else:
                        if gap >= 0.03:
                            logger.info(f"[弱转强-L2] ✓ {name} 日线高开转强确认: 高开{gap*100:.1f}%(无竞价数据)")
                            auction_only_recovery = False
                        else:
                            l2_skipped += 1
                            logger.info(f"[弱转强-L2] ✗ {name}({code}) 过滤: 日线高开不足{gap*100:.1f}%<3%")
                            continue

                # L2c: 动态gap阈值检查（弹性评分）
                gap = auction_analysis.get('gap', 0)
                auction_vol_ratio = auction_analysis.get('auction_vol_ratio', 0)
                # 基础gap阈值 = 2%，连板龙头可放宽到1%
                min_gap_dynamic = 0.01 if (weakening.dragon_type.value == "连板龙头" and peak_height >= 4) else 0.02

                if gap < min_gap_dynamic:
                    l2_penalty += 0.04
                    logger.info(f"[弱转强-L2] ⚠ {name} 高开不足(gap={gap*100:.1f}%<{min_gap_dynamic*100:.0f}%), 扣{0.04:.2f}")

                # L2d: 量能确认（从日线获取）
                vol_ratio = 0
                try:
                    if today_daily is not None and not today_daily.empty:
                        sd = today_daily[today_daily['ts_code'].str.contains(code)]
                        if not sd.empty:
                            vol_ratio = float(sd.iloc[-1].get('volume_ratio', 0))
                except Exception:
                    pass

                if auction_vol_ratio < 0.05 and (vol_ratio > 0 and vol_ratio < 1.0):
                    l2_penalty += 0.03
                    logger.info(f"[弱转强-L2] ⚠ {name} 量能不足(竞价量比{auction_vol_ratio*100:.1f}%,日线量比{vol_ratio:.1f}), 扣{0.03:.2f}")

                logger.info(f"[弱转强-L2] {'✓' if l2_penalty == 0 else '⚠'} {name}({code}) 转强确认 "
                            f"(高开{gap*100:.1f}%|动态阈值{min_gap_dynamic*100:.0f}%, 竞价量比{auction_vol_ratio*100:.1f}%, "
                            f"扣分{l2_penalty:.2f})")

                # ═══════════ Layer 3: 质量指标 — 弹性评分 ═══════════
                l3_penalty = 0.0

                # L3a: 涨停时间检查
                if is_limit_up:
                    limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                    if limit_up_time and self.weak_to_strong._is_valid_limit_time:
                        if not self.weak_to_strong._is_valid_limit_time(limit_up_time, '10:30'):
                            l3_penalty += 0.03
                            logger.info(f"[弱转强-L3] ⚠ {name} 涨停偏晚({limit_up_time}), 扣{0.03:.2f}")
                else:
                    limit_up_time = "N/A（大涨未涨停）"

                # L3b: 开板次数
                if is_limit_up:
                    break_count = int(today_row.get('开板次数', 0) or today_row.get('炸板次数', 0))
                    if break_count >= 2:
                        l3_penalty += 0.04
                        logger.info(f"[弱转强-L3] ⚠ {name} 开板{break_count}次, 扣{0.04:.2f}")

                # L3c: 流通市值上限
                float_cap = float(today_row.get('流通市值', 0))
                if float_cap > 200:
                    l3_penalty += 0.03
                    logger.info(f"[弱转强-L3] ⚠ {name} 市值偏大({float_cap:.1f}亿>200亿), 扣{0.03:.2f}")

                if l3_penalty >= 0.06:
                    l3_skipped += 1
                    logger.info(f"[弱转强-L3] ✗ {name}({code}) 过滤: 累计质量扣分{l3_penalty:.2f}≥0.06")
                    continue

                logger.info(f"[弱转强-L3] {'✓' if l3_penalty == 0 else '⚠'} {name}({code}) 通过质量检查 "
                            f"(时间{limit_up_time}, 市值{float_cap:.1f}亿, 扣分{l3_penalty:.2f})")

                # ═══════════ Layer 4: 多维评分 + 信号生成 ═══════════
                wts = self.wts_scoring
                auction_cap = int(wts.get('auction_cap', 25))
                # 1. 竞价量价维度评分（满分 auction_cap）
                auction_score = 0
                if gap >= wts.get('auction_gap_high', 0.05):
                    auction_score += wts.get('auction_gap_high_pts', 15)
                elif gap >= wts.get('auction_gap_mid', 0.03):
                    auction_score += wts.get('auction_gap_mid_pts', 10)
                elif gap >= wts.get('auction_gap_low', 0.02):
                    auction_score += wts.get('auction_gap_low_pts', 5)

                if auction_vol_ratio >= wts.get('auction_vol_high', 0.15):
                    auction_score += wts.get('auction_vol_high_pts', 10)
                elif auction_vol_ratio >= wts.get('auction_vol_mid', 0.10):
                    auction_score += wts.get('auction_vol_mid_pts', 5)

                if auction_analysis.get('auction_amount', 0) >= wts.get('auction_amount_threshold', 10_000_000):
                    auction_score += wts.get('auction_amount_pts', 5)
                auction_score = min(auction_score, auction_cap)  # 三项最高>封顶时按 auction_cap 截断

                logger.debug(f"[弱转强-L4] {name} 竞价维度: 高开{gap*100:.1f}%,量比{auction_vol_ratio*100:.1f}%,得分{auction_score}/{auction_cap}")

                # 2. 技术形态维度评分（满分 20）
                technical_score = self._calculate_technical_score(code, today_date, today_daily)
                logger.debug(f"[弱转强-L4] {name} 技术维度得分{technical_score}/20")

                # 3. 资金流入维度评分（满分 25）
                capital_score = self._calculate_capital_score(code, today_date)
                logger.debug(f"[弱转强-L4] {name} 资金维度得分{capital_score}/25")

                # 4. 市场情绪维度评分（20分）
                sector_name = (stock_to_ths_industry or {}).get(code, '') or (stock_to_ths_concept or {}).get(code, '') or today_row.get('所属行业', '') or today_row.get('industry', '')
                sector_score = self._calculate_sector_score(
                    sector_name, today_date, today_df,
                    stock_to_ths_industry=stock_to_ths_industry,
                    stock_to_ths_concept=stock_to_ths_concept
                )
                logger.debug(f"[弱转强-L4] {name} 情绪维度得分{sector_score}/20")

                # 总评分
                total_score = auction_score + technical_score + capital_score + sector_score
                total_score -= (l2_penalty + l3_penalty) * 50  # 扣分换算为评分扣减

                logger.info(f"[弱转强-L4] {name} 总评分={total_score:.0f} "
                            f"(竞价{auction_score}+技术{technical_score}+资金{capital_score}+情绪{sector_score}"
                            f"-L2扣{int(l2_penalty*50)}-L3扣{int(l3_penalty*50)})")

                # 动态阈值：连板龙头/趋势龙头不同标准
                if weakening.dragon_type.value == "连板龙头" and peak_height >= 5:
                    min_total_score = wts.get('min_total_strong_dragon', 45)  # 强龙头降低门槛
                elif weakening.dragon_type.value == "连板龙头":
                    min_total_score = wts.get('min_total_dragon', 50)
                else:
                    min_total_score = wts.get('min_total_trend', 55)  # 趋势龙头要求更高

                if total_score < min_total_score:
                    l4_skipped += 1
                    logger.info(f"[弱转强-L4] ✗ {name}({code}) 过滤: 总评分{total_score:.0f}<{min_total_score}(动态阈值)")
                    continue

                # 信号级别（基于总评分的标签，用于描述/校验规则）
                if total_score >= wts.get('signal_level_strong', 80):
                    signal_level = "强烈信号"
                elif total_score >= wts.get('signal_level_confirm', 65):
                    signal_level = "确认信号"
                else:
                    signal_level = "基础信号"

                # 置信度：confidence_mode="deduction" 走统一满分扣分制（ConfidenceScorer +
                # confidence_rules.yaml 的 weak_to_strong），并附逐项扣分明细；否则沿用旧的
                # total_score→置信度 映射（行为不变）。
                wt_mode = self.weak_to_strong.params.get("confidence_mode", "legacy")
                conf_breakdown = None
                confidence = None
                if wt_mode == "deduction":
                    from core.scoring.confidence_scorer import score_or_none
                    _wt = weakening.weakening_type or ""
                    _wt_cat = "断板" if _wt == "断板" else ("放量滞涨" if "放量滞涨" in _wt else "其他")
                    _res = score_or_none("weak_to_strong", {
                        "gap_pct": auction_analysis.get('gap', 0),
                        "auction_vol_ratio": auction_analysis.get('auction_vol_ratio', 0),
                        "flexible_score": total_score,   # 用综合评分作为弹性/质量代理
                        "weakening_type": _wt_cat,
                    })
                    if _res is not None:
                        confidence = _res.value
                        conf_breakdown = _res.to_dict()
                    else:
                        logger.warning("[弱转强] 扣分制规则缺失/失败，回退 legacy 置信度")

                if confidence is None:
                    # legacy：基础分映射
                    if total_score >= 80:
                        confidence = 0.90
                    elif total_score >= 65:
                        confidence = 0.75
                    else:
                        confidence = 0.60
                    confidence = min(confidence, 0.95)

                # 涨停时间加分
                if is_limit_up:
                    time_score = self._calculate_time_score(limit_up_time)
                else:
                    time_score = 50

                # 仓位
                if total_score >= 85:
                    position_size = "heavy"
                elif total_score >= 75:
                    position_size = "medium"
                else:
                    position_size = "light"

                # 股价
                entry_price = today_row.get('涨停价', 0) or today_row.get('close', 0)
                if not entry_price or entry_price <= 0:
                    entry_price = today_row.get('close', 0)

                signal = PatternSignal(
                    pattern_type="弱转强",
                    stock_code=code,
                    stock_name=name,
                    confidence=round(confidence, 2),
                    description=f"{weakening.dragon_type.value}{peak_height}板后{weakening_type}转强，{signal_level}，总评分{total_score:.0f}分",
                    key_metrics={
                        "龙头类型": weakening.dragon_type.value,
                        "最高连板": peak_height,
                        "走弱类型": weakening_type,
                        "走弱日期": weakening.weakening_date,
                        "信号级别": signal_level,
                        "总评分": int(total_score),
                        "竞价评分": auction_score,
                        "技术评分": technical_score,
                        "资金评分": capital_score,
                        "情绪评分": sector_score,
                        "L2扣分": f"{l2_penalty:.2f}",
                        "L3扣分": f"{l3_penalty:.2f}",
                        "次日高开": f"{auction_analysis.get('gap', 0)*100:.1f}%",
                        "竞价量比": f"{auction_analysis.get('auction_vol_ratio', 0)*100:.1f}%",
                        "涨停时间": limit_up_time,
                        "回调幅度": f"{max_drawdown*100:.1f}%",
                    },
                    entry_price=entry_price,
                    stop_loss=entry_price * 0.93,
                    take_profit=entry_price * 1.15,
                    position_size=position_size,
                    validation_rules=[
                        f"{weakening.dragon_type.value}{peak_height}板（身份）",
                        f"{weakening_type}后走弱（弱）",
                        f"总评分{total_score:.0f}分（{signal_level}）",
                        f"竞价{auction_score}分/25分（高开{auction_analysis.get('gap', 0)*100:.1f}%）",
                        f"技术{technical_score}分/25分",
                        f"资金{capital_score}分/25分",
                        f"情绪{sector_score}分/20分",
                        "开盘不回踩，快速上板（确认强）"
                    ],
                    l2_industry=sector_name
                )

                # Phase 3：扣分制下附逐项扣分明细，便于复盘"为什么不是满分"
                if conf_breakdown:
                    signal.key_metrics["置信扣分明细"] = conf_breakdown

                signals.append(signal)
                total_passed += 1
                total_detected += 1
                logger.info(f"[弱转强-L4] ✓ {name}({code}) 生成信号: 置信度{confidence:.2f}, {position_size}")

            logger.info(f"[弱转强] 检测完成: 共{len(signals)}个信号 "
                        f"(候选{len(candidates)}→L1过滤{l1_skipped}→L2过滤{l2_skipped}→"
                        f"L3过滤{l3_skipped}→L4过滤{l4_skipped}→通过{total_passed})")

            # ========== P4: 当日走弱+当日转强（生命周期 FLASH_RECOVERY 阶段）==========
            # 场景：烂板回封 / 断板后强力收复（当天入池，当天就转强）。
            # 候选改由生命周期注册中心的 FLASH_RECOVERY 阶段驱动（取代原"遍历走弱池+
            # 再判当日入池"的临时逻辑），把"当日反转"确立为一个合法阶段而非补丁。
            same_day_flicker = 0
            for _st in self._lifecycle.query((DragonPhase.FLASH_RECOVERY,), include_handoff=False):
                code = _st.stock_code
                weakening = self.weak_to_strong.weakening_pool.get(code)
                if weakening is None:
                    continue
                # 今日入池但今日涨停 = 烂板回封/断板后收复
                if code in today_zt_codes:
                    same_day_flicker += 1
                    zt_row = today_zt_dict[code]
                    current_price = zt_row.get('最新价', 0)
                    board_height = zt_row.get('连板数', 0)
                    sector_name = weakening.sector_name or ""

                    logger.info(f"[弱转强-P4] ⚡ {weakening.stock_name}({code}) 当日走弱({weakening.weakening_type})→当日转强(涨停!)")

                    # 置信度：与主路径一致——confidence_mode="deduction" 走统一扣分制；
                    # 否则用可配置兜底值（intraday_reversal_confidence，默认 0.65）。
                    p4_conf = self.wts_scoring.get('intraday_reversal_confidence', 0.65)
                    p4_breakdown = None
                    if (self.weak_to_strong and
                            self.weak_to_strong.params.get("confidence_mode", "legacy") == "deduction"):
                        from core.scoring.confidence_scorer import score_or_none
                        _wt = weakening.weakening_type or ""
                        _wt_cat = "断板" if _wt == "断板" else ("放量滞涨" if "放量滞涨" in _wt else "其他")
                        _res = score_or_none("weak_to_strong", {
                            # 日内反转无次日竞价数据，gap/量比缺失（按扣分制最差扣分计）
                            "flexible_score": 60,  # 当日即涨停收复，给中性弹性分
                            "weakening_type": _wt_cat,
                        })
                        if _res is not None:
                            p4_conf = _res.value
                            p4_breakdown = _res.to_dict()
                        else:
                            logger.warning("[弱转强-P4] 扣分制规则缺失/失败，回退兜底置信度")

                    signal = PatternSignal(
                        pattern_type="弱转强",
                        stock_code=code,
                        stock_name=weakening.stock_name,
                        confidence=round(p4_conf, 2),
                        description=f"日内反转: 今日入池走弱({weakening.weakening_type})→今日涨停收复(余{board_height}板)",
                        entry_price=current_price,
                        stop_loss=current_price * 0.95,
                        take_profit=current_price * 1.10,
                        position_size="light",
                        key_metrics={
                            "走弱类型": weakening.weakening_type,
                            "涨停收复": f"余{board_height}板",
                            "龙头类型": weakening.dragon_type.value,
                        },
                        validation_rules=[
                            f"今日入池走弱: {weakening.weakening_type}",
                            f"今日涨停收复(余{board_height}板)",
                            f"龙头类型: {weakening.dragon_type.value}",
                            "日内反转信号（需次日确认）"
                        ],
                        l2_industry=sector_name
                    )
                    if p4_breakdown:
                        signal.key_metrics["置信扣分明细"] = p4_breakdown
                    signals.append(signal)

            if same_day_flicker > 0:
                logger.info(f"[弱转强-P4] 日内反转检测完成: {same_day_flicker}个信号")

            logger.info(f"[弱转强] 检测完成: 共{len(signals)}个信号 (检查{total_checked}只, 通过{total_passed}只, 日内反转{same_day_flicker}个)")

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

            # 定龙择优参数（可被网页覆盖；默认与旧逻辑一致）
            sbd_params = get_params("second_board_dragon")
            min_fb_score = sbd_params.get("min_first_board_score", 60)
            cand_min_gap = sbd_params.get("candidate_min_gap", 0.02)
            cand_min_conf = sbd_params.get("candidate_min_confidence", 0.70)
            max_dragons = int(sbd_params.get("max_dragons_per_day", 3))
            max_per_sector = int(sbd_params.get("max_per_sector", 1))
            min_sector_fb = int(sbd_params.get("min_sector_first_board", 0))
            w_gap = sbd_params.get("rank_w_gap", 1.0)
            w_quality = sbd_params.get("rank_w_quality", 0.5)
            w_seal = sbd_params.get("rank_w_seal", 0.8)
            w_fast = sbd_params.get("rank_w_fast", 10.0)
            w_sector = sbd_params.get("rank_w_sector_assist", 6.0)
            # Phase 3：置信度计算模式。"legacy"=旧的基础分+加分（默认，行为不变）；
            # "deduction"=统一满分扣分制（config/confidence_rules.yaml 的 second_board_dragon）
            confidence_mode = sbd_params.get("confidence_mode", "legacy")

            # 预过滤：只保留连板数=2的股票
            board_col = '连板数' if '连板数' in today_df.columns else 'limit_times'
            if board_col not in today_df.columns:
                logger.debug("[二板定龙] 涨停池缺少连板数列")
                return signals

            second_board_df = today_df[today_df[board_col].astype(float) == 2.0]
            skipped = len(today_df) - len(second_board_df)
            logger.debug(f"[二板定龙] 今日涨停{len(today_df)}只，连板数=2的{len(second_board_df)}只，跳过{skipped}只")

            # 板块首板助攻统计：今日各行业/概念的首板(连板数=1)家数（梯队厚度）
            def _sector_of(row, c):
                return ((stock_to_ths_industry or {}).get(c, '')
                        or (stock_to_ths_concept or {}).get(c, '')
                        or row.get('所属行业', '') or '未知')
            sector_first_board_count: Dict[str, int] = {}
            try:
                first_board_df = today_df[today_df[board_col].astype(float) == 1.0]
                for _, fb_row in first_board_df.iterrows():
                    fb_code = str(fb_row.get('代码', '')).zfill(6)
                    sec = _sector_of(fb_row, fb_code)
                    sector_first_board_count[sec] = sector_first_board_count.get(sec, 0) + 1
            except Exception:
                pass

            # 阶段一：收集所有"够格"的候选（先不直接定龙）
            candidates: List[Dict] = []

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

                if first_board_quality.get('score', 0) < min_fb_score:
                    logger.debug(f"[二板定龙]   {name} 过滤: 首板质量分{first_board_quality.get('score', 0)} < {min_fb_score}")
                    continue

                gap_ratio = self._calculate_gap_ratio(today_row, yest_row, today_date, yest_date)
                logger.debug(f"[二板定龙]   {name} 次日高开: {gap_ratio*100:.1f}%")

                if gap_ratio < cand_min_gap:
                    logger.debug(f"[二板定龙]   {name} 过滤: 高开{gap_ratio*100:.1f}% < {cand_min_gap*100:.0f}%")
                    continue

                limit_up_time = str(today_row.get('首次封板时间', '')).strip()
                is_fast_limit = self._is_fast_limit(limit_up_time, max_time="09:40:00")
                logger.debug(f"[二板定龙]   {name} 涨停时间={limit_up_time}, 是否快速={is_fast_limit}")

                # 涨停价：涨停池一般只给"最新价"（≈涨停价），fallback 到昨收×1.1
                entry_price = (today_row.get('涨停价', 0) or
                               today_row.get('最新价', 0) or
                               today_row.get('close', 0))
                if not entry_price or entry_price <= 0:
                    yest_close = yest_row.get('最新价', 0) or yest_row.get('close', 0) or yest_row.get('收盘价', 0)
                    if yest_close and yest_close > 0:
                        # 创业板/科创板 20%，其余 10%
                        rate = 0.20 if code.startswith(('30', '68', '8')) else 0.10
                        entry_price = round(float(yest_close) * (1 + rate), 2)

                # 获取封单金额，尝试多个可能的字段名
                seal_amount = (today_row.get('封单额', 0) or
                              today_row.get('封板资金', 0) or
                              today_row.get('封单金额', 0) or
                              today_row.get('封单量', 0) * entry_price)
                float_cap = today_row.get('流通市值', 1)
                seal_ratio = seal_amount / float_cap if float_cap > 0 else 0
                logger.debug(f"[二板定龙]   {name} 封单额={seal_amount}, 流通市值={float_cap}, 封单强度: {seal_ratio*100:.2f}%")

                confidence, conf_breakdown = self._second_board_confidence(
                    confidence_mode, first_board_quality, gap_ratio, is_fast_limit, seal_ratio
                )
                logger.debug(f"[二板定龙]   {name} 综合置信度={confidence:.2f} (mode={confidence_mode})")

                if confidence < cand_min_conf:
                    logger.debug(f"[二板定龙]   {name} 过滤: 置信度{confidence:.2f} < {cand_min_conf:.2f}")
                    continue

                sector = _sector_of(today_row, code)
                assist = sector_first_board_count.get(sector, 0)

                # 板块首板助攻硬门槛（独龙难飞，默认关闭）
                if min_sector_fb > 0 and assist < min_sector_fb:
                    logger.debug(f"[二板定龙]   {name} 过滤: 板块[{sector}]首板助攻{assist}<{min_sector_fb}")
                    continue

                # 横向强度分（连续、不饱和，用于全场排序定龙；区别于会顶格到0.95的置信度）
                rank_score = (
                    gap_ratio * 100 * w_gap
                    + first_board_quality.get('score', 0) * w_quality
                    + seal_ratio * 100 * w_seal
                    + (w_fast if is_fast_limit else 0.0)
                    + assist * w_sector
                )

                candidates.append({
                    'code': code, 'name': name, 'sector': sector,
                    'confidence': confidence, 'rank_score': rank_score,
                    'gap_ratio': gap_ratio, 'seal_ratio': seal_ratio,
                    'assist': assist, 'limit_up_time': limit_up_time,
                    'quality': first_board_quality, 'entry_price': entry_price,
                    'confidence_breakdown': conf_breakdown,
                })

            # ═══════════ 阶段二：横向竞争择优定龙 ═══════════
            # 1) 强度降序  2) 每板块最多 max_per_sector 只（去同质化）  3) 全场封顶 max_dragons
            candidates.sort(key=lambda c: (c['rank_score'], c['confidence']), reverse=True)
            sector_used: Dict[str, int] = {}
            selected: List[Dict] = []
            for c in candidates:
                if len(selected) >= max_dragons:
                    break
                used = sector_used.get(c['sector'], 0)
                if used >= max_per_sector:
                    logger.debug(f"[二板定龙]   {c['name']} 落选: 板块[{c['sector']}]已选{used}只(上限{max_per_sector})")
                    continue
                sector_used[c['sector']] = used + 1
                selected.append(c)

            for rank, c in enumerate(selected, 1):
                gap_ratio = c['gap_ratio']; seal_ratio = c['seal_ratio']
                limit_up_time = c['limit_up_time']; first_board_quality = c['quality']
                entry_price = c['entry_price']; confidence = c['confidence']
                is_solo = c['assist'] == 0
                signal = PatternSignal(
                    pattern_type="二板定龙",
                    stock_code=c['code'],
                    stock_name=c['name'],
                    confidence=round(confidence, 2),
                    description=(f"板块[{c['sector']}]最强二板（全场第{rank}），首板{first_board_quality.get('type', '硬板')}后，"
                                 f"次日高开{gap_ratio*100:.1f}%，{limit_up_time}涨停，二板定龙"),
                    key_metrics={
                        "首板类型": first_board_quality.get('type', ''),
                        "首板质量分": first_board_quality.get('score', 0),
                        "次日高开": f"{gap_ratio*100:.1f}%",
                        "涨停时间": limit_up_time,
                        "封单强度": f"{seal_ratio*100:.2f}%",
                        "板块助攻": f"{c['assist']}家首板",
                        "全场龙头排名": rank,
                        "强度分": round(c['rank_score'], 1),
                        "买点时机": "竞价" if gap_ratio >= 0.04 else "开盘"
                    },
                    entry_price=entry_price,
                    stop_loss=round(entry_price * 0.95, 2) if entry_price else 0,
                    take_profit=round(entry_price * 1.12, 2) if entry_price else 0,
                    # 真龙加冕：全场第一且有板块助攻 → heavy；独龙/靠后 → medium
                    position_size="heavy" if (rank == 1 and not is_solo and confidence >= 0.85) else "medium",
                    validation_rules=[
                        f"板块[{c['sector']}]最强二板（去同质化后唯一）",
                        f"次日高开{gap_ratio*100:.1f}%（资金表态）",
                        f"{limit_up_time}涨停（速度）",
                        f"板块助攻{c['assist']}家首板（梯队厚度）",
                        f"全场强度排名第{rank}",
                    ],
                    l2_industry=c['sector'] if c['sector'] != '未知' else ''
                )
                # Phase 3：扣分制下附上逐项扣分明细，便于复盘"为什么不是满分"
                if c.get('confidence_breakdown'):
                    signal.key_metrics["置信扣分明细"] = c['confidence_breakdown']
                signals.append(signal)
                total_passed += 1
                logger.info(f"[二板定龙]   ✓ 定龙#{rank} {c['name']}({c['code']}) "
                            f"强度{c['rank_score']:.1f} 置信{confidence:.2f} 板块[{c['sector']}]助攻{c['assist']}")

            logger.info(f"[二板定龙] 检测完成: 候选{len(candidates)}只 → 定龙{len(signals)}只 "
                        f"(检查{total_checked}只, 每日上限{max_dragons}, 每板块{max_per_sector})")

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
        if yest_close == 0 and self.repo and yest_date and code:
            try:
                daily_price = self.repo.get_daily_price(code, yest_date)
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

        # 计算竞价量比 = 竞价成交量 / 昨日全天成交量（两者均为「手」，同口径）。
        # 注意：竞价成交量已在 get_auction_data 内统一换算为「手」(与日线 vol 同口径)。
        auction_vol = float(auction.get('竞价成交量', 0) or 0) if auction else 0.0

        # 昨日全天成交量优先取日线 vol(手)，与竞价成交量口径一致；
        # 不能用「昨日涨停池行不存在就默认 1」的旧逻辑——那会把比值退化成「竞价成交量原值」。
        yest_vol = 0.0
        if self.repo and yest_date and code:
            try:
                ydf = self.repo.get_daily(code, yest_date, yest_date)
                if ydf is not None and not ydf.empty:
                    vcol = 'vol' if 'vol' in ydf.columns else ('volume' if 'volume' in ydf.columns else None)
                    if vcol:
                        yest_vol = float(ydf.iloc[-1].get(vcol, 0) or 0)
            except Exception as e:
                logger.debug(f"[_analyze_auction_data] {name}({code}) 取昨日成交量失败: {e}")
        # 兜底：昨日涨停池行成交量（口径可能不同，仅在无日线时使用）
        if yest_vol <= 0 and yesterday is not None:
            try:
                yest_vol = float(yesterday.get('成交量', 0) or 0)
            except Exception:
                yest_vol = 0.0

        auction_vol_ratio = (auction_vol / yest_vol) if (yest_vol > 0 and auction_vol > 0) else 0.0

        return {
            'gap': gap,
            'auction_vol_ratio': auction_vol_ratio,
            'auction_amount': float(auction.get('竞价成交额', 0) or 0) if auction else 0.0,
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
        
        # 如果涨停池数据中没有开盘价/收盘价，经只读仓库补取（Phase 1：原 self.dm.* 已迁移到 self.repo.*）
        if (today_open == 0 or yest_close == 0) and self.repo and today_date and yest_date:
            try:
                # 获取今日开盘价
                if today_open == 0:
                    today_price = self.repo.get_daily_price(code, today_date)
                    if today_price:
                        today_open = today_price.get('open', 0)
                        logger.debug(f"[_calculate_gap_ratio] {name} 补取今日开盘价: {today_open}")
                
                # 获取昨日收盘价（close，不是pre_close）
                if yest_close == 0:
                    yest_price = self.repo.get_daily_price(code, yest_date)
                    if yest_price:
                        yest_close = yest_price.get('close', 0)
                        logger.debug(f"[_calculate_gap_ratio] {name} 补取昨日收盘价: {yest_close}")
            except Exception as e:
                logger.debug(f"[_calculate_gap_ratio] {name} 补取价格失败: {e}")
        
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

    def _second_board_confidence(self, mode: str, quality: Dict, gap: float,
                                 is_fast: bool, seal_ratio: float):
        """
        Phase 3：按 confidence_mode 选择置信度算法。

        - "deduction"：统一满分扣分制（ConfidenceScorer + confidence_rules.yaml），
          返回 (value, breakdown_dict)，breakdown 写入信号 key_metrics 供复盘归因。
        - 其它（默认 "legacy"）：旧的"基础分+加分"逻辑，返回 (value, None)，行为不变。

        规则缺失或加载失败时自动回退 legacy。
        """
        if mode == "deduction":
            try:
                from core.scoring.confidence_scorer import get_scorer
                scorer = get_scorer("second_board_dragon")
                if scorer is not None:
                    res = scorer.score({
                        "seal_ratio": seal_ratio,
                        "gap_ratio": gap,
                        "first_board_score": quality.get('score', 0),
                        "is_fast": 1 if is_fast else 0,
                    })
                    return res.value, res.to_dict()
                logger.warning("[二板定龙] 扣分制规则缺失，回退 legacy 置信度")
            except Exception as e:
                logger.warning(f"[二板定龙] 扣分制计算失败，回退 legacy: {e}")
        return self._calculate_second_board_confidence(quality, gap, is_fast, seal_ratio), None

    def _calculate_second_board_confidence(self, quality: Dict, gap: float, 
                                            is_fast: bool, seal_ratio: float) -> float:
        """计算二板定龙置信度（legacy：基础分+阶梯加分，封顶0.95）"""
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
                                    hot_sectors: List[Dict] = None,
                                    all_hot_member_codes: set = None,
                                    stock_to_hot_sectors: Dict[str, list] = None,
                                    stock_to_ths_industry: Dict[str, str] = None,
                                    stock_to_ths_concept: Dict[str, str] = None,
                                    stock_to_ths_ts_code: Dict[str, str] = None) -> List[PatternSignal]:
        """
        首板突破模式识别 - 调用HotspotFirstBoardStrategy

        Args:
            hot_sectors: 预计算的热点板块列表（避免重复计算板块热度）
            all_hot_member_codes: 所有命中热点板块的股票代码集合
            stock_to_hot_sectors: 股票→所属热点板块名称列表
            stock_to_ths_industry: 股票→THS行业名称（从集中映射传入）
            stock_to_ths_concept: 股票→THS概念名称（从集中映射传入）
        """
        signals = []


        if self.first_board_breakout is None:
            logger.warning("[首板突破] 策略未加载")
            return signals

        if today_df.empty:
            return signals

        try:
            first_board_df = today_df[today_df['连板数'] == 1].copy()
            logger.info(f"[首板突破] 今日涨停{len(today_df)}只，首板{len(first_board_df)}只")

            if first_board_df.empty:
                logger.info("[首板突破] 没有首板股票，跳过检测")
                return signals

            history_pools_filtered = {}
            if history_pools:
                for date_key, pool_df in history_pools.items():
                    if date_key != date_str:
                        history_pools_filtered[date_key] = pool_df
            logger.debug(f"[首板突破] 过滤后历史池: {len(history_pools_filtered)}天 (排除今日)")
            trade_signals = self.first_board_breakout.detect_first_board_by_sectors(
                first_board_df, history_pools_filtered, date_str, hot_sectors=hot_sectors,
                all_hot_member_codes=all_hot_member_codes,
                stock_to_hot_sectors=stock_to_hot_sectors,
                stock_to_ths_industry=stock_to_ths_industry,
                stock_to_ths_concept=stock_to_ths_concept,
                stock_to_ths_ts_code=stock_to_ths_ts_code
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
            # 注意：loguru 不识别标准库的 exc_info 参数，需用 opt(exception=True) 才会记录堆栈
            logger.opt(exception=True).error(f"[首板突破] 检测失败: {e}")

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

            # Phase 3 切源：龙二波"前龙身份/调整期"改读生命周期注册中心。
            # registry 模式下：① 先用"触发前前龙枚举器"把今日涨停票里的前龙(调整窗内)
            # upsert 进注册中心龙二波域；② 候选 = 今日触发 ∩ 注册中心龙二波域(含交接带)。
            # 枚举器是龙二波 L1 身份门的超集（仅 rebuild + 距高点窗，不含调整质量），
            # 确认信号必在候选集内 → 切源对最终信号零 diff。
            from core.pattern.dragon_lifecycle import DRAGON_SECOND_WAVE_PHASES
            dsw_source = str(get_params("dragon_lifecycle").get("dsw_candidate_source", "registry"))
            registry_dsw_codes: Optional[set] = None
            hist_cache: Dict[str, Any] = {}
            if dsw_source == "registry":
                if self._lifecycle is not None:
                    hist_cache = self._enumerate_former_dragons_to_lifecycle(
                        today_df, recent_15d_pools, today_date
                    )
                    registry_dsw_codes = {
                        self._norm_code6(st.stock_code)
                        for st in self._lifecycle.query(DRAGON_SECOND_WAVE_PHASES, include_handoff=True)
                    }
                    logger.info(
                        f"[龙二波] 候选来源=注册中心：龙二波域(含交接带){len(registry_dsw_codes)}只前龙待今日确认"
                    )
                else:
                    logger.warning("[龙二波] 候选来源=注册中心，但生命周期未构建 → 回退遍历今日全市场")

            for _, today_row in today_df.iterrows():
                code_raw = str(today_row.get('代码', '')).strip().upper()
                if '.' in code_raw:
                    parts = code_raw.split('.')
                    code = parts[0].zfill(6) + '.' + parts[1]
                else:
                    code = code_raw.zfill(6)
                    code += '.SH' if (code.startswith('688') or code.startswith('6')) else '.SZ'
                name = today_row.get('名称', '')

                # registry 切源：不在注册中心龙二波域的今日票直接跳过（身份不符）
                if registry_dsw_codes is not None and self._norm_code6(code) not in registry_dsw_codes:
                    continue
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
                # 获取历史数据以启用双轨制判断（连板 或 涨幅）；优先复用枚举器缓存避免重复取数
                hist_data = hist_cache.get(code)
                if hist_data is None and code not in hist_cache:
                    try:
                        if hasattr(self.dm, 'get_stock_daily') and hasattr(self.dm, 'date_utils'):
                            # 获取近20日历史数据用于涨幅统计
                            # 使用 get_last_n_trade_dates 获取最近25个交易日
                            last_n_dates = self.repo.date_utils.get_last_n_trade_dates(25, today_date)
                            if last_n_dates:
                                start_date = last_n_dates[-1]  # 最早的一个
                                hist_data = self.repo.get_daily(code, start_date, today_date)
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
                    # Phase 3 union：把龙二波确认的前龙并入生命周期注册中心（SECOND_WAVE_READY），
                    # 让注册中心成为"前龙身份"的并集（弱转强池 ∪ 龙二波确认），供仲裁层/后续切源消费。
                    self._upsert_second_wave_to_lifecycle(trade_signal, stock_sector, today_date)
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

    def scan_all_patterns(self, today_date: str, yesterday_date: str = None, hot_sectors: List[Dict] = None,
                          market_emotion: str = "") -> Dict[str, List[PatternSignal]]:
        """
        扫描所有模式 - 主入口方法
        
        Args:
            today_date: 今日日期 (YYYY-MM-DD 或 YYYYMMDD)
            yesterday_date: 昨日日期 (YYYY-MM-DD 或 YYYYMMDD)，如为None则自动计算
            hot_sectors: 预计算的热点板块列表（避免重复计算）
            market_emotion: 当前市场情绪周期（Phase 5：仲裁择主路由/情绪闸门）；空=不路由
            
        Returns:
            Dict[str, List[PatternSignal]]: 各模式识别结果
        """
        self._market_emotion = str(market_emotion or "")
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
        today_df = self.repo.get_limit_up_pool(today_date_ymd)
        yesterday_df = self.repo.get_limit_up_pool(yesterday_date_ymd)
        
        # 获取前日数据（用于龙二波检测等，跳过周末）
        day_before_yesterday_ymd = self._get_prev_trading_day(yesterday_date_ymd, 1)
        day_before_yesterday_df = self.repo.get_limit_up_pool(day_before_yesterday_ymd)
        
        # 获取最近15个交易日涨停池数据（用于龙二波检测，基于交易日历）
        logger.info("获取最近15个交易日涨停池数据...")
        from core.utils.date_utils import get_last_n_trade_dates
        recent_trade_dates = get_last_n_trade_dates(16, today_date_ymd)
        recent_trade_dates = [d for d in recent_trade_dates if d != today_date_ymd][:15]

        recent_zt_pools = {}
        for i, date_str_ymd in enumerate(recent_trade_dates, 1):
            df = self.repo.get_limit_up_pool(date_str_ymd)
            if not df.empty:
                recent_zt_pools[date_str_ymd] = df
                logger.debug(f"  [{i}] {date_str_ymd}: {len(df)}只涨停")
            else:
                logger.debug(f"  [{i}] {date_str_ymd}: 无数据或空数据")
        
        logger.info(f"今日涨停: {len(today_df)}只, 昨日涨停: {len(yesterday_df)}只, 前日涨停: {len(day_before_yesterday_df)}只, 历史池: {len(recent_zt_pools)}天")
        
        # 获取今日全市场日线数据（用于更新走弱池价格和技术评分）
        # 使用 all_stocks_daily (daily 接口) 而非 daily_basic，因为后者缺少 open/vol 等列
        logger.info("获取今日全市场日线数据...")
        try:
            today_daily = self.repo.get_all_stocks_daily(today_date_ymd)
            logger.info(f"获取到全市场日线数据: {len(today_daily)}只股票")
        except Exception as e:
            logger.warning(f"获取全市场日线数据失败: {e}")
            today_daily = None
        
        results = {}
        
        # ============================================================
        # 构建 stock→板块归属映射（新的倒推逻辑）
        #
        # 旧逻辑：遍历热点的 member_codes → 纯热点股票才有映射（覆盖面窄）
        # 新逻辑：用 member 接口反查每只股票的所有板块 → 全覆盖
        #
        # 流程：
        #   1. 收集所有需要查询的股票代码（涨停池 + 龙头池）
        #   2. dm.get_stock_sectors_batch() → 每只股票 → 所有板块 ts_code
        #   3. 根据 ths_index.type 分类（N=概念, I=行业）
        #   4. 交叉匹配热点板块名称集合 → 判定命中热点
        # ============================================================
        stock_to_ths_industry = {}    # stock_code → THS行业名称
        stock_to_ths_concept = {}     # stock_code → THS概念名称
        stock_to_ths_ts_code = {}     # stock_code → THS板块代码 (用于显示)
        all_hot_member_codes = set()  # 所有命中热点的股票代码集合
        stock_to_hot_sectors = {}     # stock_code → [热点板块名称列表]

        # 收集所有需要查询板块归属的股票代码（带后缀）
        all_stock_codes_with_suffix = set()

        def _collect_codes_with_suffix(pool_df, code_col='代码'):
            """从涨停池收集代码（保留后缀）"""
            if pool_df is not None and not pool_df.empty and code_col in pool_df.columns:
                codes = pool_df[code_col].astype(str).str.strip().str.upper().tolist()
                all_stock_codes_with_suffix.update(c for c in codes if c)

        _collect_codes_with_suffix(today_df)
        _collect_codes_with_suffix(yesterday_df)
        _collect_codes_with_suffix(day_before_yesterday_df)
        for pool_df in recent_zt_pools.values():
            _collect_codes_with_suffix(pool_df)

        # 从龙头池收集
        if self.weak_to_strong:
            for code in self.weak_to_strong.dragon_pool.keys():
                all_stock_codes_with_suffix.add(code)
            for code in self.weak_to_strong.weakening_pool.keys():
                all_stock_codes_with_suffix.add(code)

        # 构建热点板块名称集合（用于快速匹配）
        hot_sector_names = set()
        if hot_sectors:
            for hs in hot_sectors:
                hot_sector_names.add(hs.get('sector_name', ''))

        logger.info(f"[模式识别] 收集到 {len(all_stock_codes_with_suffix)} 只股票待查询板块归属, "
                   f"热点板块 {len(hot_sector_names)} 个")

        def _is_market_attribute_sector(sector_name: str, sector_type: str) -> bool:
            """
            判断是否为市场属性板块（融资融券/深股通/沪股通等），非实际业务概念/行业
            这些板块是股票的二级市场属性标签，不应作为行业或概念归属
            """
            if not sector_name:
                return False
            excluded_keywords = [
                '融资融券', '深股通', '沪股通', '转融券', '转融通',
                '标普道琼斯', '富时罗素', 'MSCI', '罗素',
                '创业板综', '深证成指', '上证综指',
                'QFII', 'RQFII', '陆股通',
            ]
            for kw in excluded_keywords:
                if kw in sector_name:
                    return True
            return False

        # 批量查询板块归属
        if all_stock_codes_with_suffix:
            try:
                stock_sectors_map = self.repo.get_stock_sectors_batch(
                    list(all_stock_codes_with_suffix)
                )
            except Exception as e:
                logger.warning(f"[模式识别] 批量查询板块归属失败: {e}")
                stock_sectors_map = {}

            for stock_code, sectors_df in stock_sectors_map.items():
                if sectors_df.empty:
                    continue

                for _, sector_row in sectors_df.iterrows():
                    sector_type_raw = str(sector_row.get('type', '')).strip()
                    sector_name = str(sector_row.get('name', '')).strip()
                    sector_ts_code = str(sector_row.get('ts_code', '')).strip()

                    if not sector_name:
                        continue

                    # 过滤市场属性板块（非实际业务概念/行业）
                    if _is_market_attribute_sector(sector_name, sector_type_raw):
                        continue

                    # 分类：N=概念, I=行业
                    if sector_type_raw == 'N' or sector_type_raw.startswith('概念'):
                        if stock_code not in stock_to_ths_concept:
                            stock_to_ths_concept[stock_code] = sector_name
                            stock_to_ths_ts_code[stock_code] = sector_ts_code
                    elif sector_type_raw == 'I' or sector_type_raw.startswith('行业'):
                        if stock_code not in stock_to_ths_industry:
                            stock_to_ths_industry[stock_code] = sector_name
                            stock_to_ths_ts_code[stock_code] = sector_ts_code

                    # 判断是否命中热点板块
                    if hot_sector_names and sector_name in hot_sector_names:
                        if stock_code not in stock_to_hot_sectors:
                            stock_to_hot_sectors[stock_code] = []
                        if sector_name not in stock_to_hot_sectors[stock_code]:
                            stock_to_hot_sectors[stock_code].append(sector_name)
                        all_hot_member_codes.add(stock_code)

            logger.info(f"[模式识别] THS行业映射: {len(stock_to_ths_industry)}只, "
                       f"THS概念映射: {len(stock_to_ths_concept)}只, "
                       f"命中热点板块股票: {len(all_hot_member_codes)}只, "
                       f"有热点归属的股票: {len(stock_to_hot_sectors)}只")
        else:
            logger.warning("[模式识别] 无股票代码需要查询板块归属")

        if not hot_sectors:
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
                hot_sectors=hot_sectors,
                all_hot_member_codes=all_hot_member_codes,
                stock_to_hot_sectors=stock_to_hot_sectors,
                stock_to_ths_industry=stock_to_ths_industry,
                stock_to_ths_concept=stock_to_ths_concept,
                stock_to_ths_ts_code=stock_to_ths_ts_code
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

        # Phase 4：跨策略信号仲裁（同票多策略择主/去重/协同）。默认 annotate 仅标注，零 diff。
        results = self._apply_arbitration(results)

        return results

    def _apply_arbitration(self, results: Dict[str, List[PatternSignal]]) -> Dict[str, List[PatternSignal]]:
        """运行跨策略仲裁并把结论作用到信号集合（受 config ``arbitration`` 控制）。"""
        cfg = get_params("arbitration") or {}
        if not cfg.get("enabled", True):
            self._log_cross_strategy_overlap(results)
            return results
        try:
            from core.pattern import signal_arbitrator as arb_mod
            emotion = getattr(self, "_market_emotion", "") or ""
            # Phase 5：情绪闸门（默认空表→零 diff），在仲裁前整体抑制指定策略
            results = arb_mod.gate_by_emotion(results, emotion, cfg)
            arb = arb_mod.arbitrate(results, cfg=cfg, emotion=emotion)
            overlaps = arb.overlaps
            if overlaps:
                logger.info(f"[仲裁] {len(overlaps)} 只标的多策略命中，择主/标注：")
                for c6, d in overlaps.items():
                    tag = "共振" if d.is_resonance else ""
                    logger.info(f"  {d.name}({c6}): {d.all_patterns} → 主[{d.primary_pattern}]"
                                f"{('，抑制' + str(d.suppressed_patterns)) if d.suppressed_patterns else ''} {tag}")
                wts_dsw = [d.name or c6 for c6, d in overlaps.items()
                           if "弱转强" in d.all_patterns and "龙二波" in d.all_patterns]
                if wts_dsw:
                    logger.warning(f"[仲裁] 弱转强∩龙二波 边界争议 {len(wts_dsw)} 只：{wts_dsw}（已择主裁决）")
            mode = str(cfg.get("mode", "annotate"))
            results = arb_mod.apply(results, arb, mode=mode)
            if mode != "annotate":
                logger.info(f"[仲裁] 作用模式={mode}（共振加权/去重已生效）")
        except Exception as e:
            logger.error(f"[仲裁] 执行失败，回退原始信号: {e}")
        return results

    @staticmethod
    def _norm_code6(code: str) -> str:
        """归一为 6 位代码前缀，便于跨策略（带/不带后缀）匹配。"""
        s = str(code).strip().upper()
        return s.split('.')[0].zfill(6) if s else s

    def _enumerate_former_dragons_to_lifecycle(self, today_df: Any, recent_pools: Dict[str, Any],
                                               today_date: str) -> Dict[str, Any]:
        """触发前前龙枚举器（Phase 3 切源核心）。

        对**今日涨停票**复用龙二波自身的"第一波身份重建"（`_rebuild_consecutive_from_pools`，
        双轨制：连板/涨幅/涨停次数）+ 距高点调整窗，把识别为"前龙且处于调整窗"的票
        upsert 进生命周期注册中心龙二波域（SECOND_WAVE_READY）。

        - **单一事实来源**：龙二波候选身份自此由注册中心统一提供，不再各自重建。
        - **零 diff 保证**：本枚举只用 L1 身份门的**超集**（rebuild + 距高点窗，不含调整质量
          `_get_adjust_period`），确认信号必落在候选集内；其余由下游 `detect_second_wave` 过滤。
        - 与弱转强域重叠时不覆盖其阶段，仅标记 `both_eligible`（交接带，供仲裁层裁决）。

        Returns:
            hist_cache：{带后缀代码: 日线DataFrame}，供主循环复用，避免重复取数。
        """
        hist_cache: Dict[str, Any] = {}
        if self._lifecycle is None or self.dragon_second_wave is None or today_df is None or today_df.empty:
            return hist_cache
        from core.pattern.dragon_lifecycle import DragonState, DragonPhase, DRAGON_SECOND_WAVE_PHASES
        p = getattr(self.dragon_second_wave, "params", {}) or {}
        min_adj = int(p.get("min_adjust_days", 2))
        max_adj = int(p.get("max_adjust_days", 15))

        enum_count = 0
        for _, row in today_df.iterrows():
            code_raw = str(row.get('代码', '')).strip().upper()
            if '.' in code_raw:
                parts = code_raw.split('.')
                code = parts[0].zfill(6) + '.' + parts[1]
            else:
                code = code_raw.zfill(6)
                code += '.SH' if (code.startswith('688') or code.startswith('6')) else '.SZ'
            code6 = self._norm_code6(code)

            # 取历史日线（双轨制需要；缓存供主循环复用）
            hist_data = None
            try:
                if hasattr(self.dm, 'get_stock_daily') and hasattr(self.dm, 'date_utils'):
                    last_n = self.repo.date_utils.get_last_n_trade_dates(25, today_date)
                    if last_n:
                        hist_data = self.repo.get_daily(code, last_n[-1], today_date)
            except Exception:
                hist_data = None
            hist_cache[code] = hist_data

            try:
                rec = self.dragon_second_wave._rebuild_consecutive_from_pools(code6, recent_pools, hist_data)
            except Exception:
                continue
            if not rec.get('is_valid'):
                continue
            fw = rec.get('first_wave', {}) or {}
            peak_date = fw.get('peak_date', '')
            try:
                dsp = self.dragon_second_wave._calculate_days_since_peak(peak_date, today_date)
            except Exception:
                continue
            # 距高点调整窗（与 detect_second_wave L1a 一致的上下界，留 +5 余量保持超集）
            if not (min_adj <= dsp <= max_adj + 5):
                continue

            existing = self._lifecycle.get(code)
            if existing is not None and existing.phase not in DRAGON_SECOND_WAVE_PHASES:
                # 已属弱转强/龙头域：不覆盖，仅标记交接带（既是走弱观察、又是前龙反包）
                existing.both_eligible = True
                enum_count += 1
                continue
            st = DragonState(
                stock_code=code,
                stock_name=str(row.get('名称', '')),
                dragon_type=str(fw.get('wave_type', '')),
                peak_height=int(fw.get('max_boards', 0) or 0),
                peak_date=peak_date,
                days_since_peak=dsp,
                phase=DragonPhase.SECOND_WAVE_READY,
                source="dragon_second_wave_enum",
            )
            self._lifecycle.upsert(st)
            enum_count += 1

        logger.info(f"[龙二波] 触发前前龙枚举：{enum_count} 只今日票识别为前龙(调整窗内)并入注册中心")
        return hist_cache

    def _upsert_second_wave_to_lifecycle(self, trade_signal: Any, sector: str, today_date: str) -> None:
        """把龙二波确认的前龙并入生命周期注册中心（SECOND_WAVE_READY）。

        这是 Phase 3「身份并集」的落地：注册中心此前仅由弱转强走弱池构建，龙二波域为空；
        本方法让其纳入龙二波自有的前龙身份重建结果，成为前龙身份的并集来源。
        仅写注册中心，不改任何信号输出（零 diff）。
        """
        if self._lifecycle is None:
            return
        try:
            from core.pattern.dragon_lifecycle import DragonState, DragonPhase
            km = getattr(trade_signal, "key_metrics", {}) or {}
            # 解析见顶日（"第一波日期" 形如 "起涨日至见顶日"）
            peak_date = ""
            fw = str(km.get("第一波日期", ""))
            if "至" in fw:
                peak_date = fw.split("至")[-1].strip()
            # 解析调整深度（"23.5%" → 0.235）
            depth = 0.0
            try:
                depth = float(str(km.get("调整深度", "0")).replace("%", "")) / 100.0
            except Exception:
                depth = 0.0
            adjust_days = int(km.get("调整天数", 0) or 0)
            code = str(getattr(trade_signal, "stock_code", ""))
            st = DragonState(
                stock_code=code,
                stock_name=getattr(trade_signal, "stock_name", ""),
                dragon_type=str(km.get("第一波类型", "")),
                sector_name=sector,
                peak_date=peak_date,
                max_drawdown=depth,
                days_since_peak=adjust_days,
                phase=DragonPhase.SECOND_WAVE_READY,
                source="dragon_second_wave",
                extra={"key_metrics": km},
            )
            self._lifecycle.upsert(st)
        except Exception as e:
            logger.debug(f"[龙二波] 并入生命周期注册中心失败: {e}")

    def _log_cross_strategy_overlap(self, results: Dict[str, List[PatternSignal]]) -> Dict[str, List[str]]:
        """统计同一标的被多个策略同时命中的情况（跨策略重叠）。

        这是"系统互补性"的客观度量，也是 Phase 4 信号仲裁层去重/协同的输入：
        - 同股多策略 → 潜在冲突（需仲裁取舍）或共振（可加权）；
        - 弱转强 ∩ 龙二波 重叠尤其关键：二者本应按生命周期窗错位互补，重叠即边界争议。

        仅记录日志、返回映射，**不修改任何信号**。
        """
        code_to_patterns: Dict[str, List[str]] = {}
        code_to_name: Dict[str, str] = {}
        for ptype, signals in results.items():
            for s in (signals or []):
                c6 = self._norm_code6(getattr(s, "stock_code", ""))
                if not c6:
                    continue
                code_to_patterns.setdefault(c6, [])
                if ptype not in code_to_patterns[c6]:
                    code_to_patterns[c6].append(ptype)
                code_to_name.setdefault(c6, getattr(s, "stock_name", ""))

        overlaps = {c: ps for c, ps in code_to_patterns.items() if len(ps) > 1}
        if overlaps:
            logger.info(f"[跨策略重叠] {len(overlaps)} 只标的被多策略同时命中：")
            for c, ps in overlaps.items():
                logger.info(f"  {code_to_name.get(c, '')}({c}): {' + '.join(ps)}")
        # 弱转强 ∩ 龙二波 专项（生命周期窗本应错位，重叠=边界争议，重点观测）
        wts_dsw = [c for c, ps in overlaps.items() if "弱转强" in ps and "龙二波" in ps]
        if wts_dsw:
            logger.warning(
                f"[跨策略重叠] 弱转强∩龙二波 边界争议 {len(wts_dsw)} 只："
                f"{[code_to_name.get(c, c) for c in wts_dsw]}（待 Phase 4 仲裁/生命周期窗收敛）"
            )
        return overlaps
    
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
        - 入池日期为当天的股票（当天刚走弱）不检测转强（→ 生命周期 FLASH_RECOVERY 单独处理）

        Phase 2b：候选来源改为**龙头生命周期注册中心**的弱转强信号域阶段
        （``WEAKENING`` / ``WATCHING``）——它已统一处理"排除当天入池、观察期≤N日、
        回调≤上限"三项。本函数仅在其上再叠加注册中心未覆盖的口径：

        - 回调上限 0.25 兜底闸（注册中心按 0.20 预筛，主路径 L1a 同样按 0.20 收紧，
          最终信号集合不变）；
        - 成交量连续萎缩（<5日均量50%）过滤（依赖 today_daily，注册中心不持有日线）。

        Returns:
            符合条件的股票代码列表
        """
        valid_codes = []

        # 候选来源：生命周期注册中心的弱转强信号域（不含 FLASH_RECOVERY，后者单独走 P4）
        if self._lifecycle is None:
            logger.warning("[走弱池筛选] 生命周期未构建，本日不纳入历史走弱观察")
            return valid_codes
        states = self._lifecycle.query(
            (DragonPhase.WEAKENING, DragonPhase.WATCHING), include_handoff=False
        )

        for st in states:
            code = st.stock_code
            weakening = self.weak_to_strong.weakening_pool.get(code)
            if weakening is None:
                continue
            observation_days = st.days_since_weakening

            # 回调上限兜底（注册中心已按 0.20 预筛，此处保留 0.25 闸口语义）
            drawdown = weakening.max_drawdown
            if drawdown > 0.25:
                logger.debug(f"[走弱池筛选] {weakening.stock_name} 回调过大({drawdown*100:.1f}% > 25%)")
                continue

            # 成交量连续萎缩过滤（注册中心未覆盖；vol/volume列兼容保护）
            if today_daily is not None and not today_daily.empty:
                stock_daily = today_daily[today_daily['ts_code'].str.contains(code)]
                if not stock_daily.empty:
                    vol_col = 'vol' if 'vol' in stock_daily.columns else ('volume' if 'volume' in stock_daily.columns else None)
                    if vol_col and len(stock_daily) >= 3:
                        vol_vals = stock_daily[vol_col].astype(float)
                        avg_volume_5d = vol_vals.tail(5).mean()
                        today_volume = vol_vals.iloc[-1]
                        if today_volume < avg_volume_5d * 0.5 and avg_volume_5d > 0:
                            logger.debug(f"[走弱池筛选] {weakening.stock_name} 成交量萎缩(<50%)")
                            continue

            valid_codes.append(code)
            logger.info(f"[走弱池筛选] ✓ {weakening.stock_name}({code}) 符合条件，入池{observation_days}天，回调{drawdown*100:.1f}%")

        logger.info(f"[走弱池筛选] 共{len(self.weak_to_strong.weakening_pool)}只，历史走弱{len(valid_codes)}只纳入观察")
        return valid_codes
    
    def _calculate_technical_score(self, stock_code: str, date_str: str, today_daily: pd.DataFrame = None) -> int:
        """
        计算技术形态维度评分（满分20分；L4 总分里按 0–20 计入）

        评分标准：
        - 收盘价站上5日均线: +8分
        - 5日均线拐头向上: +5分
        - 成交量突破5日均量1.5倍: +5分
        - 收盘价高于开盘价（阳线）: +2分

        依赖个股近 25 自然日日线（经 repo 取数）。若该股不在预取 universe 且盘后日线
        暂未取到，daily 窗口会为空 → 本维度返回 0（属数据缺失，非「技术形态差」）。
        """
        score = 0

        if not self.repo:
            return 0

        try:
            start_date = (datetime.strptime(date_str, "%Y%m%d") - timedelta(days=25)).strftime("%Y%m%d")
            daily_df = self.repo.get_daily(stock_code, start_date, date_str)
            if daily_df is None or daily_df.empty or len(daily_df) < 5:
                logger.debug(f"[技术评分] {stock_code} 日线不足({0 if daily_df is None else len(daily_df)}根<5)，"
                             f"该维度计 0（数据缺失，非形态差）")
                return 0

            df = daily_df.sort_values('trade_date')
            df['close'] = df['close'].astype(float)
            df['ma5'] = df['close'].rolling(window=5).mean()

            wts = self.wts_scoring
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest

            # 收盘价站上5日均线
            if pd.notna(latest['ma5']) and latest['close'] > latest['ma5']:
                score += wts.get('tech_above_ma5_pts', 8)

            # 5日均线拐头向上
            if pd.notna(latest['ma5']) and pd.notna(prev['ma5']) and latest['ma5'] > prev['ma5']:
                score += wts.get('tech_ma5_turnup_pts', 5)

            # 成交量突破（volume列可能名为vol或volume）
            vol_col = 'vol' if 'vol' in df.columns else ('volume' if 'volume' in df.columns else None)
            if vol_col:
                df[vol_col] = df[vol_col].astype(float)
                avg_volume_5d = df[vol_col].tail(5).mean()
                if latest[vol_col] > avg_volume_5d * wts.get('tech_vol_breakout_ratio', 1.5):
                    score += wts.get('tech_vol_breakout_pts', 5)

            # 收盘价高于开盘价（阳线）
            if 'open' in df.columns:
                today_close = float(latest['close'])
                today_open = float(latest['open'])
                if today_close > today_open:
                    score += wts.get('tech_yang_pts', 2)
        except Exception as e:
            logger.debug(f"[技术评分] {stock_code} 计算失败: {e}")

        return score
    
    def _calculate_capital_score(self, stock_code: str, date_str: str) -> int:
        """
        计算资金流入维度评分（满分25分）

        数据源：``dm.get_moneyflow`` → Tushare ``moneyflow``（DataFrame，金额单位「万元」）。
        关键列：net_mf_amount(主力净流入)、buy/sell_lg_amount(大单)、buy/sell_elg_amount(特大单)。

        评分标准：
        - 主力净流入为正(net_mf_amount>0): +10分
        - 大单+特大单净流入占当日总成交额比 ≥20%: +10分（≥10%: +5分）
        - 最近 3 个交易日主力净流入为正 ≥2 天（持续流入）: +5分
        """
        score = 0

        # 历史口径：曾错误调用不存在的 dm.get_moneyflow_data 且按 dict 解析，导致本维度恒为 0。
        if not (self.dm and hasattr(self.dm, 'get_moneyflow')):
            return 0

        try:
            wts = self.wts_scoring
            lookback = int(wts.get('capital_lookback_days', 3))
            # 一次取最近 N 个交易日（含当日），既算当日强度，又判断持续性
            df = self.dm.get_moneyflow(stock_code, trade_date=date_str, lookback_days=lookback)
            if df is None or df.empty:
                return 0
            if 'trade_date' in df.columns:
                df = df.sort_values('trade_date')
            row = df.iloc[-1]  # 当日

            def _f(container, key: str) -> float:
                try:
                    return float(container.get(key, 0) or 0)
                except Exception:
                    return 0.0

            # 1) 主力净流入为正
            net_mf = _f(row, 'net_mf_amount')
            if net_mf > 0:
                score += wts.get('capital_net_inflow_pts', 10)

            # 2) 大单(含特大单)净流入占当日总成交额比
            big_net = (_f(row, 'buy_lg_amount') + _f(row, 'buy_elg_amount')) \
                      - (_f(row, 'sell_lg_amount') + _f(row, 'sell_elg_amount'))
            total_amt = sum(_f(row, c) for c in (
                'buy_sm_amount', 'sell_sm_amount', 'buy_md_amount', 'sell_md_amount',
                'buy_lg_amount', 'sell_lg_amount', 'buy_elg_amount', 'sell_elg_amount'))
            big_ratio = (big_net / total_amt) if total_amt > 0 else 0.0
            if big_ratio >= wts.get('capital_big_ratio_high', 0.20):
                score += wts.get('capital_big_ratio_high_pts', 10)
            elif big_ratio >= wts.get('capital_big_ratio_mid', 0.10):
                score += wts.get('capital_big_ratio_mid_pts', 5)

            # 3) 持续流入：近 N 日主力净流入为正 ≥ capital_persist_days 天
            if 'net_mf_amount' in df.columns:
                pos_days = int((pd.to_numeric(df['net_mf_amount'], errors='coerce') > 0).sum())
                if pos_days >= int(wts.get('capital_persist_days', 2)):
                    score += wts.get('capital_persist_pts', 5)
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
        wts = self.wts_scoring

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
                
                if sector_limit_up_count >= wts.get('sector_limit_up_high', 3):
                    score += wts.get('sector_limit_up_high_pts', 10)
                elif sector_limit_up_count >= wts.get('sector_limit_up_low', 1):
                    score += wts.get('sector_limit_up_low_pts', 5)
            
            # 获取板块涨幅（如果有板块数据）
            if self.dm and hasattr(self.dm, 'get_sector_daily'):
                sector_data = self.repo.get_sector_daily(sector_name, date_str)
                if sector_data:
                    sector_change = sector_data.get('change_pct', 0)
                    if sector_change >= wts.get('sector_change_high', 2.0):
                        score += wts.get('sector_change_high_pts', 10)
                    elif sector_change >= wts.get('sector_change_low', 1.0):
                        score += wts.get('sector_change_low_pts', 5)
        except Exception as e:
            logger.debug(f"[情绪评分] {sector_name} 计算失败: {e}")
        
        return score