"""
A股短线情绪量化系统 - 主程序入口
整合所有模块，提供CLI交互

架构：五层复盘流水线 (Review Pipeline)
  Layer 1: 看大盘（定仓位）- MarketEnvAnalyzer
  Layer 2: 看板块（定方向）- SectorAnalysisOrchestrator
  Layer 3: 看个股（定标的）- StockSelectionEngine
  Layer 4: 定计划（定执行）- TradePlanGenerator
  Layer 5: 盘后总结 - ReviewAnalyzer
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
from core.data.data_manager_main import DataManager
from core.data.industry_mapper import IndustryMapper
from core.analysis.pattern_recognition import PatternRecognition
from core.analysis.emotion_cycle_engine import EmotionCycleEngine
from core.analysis.ths_sector_tracker import THSSectorTracker
from core.analysis.sector_analysis_orchestrator import SectorAnalysisOrchestrator
from core.report.report_generator_v2 import ReportGeneratorV2
from core.execution.execution_engine import UnifiedExecutionEngine
from core.execution.retail_trader_support_v2 import RetailTraderSupportV2
from core.utils import DateUtils

from core.analysis.moneyflow_analyzer import create_moneyflow_analyzer
from core.analysis.chip_structure_analyzer import create_chip_analyzer
from core.analysis.emotion_cycle_integrated import create_integrated_engine

from core.pipeline.review_pipeline import ReviewPipeline, SharedContext

logger = loguru.logger

class SentimentSystem:
    def __init__(self):
        self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        self.mapper = IndustryMapper(self.dm)
        self.emotion_engine = EmotionCycleEngine(dm=self.dm)
        self.sector_tracker = THSSectorTracker(self.dm)
        self.sector_orchestrator = SectorAnalysisOrchestrator(self.dm, cache_enabled=True)
        self.reporter = ReportGeneratorV2(OUTPUT_DIR)
        self.execution_engine = None
        self.retail_support = None
        self.today = datetime.now().strftime("%Y%m%d")
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        self.moneyflow_analyzer = None
        self.chip_analyzer = None
        self.integrated_emotion_engine = None

        self.pipeline = ReviewPipeline(self.dm, self.mapper)

    def run_daily_analysis(self, date: str = None):
        """
        执行每日完整分析流程 - 使用五层复盘流水线

        五层流水线：
          Layer 1: 看大盘（定仓位）
          Layer 2: 看板块（定方向）
          Layer 3: 看个股（定标的）
          Layer 4: 定计划（定执行）
          Layer 5: 盘后总结
        """
        if date is None:
            date = self.today

        date_utils = DateUtils()
        date = date_utils.get_nearest_trade_date(date)
        self.yesterday = date_utils.get_prev_trade_date(date)

        logger.info(f"开始执行 {date} 的日度分析（五层复盘流水线）...")
        logger.info(f"对比日期: {self.yesterday}")

        # ========== 执行五层复盘流水线 ==========
        ctx = self.pipeline.execute(date)

        if ctx.errors:
            logger.error(f"流水线执行出现 {len(ctx.errors)} 个错误")
            for err in ctx.errors:
                logger.error(f"  - {err}")

        # ========== 打印流水线摘要 ==========
        self.pipeline.print_summary(ctx)

        # ========== DEBUG: 情绪周期详细数据 ==========
        self._print_emotion_debug(ctx.emotion_result)

        # ========== 生成报告 ==========
        self._generate_reports(ctx)

        # ========== 生成散户决策支持报告 ==========
        self._generate_retail_support_report_v2(ctx)

        # ========== 输出交易建议 ==========
        all_persistence_df = pd.concat(
            [ctx.concept_persistence_df, ctx.industry_persistence_df],
            ignore_index=True
        ) if not ctx.concept_persistence_df.empty or not ctx.industry_persistence_df.empty else pd.DataFrame()

        self._print_trading_advice(ctx.main_themes_df, ctx.patterns,
                                   ctx.emotion_result, all_persistence_df)

    def _print_emotion_debug(self, emotion_result: Dict):
        """打印情绪周期DEBUG数据"""
        if not emotion_result:
            return

        logger.info("=" * 60)
        logger.info("【DEBUG】情绪周期分析详细数据")
        logger.info("=" * 60)
        logger.info(f"情绪周期: {emotion_result.get('cycle_name', 'N/A')}")

        strategy = emotion_result.get('strategy')
        if strategy:
            logger.info(f"策略建议: {strategy.strategy}")
            logger.info(f"仓位控制: {strategy.position}")
            logger.info(f"禁忌操作: {strategy.forbidden_actions}")

        metrics = emotion_result.get('metrics', {})
        logger.info("\n【DEBUG】原始统计数据:")
        logger.info(f"  涨停家数: {metrics.get('limit_up_count', 'N/A')}")
        logger.info(f"  跌停家数: {metrics.get('nuclear_button_count', 'N/A')}")
        logger.info(f"  炸板率: {metrics.get('broken_rate', 'N/A')}%")
        logger.info(f"  最高连板: {metrics.get('max_board_height', 'N/A')}板")

        logger.info("\n【DEBUG】昨日涨停今日开盘表现:")
        logger.info(f"  平均溢价率: {metrics.get('prev_limit_up_premium', 'N/A')}%")
        logger.info(f"  开盘卖出胜率: {metrics.get('win_rate', 'N/A')}%")
        logger.info(f"  平均赢面: {metrics.get('avg_profit', 'N/A')}%")

        board_distribution = metrics.get('board_distribution', {})
        if board_distribution:
            logger.info(f"\n【DEBUG】连板分布: {board_distribution}")
            consecutive_count = sum(v for k, v in board_distribution.items() if k >= 2)
            first_board_count = board_distribution.get(1, 0)
            logger.info(f"  连板家数: {consecutive_count}")
            logger.info(f"  首板家数: {first_board_count}")

        scores = emotion_result.get('scores', {})
        logger.info("\n【DEBUG】情绪周期评分详情:")
        if scores:
            logger.info(f"  高潮期(boom)得分: {scores.get('boom', 'N/A')}")
            logger.info(f"  上升期(rise)得分: {scores.get('rise', 'N/A')}")
            logger.info(f"  震荡期(shake)得分: {scores.get('shake', 'N/A')}")
            logger.info(f"  退潮期(decline)得分: {scores.get('decline', 'N/A')}")
            logger.info(f"  冰点期(freeze)得分: {scores.get('freeze', 'N/A')}")
        logger.info("=" * 60)

    def _generate_reports(self, ctx: SharedContext):
        """生成分析报告"""
        logger.info("[报告] 生成分析报告...")

        report_data = self.pipeline.get_context_dict(ctx)

        timestamp = datetime.now().strftime("%H%M%S")
        report_file_name = f"短线情绪分析报告_{ctx.trade_date}_{timestamp}.xlsx"
        report_path = self.reporter.create_daily_report(report_data, file_name=report_file_name)
        logger.info(f"[OK] 分析报告保存至: {report_path}")

    def _generate_retail_support_report_v2(self, ctx: SharedContext):
        """生成散户决策支持报告"""
        logger.info("[散户支持] 生成散户决策支持报告...")
        self._generate_retail_support_report(
            ctx.trade_date, ctx.zt_pool, ctx.hierarchy_df,
            ctx.patterns, ctx.emotion_result
        )

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
                # 共振分析结果使用'主线名称'列（格式：概念+行业）
                if '主线名称' in mainline_df.columns:
                    hot_sectors = mainline_df['主线名称'].tolist()
                elif '核心概念' in mainline_df.columns:
                    hot_sectors = mainline_df['核心概念'].tolist()
                elif '核心行业' in mainline_df.columns:
                    hot_sectors = mainline_df['核心行业'].tolist()
                else:
                    hot_sectors = []
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
                        resonance_tag = "[共振]" if row.get('热点共振', False) else ""
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
        # 共振分析结果使用'主线名称'、'核心概念'、'核心行业'列
        if '主线名称' in mainline_df.columns:
            sector_column = '主线名称'
        elif '核心概念' in mainline_df.columns:
            sector_column = '核心概念'
        elif '核心行业' in mainline_df.columns:
            sector_column = '核心行业'
        else:
            logger.warning(f"【_filter_resonance_signals】未知的列名，可用列: {list(mainline_df.columns)}")
            return patterns
        
        score_column = '综合评分' if '综合评分' in mainline_df.columns else 'Strength_Score'

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
            logger.info(f"[OK] 散户决策报告已保存: {report_file}")
            
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
        
        # 情绪周期判断（新增：显示综合判断结果）
        cycle_name = emotion_result.get('cycle_name', '未知')
        strategy = emotion_result.get('strategy', None)
        integrated = emotion_result.get('integrated_analysis')
        
        # 优先显示综合判断结果
        if integrated:
            print(f"\n[情绪周期 - 综合判断]")
            print(f"  规则引擎: {integrated['rule_state']}")
            print(f"  ML模型: {integrated['ml_state']}")
            print(f"  最终判断: {integrated['final_state']} (置信度{integrated['confidence']:.1%})")
            if not integrated['agreement']:
                print(f"  [警告] 规则与ML判断不一致，{integrated['analysis']}")
            print(f"  风险等级: {integrated['risk_level']}")
        else:
            print(f"\n[情绪周期] {cycle_name}")
        
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
                stage_icon = "[高潮]" if row['所处阶段'] == '高潮期' else "[加速]" if row['所处阶段'] == '加速期' else "[萌芽]"
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
    parser.add_argument('--date', type=str, help='分析日期 (YYYYMMDD)，默认今日')
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
            system.run_daily_analysis(args.date)
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
