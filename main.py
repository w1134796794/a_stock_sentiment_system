"""
A股短线情绪量化系统 - 主程序入口
整合所有模块，提供CLI交互
"""
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
import pandas as pd
import numpy as np
import loguru

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    TUSHARE_TOKEN, CACHE_DIR, OUTPUT_DIR, 
    INDUSTRY_MAPPING_FILE, TRADE_HOUR, TRADE_MINUTE
)
from core.data.data_manager import DataManager
from core.data.industry_mapper import IndustryMapper
from core.analysis.pattern_recognition import PatternRecognition
from core.analysis.emotion_cycle_engine import EmotionCycleEngine
# 使用新版同花顺板块追踪器
from core.analysis.ths_sector_tracker import THSSectorTracker as SectorRotationTracker
from core.report.report_generator_v2 import ReportGeneratorV2
from core.execution.execution_engine import UnifiedExecutionEngine
from core.execution.retail_trader_support_v2 import RetailTraderSupportV2
from core.utils import DateUtils

logger = loguru.logger

class SentimentSystem:
    def __init__(self):
        self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        self.mapper = IndustryMapper(INDUSTRY_MAPPING_FILE)
        self.emotion_engine = EmotionCycleEngine(dm=self.dm)  # 情绪周期引擎，传入DataManager用于计算溢价率
        self.sector_tracker = SectorRotationTracker(self.dm)  # 板块轮动追踪器
        self.reporter = ReportGeneratorV2(OUTPUT_DIR)
        self.execution_engine = None  # 延迟初始化
        self.retail_support = None  # 散户支持模块延迟初始化
        self.today = datetime.now().strftime("%Y%m%d")
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        
    def run_daily_analysis(self, date: str = None):
        """
        执行每日完整分析流程
        支持非交易日自动关联最近交易日
        """
        if date is None:
            date = self.today
        
        # 使用DateUtils获取最近交易日（自动处理非交易日）
        date_utils = DateUtils()
        date = date_utils.get_nearest_trade_date(date)
        
        # 获取上一个交易日
        self.yesterday = date_utils.get_prev_trade_date(date)
        
        logger.info(f"开始执行 {date} 的日度分析...")
        logger.info(f"对比日期: {self.yesterday}")
        
        # 1. 数据获取
        logger.info("[1/5] 获取涨停池数据...")

        zt_pool = self.dm.get_limit_up_pool(date)
        if zt_pool.empty:
            logger.warning(f"未获取到 {date} 的涨停数据，可能非交易日")
            return

        logger.info(f"获取到 {len(zt_pool)} 只涨停股票")
        
        # 2. 构建层级结构
        logger.info("[2/5] 构建行业层级映射...")
        hierarchy_df = self.mapper.build_hierarchy_dataframe(zt_pool)
        if not hierarchy_df.empty:
            logger.info(f"层级映射完成，覆盖 {hierarchy_df['L2_Industry'].nunique()} 个二级行业")
        
        # 3. 情绪周期分析
        logger.info("[3/6] 分析情绪周期...")
        
        # 获取跌停数据（用于计算核按钮）
        limit_down_df = self.dm.get_limit_down_pool(date)
        
        # 获取昨日涨停数据（用于计算溢价）
        prev_limit_up_df = self.dm.get_limit_up_pool(self.yesterday)
        
        # 使用情绪周期引擎分析
        emotion_result = self.emotion_engine.analyze_market_data(
            limit_up_df=zt_pool,
            limit_down_df=limit_down_df,
            prev_limit_up_df=prev_limit_up_df
        )
        
        # ========== DEBUG: 情绪周期详细数据 ==========
        logger.info("=" * 60)
        logger.info("【DEBUG】情绪周期分析详细数据")
        logger.info("=" * 60)
        logger.info(f"情绪周期: {emotion_result['cycle_name']}")
        logger.info(f"策略建议: {emotion_result['strategy'].strategy}")
        logger.info(f"仓位控制: {emotion_result['strategy'].position}")
        logger.info(f"禁忌操作: {emotion_result['strategy'].forbidden_actions}")
        
        # 打印原始数据 (从metrics字段获取)
        metrics = emotion_result.get('metrics', {})
        logger.info("\n【DEBUG】原始统计数据:")
        logger.info(f"  涨停家数: {metrics.get('limit_up_count', 'N/A')}")
        logger.info(f"  跌停家数: {metrics.get('nuclear_button_count', 'N/A')}")
        logger.info(f"  炸板率: {metrics.get('broken_rate', 'N/A')}%")
        logger.info(f"  最高连板: {metrics.get('max_board_height', 'N/A')}板")
        
        # 昨日涨停表现（基于开盘价）
        logger.info("\n【DEBUG】昨日涨停今日开盘表现:")
        logger.info(f"  平均溢价率: {metrics.get('prev_limit_up_premium', 'N/A')}%")
        logger.info(f"  开盘卖出胜率: {metrics.get('win_rate', 'N/A')}%")
        logger.info(f"  平均赢面: {metrics.get('avg_profit', 'N/A')}%")
        logger.info("  (说明: 昨日涨停股票，今日开盘卖出的统计)")
        
        # 连板分布
        board_distribution = metrics.get('board_distribution', {})
        if board_distribution:
            logger.info(f"\n【DEBUG】连板分布: {board_distribution}")
            # 计算连板家数和首板家数
            consecutive_count = sum(v for k, v in board_distribution.items() if k >= 2)
            first_board_count = board_distribution.get(1, 0)
            logger.info(f"  连板家数: {consecutive_count}")
            logger.info(f"  首板家数: {first_board_count}")
        
        # 打印评分详情 (从scores字段获取)
        scores = emotion_result.get('scores', {})
        logger.info("\n【DEBUG】情绪周期评分详情:")
        if scores:
            logger.info(f"  高潮期(boom)得分: {scores.get('boom', 'N/A')}")
            logger.info(f"  上升期(rise)得分: {scores.get('rise', 'N/A')}")
            logger.info(f"  震荡期(shake)得分: {scores.get('shake', 'N/A')}")
            logger.info(f"  退潮期(decline)得分: {scores.get('decline', 'N/A')}")
            logger.info(f"  冰点期(freeze)得分: {scores.get('freeze', 'N/A')}")
        else:
            logger.info("  暂无评分数据")
        logger.info("=" * 60)
        
        # 4. 主线板块分析（使用带交叉验证的新方法）
        logger.info("[4/6] 计算主线板块强度...(剔除ST板块)")
        # 获取limit_cpt_list数据用于概念分析
        limit_cpt_df = self.dm.get_limit_cpt_list(date)
        
        # ========== DEBUG: 原始概念板块数据 ==========
        logger.info("=" * 60)
        logger.info("【DEBUG】原始概念板块数据 (limit_cpt_list)")
        logger.info("=" * 60)
        if not limit_cpt_df.empty:
            logger.info(f"获取到 {len(limit_cpt_df)} 个概念板块")
            for i, row in limit_cpt_df.head(5).iterrows():
                name = row.get('name', row.get('concept', 'N/A'))
                up_nums = row.get('up_nums', 'N/A')
                cons_nums = row.get('cons_nums', 'N/A')
                rank = row.get('rank', i+1)
                logger.info(f"  排名{rank}: {name} (涨停{up_nums}家, 连板{cons_nums}家)")
        else:
            logger.info("  未获取到概念板块数据")
        logger.info("=" * 60)
        
        # 使用带交叉验证的分析方法
        # 先获取热门行业数据（用于判断概念-行业共振）
        hot_industries = self.sector_tracker.get_hot_industries_from_sectors(top_n=5)
        if hot_industries:
            logger.info(f"✓ 识别到热门行业: {hot_industries}")
        else:
            logger.info("⚠ 未能识别热门行业，信号类型可能默认为'弱势'")
        
        mainline_df = self.sector_tracker.analyze_with_validation(
            date, top_n=10, hot_industries=hot_industries
        )
        
        # ========== DEBUG: 热点主线详细数据 ==========
        logger.info("=" * 60)
        logger.info("【DEBUG】热点主线分析详细数据 (带交叉验证)")
        logger.info("=" * 60)
        if not mainline_df.empty:
            logger.info(f"✓ 分析完成，共 {len(mainline_df)} 个板块\n")
            for i, row in mainline_df.iterrows():
                logger.info(f"【Top{i+1}】{row['板块名称']}")
                logger.info(f"  基础数据:")
                logger.info(f"    - 当前排名: {row['当前排名']}")
                logger.info(f"    - 涨停家数: {row['涨停家数']}")
                logger.info(f"    - 综合评分: {row['综合评分']}")
                logger.info(f"    - 所处阶段: {row['所处阶段']}")
                logger.info(f"    - 市场周期: {row['市场周期']}")
                logger.info(f"  资金因子:")
                logger.info(f"    - 成交额变化: {row['成交额变化']}")
                logger.info(f"    - 换手率: {row['换手率']}")
                logger.info(f"  趋势因子:")
                logger.info(f"    - 排名动量: {row['排名动量']}")
                logger.info(f"    - 涨停趋势: {row['涨停趋势']}")
                logger.info(f"    - 持续性评分: {row['持续性评分']}")
                logger.info(f"  交叉验证:")
                logger.info(f"    - 主要行业: {row.get('主要行业', 'N/A')}")
                logger.info(f"    - 行业集中度: {row.get('行业集中度', 'N/A')}")
                logger.info(f"    - 信号类型: {row.get('信号类型', 'N/A')}")
                logger.info(f"    - 信号强度: {row.get('信号强度', 'N/A')}")
                logger.info(f"    - 共振得分: {row.get('共振得分', 'N/A')}")
                logger.info(f"  策略建议:")
                logger.info(f"    - 操作建议: {row['操作建议']}")
                logger.info(f"    - 建议仓位: {row['建议仓位']}")
                logger.info(f"    - 调整后仓位: {row.get('调整后仓位', row['建议仓位'])}")
                logger.info(f"    - 紧急度: {row['紧急度']}")
                logger.info(f"    - 策略理由: {row['策略理由']}")
                logger.info(f"    - 验证理由: {row.get('验证理由', 'N/A')}")
                logger.info("")
        else:
            logger.info("  无主线板块数据")
        logger.info("=" * 60)
        
        # 5. 板块持续性分析
        logger.info("[5/6] 分析板块持续性...")
        sector_persistence_df = self.sector_tracker.analyze_sectors_persistence(date, top_n=10)
        if not sector_persistence_df.empty:
            logger.info(f"✓ 板块持续性分析完成，发现 {len(sector_persistence_df)} 个板块")
            for _, row in sector_persistence_df.head(5).iterrows():
                logger.info(f"  {row['板块名称']}: 评分{row['持续性评分']}, 阶段[{row['所处阶段']}], {row['操作建议']}")
        else:
            logger.info("  无板块持续性数据")
        
        # 6. 综合当日数据和20日数据计算板块权重
        logger.info("[6/6] 计算综合板块权重（当日+20日）...")
        display_mainline_df = self._calculate_combined_mainline(hierarchy_df, date)
        
        # 7. 模式识别
        logger.info("[7/7] 识别交易模式...")
        pr = PatternRecognition(self.dm, sector_engine=None, mapper=self.mapper)
        
        # 从板块持续性分析中提取热点板块信息
        hot_sectors = []
        if not sector_persistence_df.empty:
            for _, row in sector_persistence_df.iterrows():
                stage = row.get('所处阶段', '')
                if stage in ['加速期', '高潮期', '萌芽期']:
                    hot_sectors.append({
                        'sector_name': row.get('板块名称', ''),
                        'stats': {'涨停家数': row.get('涨停家数', 0)},
                        'trend_stage': stage,
                        'action': row.get('操作建议', ''),
                        'confidence': row.get('持续性评分', 50) / 100
                    })
        
        # 调试日志：显示传递的热点板块
        logger.info(f"传递给模式识别的热点板块: {len(hot_sectors)}个")
        for hs in hot_sectors[:5]:  # 显示前5个
            logger.info(f"  - {hs['sector_name']} ({hs['trend_stage']})")
        
        patterns = pr.scan_all_patterns(date, self.yesterday, hot_sectors=hot_sectors)

        total_signals = sum(len(v) for v in patterns.values())
        logger.info(f"识别到 {total_signals} 个交易信号")
        for ptype, signals in patterns.items():
            if signals:
                logger.info(f"  - {ptype}: {len(signals)}个")
        
        # 7. 为核心标的获取概念数据
        logger.info("[7/7] 获取核心标的所属概念...")
        if not hierarchy_df.empty:
            # 只获取10点半前封板的核心标的的概念
            core_stocks_mask = hierarchy_df.apply(lambda row: self._is_core_stock(row), axis=1)
            core_stocks_df = hierarchy_df[core_stocks_mask].copy()
            
            if not core_stocks_df.empty:
                logger.info(f"核心标的: {len(core_stocks_df)}只")
        
        # 8. 获取龙头池和走弱池数据
        logger.info("[8/8] 获取龙头池和走弱池数据...")
        dragon_pool_data = []
        weakening_pool_data = []
        if hasattr(pr, 'weak_to_strong') and pr.weak_to_strong:
            try:
                pool_summary = pr.weak_to_strong.get_pools_summary()
                dragon_pool_data = pool_summary.get('dragon_pool', [])
                weakening_pool_data = pool_summary.get('weakening_pool', [])
                logger.info(f"✓ 龙头池: {len(dragon_pool_data)}只, 走弱池: {len(weakening_pool_data)}只")
            except Exception as e:
                logger.warning(f"获取龙头池数据失败: {e}")
        
        # 9. 生成报告
        logger.info("[9/9] 生成分析报告...")
        # 使用 mainline_df（来自 analyze_with_validation）作为热点概念数据
        # 包含：当前排名、10日累计排名、共振得分、持续性评分等核心指标
        report_data = {
            'date': date,
            'mainline_df': mainline_df,  # 使用 sector_tracker.analyze_with_validation 的结果
            'emotion_result': emotion_result,
            'sector_persistence_df': sector_persistence_df,
            'patterns': patterns,
            'hierarchy_df': hierarchy_df,
            'zt_pool': zt_pool,
            'dragon_pool': dragon_pool_data,
            'weakening_pool': weakening_pool_data
        }
        
        # 使用带时间戳的文件名避免文件被占用
        timestamp = datetime.now().strftime("%H%M%S")
        report_file_name = f"短线情绪分析报告_{date}_{timestamp}.xlsx"
        report_path = self.reporter.create_daily_report(report_data, file_name=report_file_name)
        logger.info(f"✅ 分析完成，报告保存至: {report_path}")
        
        # 10. 生成交易计划（复盘后生成次日计划）
        logger.info("[10/10] 生成次日交易计划...")
        self._generate_trade_plans(date, patterns, display_mainline_df, emotion_result)
        
        # 11. 生成散户支持报告（隔夜预判、三阶过滤、剧本推演等）
        logger.info("[11/11] 生成散户决策支持报告...")
        self._generate_retail_support_report(date, zt_pool, hierarchy_df, patterns, emotion_result)
        
        # 11. 输出交易建议
        self._print_trading_advice(display_mainline_df, patterns, emotion_result, sector_persistence_df)
    
    def _generate_trade_plans(self, date: str, patterns: Dict, mainline_df: pd.DataFrame, emotion_result: Dict):
        """
        生成次日交易计划
        整合所有模式信号，生成可执行的交易计划
        
        筛选逻辑：
        1. 必须是模式选股选出来的标的
        2. 必须与热点主线共振（属于主线板块或概念相关）
        3. 考虑情绪周期调整仓位
        """
        try:
            # 初始化执行引擎
            if self.execution_engine is None:
                self.execution_engine = UnifiedExecutionEngine(self.dm, None)
            
            # 获取热点主线板块名称列表
            hot_sectors = []
            if not mainline_df.empty:
                # 使用正确的列名 L2_Industry
                column_name = 'L2_Industry' if 'L2_Industry' in mainline_df.columns else '板块名称'
                hot_sectors = mainline_df[column_name].tolist()
                logger.info(f"【交易计划】热点主线板块: {hot_sectors}")
            
            # 获取情绪周期建议仓位
            emotion_cycle = emotion_result.get('cycle_name', '震荡期')
            suggested_position = emotion_result.get('strategy', {}).position if hasattr(emotion_result.get('strategy'), 'position') else "30-50%"
            logger.info(f"【交易计划】当前情绪周期: {emotion_cycle}, 建议仓位: {suggested_position}")
            
            # 过滤与热点共振的模式信号
            filtered_patterns = self._filter_resonance_signals(patterns, mainline_df)
            
            # 统计过滤结果
            total_before = sum(len(signals) for signals in patterns.values())
            total_after = sum(len(signals) for signals in filtered_patterns.values())
            logger.info(f"【交易计划】模式信号过滤: {total_before} -> {total_after} (保留与热点共振的标的)")
            
            # 生成并保存交易计划
            trade_plans_dir = Path(OUTPUT_DIR) / "trade_plans"
            trade_plans_dir.mkdir(parents=True, exist_ok=True)

            plans_df, report = self.execution_engine.generate_and_save_plans(
                analysis_date=date,
                all_signals=filtered_patterns,
                output_dir=str(trade_plans_dir)
            )
            
            if not plans_df.empty:
                logger.info(f"✅ 交易计划生成完成: {len(plans_df)} 条计划")
                # 打印交易报告摘要
                print("\n" + "="*60)
                print("【次日交易计划摘要】")
                print(f"情绪周期: {emotion_cycle} | 建议仓位: {suggested_position}")
                print(f"热点主线: {', '.join(hot_sectors[:3])}")
                print("="*60)
                # 按介入时机分组显示
                for timing in plans_df['介入时机'].unique():
                    group = plans_df[plans_df['介入时机'] == timing]
                    print(f"\n【{timing}】{len(group)}只")
                    for _, row in group.head(3).iterrows():
                        resonance_tag = "🔥共振" if row.get('热点共振', False) else ""
                        print(f"  - {row['名称']}({row['代码']}) - {row['模式']} {resonance_tag}")
                        print(f"    目标价:{row['目标价']:.2f} 止损:{row['止损价']:.2f} 仓位:{row['仓位']}")
                print("="*60)
            else:
                logger.info("当日无交易计划生成")
                
        except Exception as e:
            logger.error(f"生成交易计划失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    def _filter_resonance_signals(self, patterns: Dict, mainline_df: pd.DataFrame) -> Dict:
        """
        过滤与热点主线共振的模式信号

        Args:
            patterns: 所有模式信号
            mainline_df: 热点主线板块DataFrame，包含板块名称和热度评分

        Returns:
            过滤后的模式信号
        """
        if mainline_df.empty:
            logger.warning("【_filter_resonance_signals】无热点主线数据，返回所有信号")
            return patterns

        # 构建板块名称到热度评分的映射
        sector_column = 'L2_Industry' if 'L2_Industry' in mainline_df.columns else '板块名称'
        score_column = 'Strength_Score' if 'Strength_Score' in mainline_df.columns else '综合评分'

        hot_sectors = mainline_df[sector_column].tolist()
        sector_heat_map = {}
        for _, row in mainline_df.iterrows():
            sector_name = row[sector_column]
            heat_score = row.get(score_column, 0)
            sector_heat_map[sector_name] = heat_score

        filtered = {}

        for pattern_name, signals in patterns.items():
            filtered_signals = []

            for signal in signals:
                # 检查信号是否与热点共振
                is_resonance = False
                resonance_sectors = []
                max_heat_score = 0

                # 检查股票所属行业/概念是否在热点主线中
                signal_industry = getattr(signal, 'l2_industry', '') or getattr(signal, 'industry', '')
                signal_concepts = getattr(signal, 'concepts', []) or getattr(signal, 'concept', '')

                # 将concepts转换为列表
                if isinstance(signal_concepts, str):
                    signal_concepts = [c.strip() for c in signal_concepts.split(',') if c.strip()]

                # 检查行业匹配
                if signal_industry and any(sector in signal_industry or signal_industry in sector
                                           for sector in hot_sectors):
                    is_resonance = True
                    resonance_sectors.append(signal_industry)
                    # 获取该板块的热度评分
                    for sector in hot_sectors:
                        if sector in signal_industry or signal_industry in sector:
                            heat_score = sector_heat_map.get(sector, 0)
                            max_heat_score = max(max_heat_score, heat_score)

                # 检查概念匹配
                for concept in signal_concepts:
                    if any(sector in concept or concept in sector for sector in hot_sectors):
                        is_resonance = True
                        if concept not in resonance_sectors:
                            resonance_sectors.append(concept)
                        # 获取该概念对应板块的热度评分
                        for sector in hot_sectors:
                            if sector in concept or concept in sector:
                                heat_score = sector_heat_map.get(sector, 0)
                                max_heat_score = max(max_heat_score, heat_score)

                # 如果与热点共振，添加到过滤后的信号
                if is_resonance:
                    # 给信号添加共振标记和热度评分
                    signal.hot_resonance = True
                    signal.resonance_sectors = resonance_sectors
                    signal.sector_heat_score = max_heat_score
                    filtered_signals.append(signal)
                    logger.debug(f"【共振】{signal.stock_name}({signal.stock_code}) 与热点共振: {resonance_sectors}, 热度:{max_heat_score:.1f}")
                else:
                    # 如果不共振但置信度>=0.9，也保留（优质独立逻辑）
                    if getattr(signal, 'confidence', 0) >= 0.9:
                        signal.hot_resonance = False
                        signal.resonance_sectors = []
                        signal.sector_heat_score = 0
                        filtered_signals.append(signal)
                        logger.debug(f"【高置信】{signal.stock_name}({signal.stock_code}) 置信度{signal.confidence}，虽非热点但保留")

            if filtered_signals:
                filtered[pattern_name] = filtered_signals

        return filtered
    
    def _generate_retail_support_report(self, date: str, today_zt: pd.DataFrame,
                                        hierarchy_df: pd.DataFrame, patterns: Dict,
                                        emotion_result: Dict):
        """
        生成散户决策支持报告 - V2版本
        包含：隔夜预判、板块分析、散户指标、剧本推演等
        """
        try:
            # 初始化散户支持模块V2
            if self.retail_support is None:
                self.retail_support = RetailTraderSupportV2(self.dm)
            
            # 获取昨日涨停数据
            yesterday_zt = self.dm.get_limit_up_pool(self.yesterday)
            
            # 1. 分析板块（T+0视角）
            history_pools = {
                date: today_zt,
                self.yesterday: yesterday_zt if not yesterday_zt.empty else pd.DataFrame()
            }
            self.sector_analysis = self.retail_support.analyze_sectors_v2(
                today_zt, history_pools
            )
            sector_analysis = self.sector_analysis
            
            # 2. 生成隔夜决策清单
            decisions = self.retail_support.generate_overnight_decisions_v2(
                today_zt, yesterday_zt, sector_analysis
            )
            
            # 3. 计算散户特供指标（简化版）
            # 从emotion_result获取策略信息
            strategy = emotion_result.get('strategy', None)
            temperature = '稳健'
            if strategy:
                temperature = strategy.position if hasattr(strategy, 'position') else '稳健'
            
            indicators = {
                '昨日涨停溢价率': 10.0,  # 简化，实际需要计算
                '一字板占比': 0.0,       # 简化，实际需要计算
                '次日策略建议': temperature
            }
            
            # 4. 推演次日剧本
            scenarios = self.retail_support.forecast_scenarios_v2(
                indicators, sector_analysis, today_zt
            )
            
            # 5. 生成报告
            report = self.retail_support.generate_overnight_report_v2(
                decisions, indicators, scenarios, sector_analysis
            )
            
            # 6. 保存报告
            report_file = Path(OUTPUT_DIR) / f"散户决策报告_{date}.txt"
            with open(report_file, 'w', encoding='utf-8') as f:
                f.write(report)
            logger.info(f"✓ 散户决策报告已保存: {report_file}")
            
            # 6. 打印摘要 - 使用格式化函数
            def fmt_val(val, decimal_places=2):
                if val is None:
                    return 0.0
                if isinstance(val, (np.integer, np.int64, np.int32)):
                    return int(val)
                if isinstance(val, (np.floating, np.float64, np.float32)):
                    return round(float(val), decimal_places)
                if isinstance(val, (int, float)):
                    if isinstance(val, float):
                        return round(val, decimal_places)
                    return val
                return val
            
            print("\n" + "="*60)
            print("【散户决策支持摘要】")
            print("="*60)
            print(f"\n[散户特供指标]")
            print(f"  昨日涨停溢价率: {fmt_val(indicators.get('昨日涨停溢价率', 0), 2)}%")
            print(f"  一字板占比: {fmt_val(indicators.get('一字板占比', 0), 1)}%")
            print(f"  策略建议: {indicators.get('次日策略建议', '稳健')}")
            
            print(f"\n[隔夜决策清单] ({len(decisions)}项):")
            for decision in decisions[:5]:
                print(f"  - {decision.stock_name}({decision.stock_code}) - {decision.decision_type}")
                print(f"    适配度: {decision.retail_suitability} | {decision.suggested_action}")
            
            print(f"\n[次日剧本推演]:")
            for i, scenario in enumerate(scenarios[:3], 1):
                print(f"  {i}. {scenario.scenario_name} (概率{scenario.probability:.0%})")
                print(f"     描述: {scenario.description}")
                if hasattr(scenario, 'actions_if_empty') and scenario.actions_if_empty:
                    print(f"     空仓动作: {scenario.actions_if_empty[0]}")
            
            print("="*60)
            
        except Exception as e:
            logger.error(f"生成散户支持报告失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    def _print_trading_advice(self, mainline_df, patterns, emotion_result, sector_persistence_df=None):
        """输出简明的交易建议"""
        print("\n" + "="*60)
        print("【今日交易决策辅助】")
        print("="*60)
        
        # 情绪周期判断
        cycle_name = emotion_result.get('cycle_name', '未知')
        strategy = emotion_result.get('strategy', None)
        
        if cycle_name == '高潮期':
            print("[!] 市场情绪高潮，建议减仓观望，避免高位接盘")
        elif cycle_name == '冰点期':
            print("[i] 市场情绪冰点，轻仓试错或空仓等待")
        elif cycle_name == '上升期':
            print("[+] 市场上升期，积极参与主线板块")
        elif cycle_name == '震荡期':
            print("[~] 市场震荡期，快进快出，严格止损")
        elif cycle_name == '退潮期':
            print("[-] 市场退潮期，空仓或极小仓位试错")
        
        if strategy:
            print(f"\n[策略建议] {strategy.strategy}")
            print(f"[仓位控制] {strategy.position}")
        
        # 主线推荐 - 优先使用板块持续性分析
        if sector_persistence_df is not None and not sector_persistence_df.empty:
            print("\n[重点关注的板块 - 持续性Top5]:")
            for i, row in sector_persistence_df.head(5).iterrows():
                stage_icon = "🔥" if row['所处阶段'] == '高潮期' else "📈" if row['所处阶段'] == '加速期' else "🌱"
                print(f"  {i+1}. {stage_icon} {row['板块名称']} (涨停{row['涨停家数']}家, 评分{row['持续性评分']})")
                print(f"     阶段: {row['所处阶段']} | {row['操作建议']}")
        elif not mainline_df.empty:
            print("\n[重点关注的板块 - 主线Top3]:")
            for i, row in mainline_df.head(3).iterrows():
                print(f"  {i+1}. {row['L2_Industry']} (涨停{row['LimitUp_Count']}家, 强度{row['Strength_Score']})")
        
        # 模式推荐
        print("\n[明日竞价关注标的]:")
        watchlist = []
        for ptype, signals in patterns.items():
            for sig in signals[:2]:  # 每类模式取前2
                watchlist.append(f"  - {sig.stock_name} ({sig.pattern_type}) - {sig.description}")
        if watchlist:
            print("\n".join(watchlist[:5]))  # 最多显示5个
        else:
            print("  暂无明确信号，建议结合明日竞价情况")
        
        print("="*60)
    
    def run_backtest_mode(self, start_date: str, end_date: str):
        """回测模式"""
        logger.info(f"启动回测: {start_date} 至 {end_date}")
        # 实现多日期回测逻辑
        pass
    
    def _calculate_combined_mainline(self, hierarchy_df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        """
        计算综合板块权重（当日数据 + 20日统计数据 + 东财行业板块因子）
        权重公式: 综合强度 = 20日涨停数×0.35 + 当日涨停数×0.35 + 最高连板×0.15 + 东财综合强度×0.15
        """
        # 如果没有hierarchy数据，返回空DataFrame
        if hierarchy_df.empty:
            logger.warning("[_calculate_combined_mainline] hierarchy_df为空，无法计算主线")
            return pd.DataFrame()
        # 0. 获取东财行业板块数据
        sector_data = self.dm.get_industry_sector_data(trade_date)
        sector_dict = {}
        if not sector_data.empty:
            # 构建行业名称到因子数据的映射
            for _, row in sector_data.iterrows():
                sector_dict[row['name']] = {
                    'pct_change': row['pct_change'],
                    'up_down_ratio': row['up_down_ratio'],
                    'up_down_diff': row['up_down_diff'],
                    'leading_pct': row['leading_pct'],
                    'leading_strength': row['leading_strength'],
                    'turnover_rate': row['turnover_rate'],
                    'activity_score': row['activity_score'],
                    'composite_strength': row['composite_strength'],
                    'up_num': row['up_num'],
                    'down_num': row['down_num']
                }
            logger.info(f"✓ 获取东财行业板块数据: {len(sector_dict)}个板块")
        
        # 1. 统计当日各板块涨停数
        today_stats = {}
        if not hierarchy_df.empty:
            for l2_name, group in hierarchy_df.groupby('L2_Industry'):
                if l2_name == '其他':
                    continue
                
                # 获取该组的L1和L2（取第一个非空值）
                l1_values = group['L1_Industry'].dropna()
                l2_values = group['L2_Industry'].dropna()
                
                l1 = l1_values.iloc[0] if not l1_values.empty and l1_values.iloc[0] != '其他' else '未知'
                l2 = l2_values.iloc[0] if not l2_values.empty and l2_values.iloc[0] != '其他' else '未知'
                
                # 如果L1或L2为空，尝试从行业映射获取
                if l1 == '未知' or l2 == '未知' or l1 == '其他' or l2 == '其他':
                    l2_mapped = self.mapper.get_l2_by_l3(l2_name)
                    l1_mapped = self.mapper.get_l1_by_l2(l2_mapped)
                    if l2 == '未知' or l2 == '其他':
                        l2 = l2_mapped if l2_mapped != '其他' else l2
                    if l1 == '未知' or l1 == '其他':
                        l1 = l1_mapped if l1_mapped != '其他' else l1
                
                today_stats[l2_name] = {
                    'today_count': len(group),
                    'max_board': group['BoardHeight'].max() if 'BoardHeight' in group.columns else 1,
                    'l1': l1,
                    'l2': l2
                }
        
        # 2. 加载20日统计数据
        mainline_20d_path = Path(OUTPUT_DIR) / "mainline_sectors.xlsx"
        combined_data = []
        
        if mainline_20d_path.exists():
            mainline_20d_df = pd.read_excel(mainline_20d_path)
            
            # 3. 合并数据计算综合权重
            for _, row in mainline_20d_df.iterrows():
                l2_name = row['L2_Industry']
                
                # 20日数据
                count_20d = row['Total_Limit_Up']
                
                # 当日数据（如果有）
                today_data = today_stats.get(l2_name, {})
                today_count = today_data.get('today_count', 0)
                today_max_board = today_data.get('max_board', 1)
                
                # 取20日和当日的最高连板
                max_board = max(
                    today_max_board,
                    today_data.get('max_board', 1)
                )
                
                # 获取东财行业板块因子
                sector_factor = sector_dict.get(l2_name, {})
                dc_composite = sector_factor.get('composite_strength', 0)
                dc_up_down_ratio = sector_factor.get('up_down_ratio', 0)
                dc_leading_strength = sector_factor.get('leading_strength', 0)
                
                # 计算综合强度分
                # 权重: 20日涨停数×0.35 + 当日涨停数×0.35 + 最高连板×0.15 + 东财综合强度×0.15
                strength_score = (
                    count_20d * 0.35 +
                    today_count * 0.35 * 3 +  # 当日涨停权重放大3倍（当日重要性更高）
                    max_board * 0.15 * 5 +     # 连板权重放大5倍
                    dc_composite * 0.15        # 东财综合强度因子
                )
                
                combined_data.append({
                    'L1_Industry': row['L1_Industry'],
                    'L2_Industry': row['L2_Industry'],
                    'L2_Industry': l2_name,
                    'LimitUp_Count': count_20d,  # 兼容报告生成器
                    'LimitUp_Count_20d': count_20d,
                    'LimitUp_Count_Today': today_count,
                    'Max_BoardHeight': max_board,
                    'Strength_Score': round(strength_score, 2),
                    # 东财因子字段
                    'DC_Composite': round(dc_composite, 2),
                    'DC_UpDownRatio': round(dc_up_down_ratio, 2),
                    'DC_LeadingStrength': round(dc_leading_strength, 2),
                    'DC_PctChange': round(sector_factor.get('pct_change', 0), 2),
                    'DC_UpNum': sector_factor.get('up_num', 0),
                    'DC_DownNum': sector_factor.get('down_num', 0)
                })
            
            # 4. 检查当日新出现的强势板块（不在20日统计中但当日有多只涨停）
            for l2_name, data in today_stats.items():
                if l2_name not in mainline_20d_df['L2_Industry'].values and data['today_count'] >= 2:
                    # 获取东财行业板块因子
                    sector_factor = sector_dict.get(l2_name, {})
                    dc_composite = sector_factor.get('composite_strength', 0)
                    dc_up_down_ratio = sector_factor.get('up_down_ratio', 0)
                    dc_leading_strength = sector_factor.get('leading_strength', 0)
                    
                    # 新出现的强势板块 - 融入东财因子
                    strength_score = (
                        data['today_count'] * 0.35 * 3 + 
                        data['max_board'] * 0.15 * 5 +
                        dc_composite * 0.15  # 东财综合强度因子
                    )
                    
                    # 获取正确的L1和L2
                    l1 = data['l1']
                    l2 = data['l2']
                    
                    # 如果L1或L2是"其他"，尝试从行业映射获取
                    if l1 == '其他' or l2 == '其他' or l1 == '未知' or l2 == '未知':
                        # 从mapper获取正确的行业信息
                        l2_correct = self.mapper.get_l2_by_l3(l2_name)
                        l1_correct = self.mapper.get_l1_by_l2(l2_correct)
                        l1 = l1_correct if l1_correct != '其他' else l1
                        l2 = l2_correct if l2_correct != '其他' else l2
                    
                    combined_data.append({
                        'L1_Industry': l1,
                        'L2_Industry': l2,
                        'L2_Industry': l2_name,
                        'LimitUp_Count': data['today_count'],  # 兼容报告生成器
                        'LimitUp_Count_20d': 0,
                        'LimitUp_Count_Today': data['today_count'],
                        'Max_BoardHeight': data['max_board'],
                        'Strength_Score': round(strength_score, 2),
                        'Is_New': True,  # 标记为新板块
                        # 东财因子字段
                        'DC_Composite': round(dc_composite, 2),
                        'DC_UpDownRatio': round(dc_up_down_ratio, 2),
                        'DC_LeadingStrength': round(dc_leading_strength, 2),
                        'DC_PctChange': round(sector_factor.get('pct_change', 0), 2),
                        'DC_UpNum': sector_factor.get('up_num', 0),
                        'DC_DownNum': sector_factor.get('down_num', 0)
                    })
                    logger.info(f"发现新强势板块: {l2_name} (当日{data['today_count']}只涨停)")
        else:
            # 如果没有20日数据，使用当日数据
            logger.info("20日统计数据不存在，使用当日数据")
            return mainline_df
        
        # 5. 排序并返回
        result_df = pd.DataFrame(combined_data)
        if not result_df.empty:
            result_df = result_df.sort_values('Strength_Score', ascending=False)
            logger.info(f"✓ 综合板块分析完成: {len(result_df)} 个板块")
            # 显示TOP5
            for i, row in result_df.head(5).iterrows():
                new_flag = " [NEW]" if row.get('Is_New', False) else ""
                logger.info(f"  {row['L2_Industry']}: 强度{row['Strength_Score']:.1f} (20日{row['LimitUp_Count_20d']}, 当日{row['LimitUp_Count_Today']}){new_flag}")
        
        return result_df
    
    def _is_core_stock(self, row) -> bool:
        """
        判断是否为10点半前封板的核心标的
        """
        # 检查涨停时间
        limit_up_time = str(row.get('LimitUpTime', ''))
        if limit_up_time.isdigit():
            limit_up_time = limit_up_time.zfill(6)
        if len(limit_up_time) == 6:
            limit_up_time = f"{limit_up_time[:2]}:{limit_up_time[2:4]}:{limit_up_time[4:]}"
        
        # 只保留10:30前封板的
        if limit_up_time and limit_up_time <= '10:30:00':
            l1 = row.get('L1_Industry', '')
            l2 = row.get('L2_Industry', '')
            l3 = row.get('L2_Industry', '')
            
            # 跳过"其他"行业
            if l3 != '其他' and l2 != '其他' and l1 != '其他':
                return True
        
        return False
    
    def update_industry_mapping(self):
        """手动更新行业映射"""
        logger.info("更新行业映射数据...")
        # 从AkShare获取最新行业列表并更新Excel
        try:
            concept_df = self.dm.get_concept_industry()
            if not concept_df.empty:
                logger.info(f"获取到 {len(concept_df)} 个行业板块")
                # 这里可以实现自动映射逻辑
        except Exception as e:
            logger.error(f"更新失败: {e}")

def main():
    parser = argparse.ArgumentParser(description='A股短线情绪量化系统')
    parser.add_argument('--date', type=str, help='分析日期(YYYYMMDD)，默认今日')
    parser.add_argument('--mode', type=str, default='daily', 
                       choices=['daily', 'backtest', 'update'], help='运行模式')
    parser.add_argument('--start', type=str, help='回测开始日期')
    parser.add_argument('--end', type=str, help='回测结束日期')
    
    args = parser.parse_args()
    
    # 配置日志
    setup_logging()
    
    system = SentimentSystem()
    
    if args.mode == 'daily':
        date = args.date if args.date else system.today
        system.run_daily_analysis(date)
    elif args.mode == 'backtest':
        if not args.start or not args.end:
            print("回测模式需要指定 --start 和 --end 日期")
            return
        system.run_backtest_mode(args.start, args.end)
    elif args.mode == 'update':
        system.update_industry_mapping()

def setup_logging():
    """配置日志输出"""
    # 移除默认的 stderr handler
    loguru.logger.remove()
    
    # 创建 logs 目录
    LOG_DIR = Path(__file__).parent / "logs"
    LOG_DIR.mkdir(exist_ok=True)
    
    # 添加控制台输出
    loguru.logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="DEBUG",
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )
    
    # 添加文件日志 - 保存到 logs 文件夹
    loguru.logger.add(
        LOG_DIR / "system.log",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        level="DEBUG",
        backtrace=True,
        diagnose=True,
    )
    
    return LOG_DIR


def run_backtest(start_date: str = None, end_date: str = None):
    """运行策略回测"""
    from backtest import BacktestEngine
    from backtest.backtest_engine import BacktestConfig
    from backtest.performance_analyzer import PerformanceAnalyzer

    logger.info("=" * 60)
    logger.info("开始运行策略回测")
    logger.info("=" * 60)

    # 设置默认日期
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    logger.info(f"回测区间: {start_date} 至 {end_date}")

    # 初始化
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)

    config = BacktestConfig(
        initial_capital=100000.0,
        max_position_per_stock=0.20,
        max_total_position=0.80,
        stop_loss_pct=0.05,
        take_profit_pct=0.10
    )

    backtest = BacktestEngine(dm, config)

    # 运行回测
    trade_plans_dir = Path(OUTPUT_DIR) / "trade_plans"

    if not trade_plans_dir.exists():
        logger.warning(f"交易计划目录不存在: {trade_plans_dir}")
        logger.info("请先运行每日分析生成交易计划: python main.py")
        return

    result = backtest.run_backtest(
        start_date=start_date,
        end_date=end_date,
        trade_plans_dir=str(trade_plans_dir)
    )

    # 生成报告
    analyzer = PerformanceAnalyzer()
    report = analyzer.generate_performance_report(result)
    print("\n" + report)

    # 保存详细结果
    _save_backtest_results(result, OUTPUT_DIR)

    logger.info("回测完成！")


def _save_backtest_results(result: dict, output_dir: str):
    """保存回测结果到文件"""
    output_path = Path(output_dir) / "backtest_results"
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存交易记录
    if result.get('trade_history'):
        trades_df = pd.DataFrame([{
            'date': t.date,
            'stock_code': t.stock_code,
            'stock_name': t.stock_name,
            'pattern_type': t.pattern_type,
            'action': t.action,
            'entry_price': t.entry_price,
            'exit_price': t.exit_price,
            'shares': t.shares,
            'pnl': t.pnl,
            'pnl_pct': t.pnl_pct,
            'holding_days': t.holding_days,
            'hot_resonance': t.hot_resonance,
            'resonance_sectors': t.resonance_sectors,
            'stop_loss_triggered': t.stop_loss_triggered,
            'take_profit_triggered': t.take_profit_triggered
        } for t in result['trade_history']])

        trades_file = output_path / f"backtest_trades_{timestamp}.csv"
        trades_df.to_csv(trades_file, index=False)
        logger.info(f"交易记录已保存: {trades_file}")

    # 保存净值曲线
    if result.get('daily_nav'):
        nav_df = pd.DataFrame(result['daily_nav'])
        nav_file = output_path / f"backtest_nav_{timestamp}.csv"
        nav_df.to_csv(nav_file, index=False)
        logger.info(f"净值曲线已保存: {nav_file}")

    # 保存汇总报告
    summary = {
        'total_return': result.get('total_return', 0),
        'annualized_return': result.get('annualized_return', 0),
        'sharpe_ratio': result.get('sharpe_ratio', 0),
        'max_drawdown': result.get('max_drawdown', 0),
        'win_rate': result.get('win_rate', 0),
        'profit_loss_ratio': result.get('profit_loss_ratio', 0),
        'total_trades': result.get('total_trades', 0),
        'initial_capital': result.get('initial_capital', 0),
        'final_capital': result.get('final_capital', 0)
    }

    summary_df = pd.DataFrame([summary])
    summary_file = output_path / f"backtest_summary_{timestamp}.csv"
    summary_df.to_csv(summary_file, index=False)
    logger.info(f"汇总报告已保存: {summary_file}")


if __name__ == "__main__":
    # 配置日志
    setup_logging()

    # 解析命令行参数
    parser = argparse.ArgumentParser(description='A股短线情绪量化系统')
    parser.add_argument('--mode', choices=['analysis', 'backtest', 'risk', 'position'],
                       default='analysis', help='运行模式')
    parser.add_argument('--start-date', type=str, help='回测开始日期 (YYYYMMDD)')
    parser.add_argument('--end-date', type=str, help='回测结束日期 (YYYYMMDD)')

    args = parser.parse_args()

    print(">>> A股短线情绪量化系统启动...")
    print(f"模式: {args.mode}")
    print("提示: 首次运行请先在 config/settings.py 中配置Tushare Token")
    print("-" * 60)

    try:
        if args.mode == 'analysis':
            # 执行每日分析
            system = SentimentSystem()
            system.run_daily_analysis()
        elif args.mode == 'backtest':
            # 运行回测
            run_backtest(args.start_date, args.end_date)
        elif args.mode == 'risk':
            # 运行风险分析演示
            from run_backtest import run_risk_analysis_demo
            run_risk_analysis_demo()
        elif args.mode == 'position':
            # 运行仓位管理演示
            from run_backtest import run_position_sizing_demo
            run_position_sizing_demo()
    except Exception as e:
        logger.error(f"系统运行错误: {e}")
        print(f"[X] 运行出错: {e}")
        print("请检查:")
        print("1. 是否已安装依赖: pip install pandas tushare akshare xlsxwriter loguru")
        print("2. 是否已配置Tushare Token")
        print("3. 网络连接是否正常")
