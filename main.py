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
    SNAPSHOT_DIR, APP_DB_PATH, FACTOR_DB_PATH, KB_DB_PATH,
)
from core.data.data_manager_main import DataManager
from core.data.industry_mapper import IndustryMapper
from core.report.report_generator_v2 import ReportGeneratorV2
from core.execution.retail_trader_support_v2 import RetailTraderSupportV2
from core.utils import DateUtils

from core.pipeline.review_pipeline import ReviewPipeline, SharedContext

logger = loguru.logger

class SentimentSystem:
    """A股短线情绪量化系统主入口

    职责被收敛为：
      - 持有共享的 DataManager / IndustryMapper / Reporter
      - 通过 ReviewPipeline 执行五层复盘流程
      - 生成 Excel 报告与散户决策报告
    """

    def __init__(self):
        self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        self.mapper = IndustryMapper(self.dm)
        self.reporter = ReportGeneratorV2(OUTPUT_DIR)
        self.retail_support: RetailTraderSupportV2 | None = None
        self.today = datetime.now().strftime("%Y%m%d")
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

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

        # P0：与 Excel 同源，把 data_dict 落成结构化快照（供 Web / KB 复用）。
        # 任何失败都不得影响既有 Excel 产出，故整体 try/except 兜底。
        try:
            from snapshot import SnapshotWriter
            SnapshotWriter(SNAPSHOT_DIR, APP_DB_PATH, FACTOR_DB_PATH).write(report_data)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Snapshot] 快照写出失败（不影响 Excel）: {e}")

        # P2：把当日快照灌入知识库（无 key 走词法检索，离线可用），供 Web 问答检索。
        try:
            from snapshot.writer import build_snapshot
            from kb.store import KBStore
            from kb.ingest import ingest_snapshot
            from kb.embeddings import get_embedder
            ingest_snapshot(build_snapshot(report_data), KBStore(KB_DB_PATH), get_embedder())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[KB] 知识库灌库失败（不影响主流程）: {e}")

    def _generate_retail_support_report_v2(self, ctx: SharedContext):
        """生成散户决策支持报告"""
        logger.info("[散户支持] 生成散户决策支持报告...")
        self._generate_retail_support_report(
            ctx.trade_date, ctx.zt_pool, ctx.hierarchy_df,
            ctx.patterns, ctx.emotion_result
        )

    def _generate_retail_support_report(self, date: str, today_zt: pd.DataFrame,
                                        hierarchy_df: pd.DataFrame, patterns: Dict,
                                        emotion_result: Dict):
        """
        生成散户决策支持报告 - V2版本
        包含：隔夜预判、板块分析、散户指标（V2 已移除主观"次日剧本推演"）
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
            
            # 4. 生成报告（V2 已移除主观"次日剧本推演"，一切以真实数据说话）
            report = self.retail_support.generate_overnight_report_v2(
                decisions, indicators, sector_analysis
            )
            
            # 5. 保存报告
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
