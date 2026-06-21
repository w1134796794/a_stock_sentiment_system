"""A股短线情绪量化系统 - 主程序入口。

当前 dev 分支只保留指标化数据生成与快照驱动回测：
  Phase 1: 预取并标准化基础数据
  Phase 2: 批处理因子指标
  Phase 3: 配置化筛选
  Phase 4: 生成快照与 Web 看板数据
  Phase 5: 实时行情由 /realtime 叠加确认
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import loguru

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import CACHE_DIR, TUSHARE_TOKEN
from core.data.data_manager_main import DataManager
from core.etl.daily_pipeline import ETLDailyPipeline, ETLDailyResult
from core.utils import DateUtils

logger = loguru.logger


class SentimentSystem:
    """指标化数据主入口。"""

    def __init__(self):
        self.dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        self.today = datetime.now().strftime("%Y%m%d")
        self.yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        self.pipeline = ETLDailyPipeline(self.dm)

    def run_daily_analysis(self, date: str | None = None) -> ETLDailyResult:
        """执行每日完整数据生成流程。"""
        target = str(date or self.today)
        date_utils = DateUtils()
        target = date_utils.get_nearest_trade_date(target)
        self.yesterday = date_utils.get_prev_trade_date(target)

        logger.info(f"开始执行 {target} 的日度数据生成...")
        logger.info(f"对比日期: {self.yesterday}")

        result = self.pipeline.run(target, self.yesterday)
        self._print_summary(result)
        if not result.ok:
            raise RuntimeError("数据生成未完整成功，请查看日志和 webdata/etl_quality 质量报告")
        return result

    @staticmethod
    def _print_summary(result: ETLDailyResult) -> None:
        """打印主流程摘要。"""
        print("\n" + "=" * 70)
        print("【指标数据生成摘要】")
        print("=" * 70)
        print(f"交易日: {result.trade_date}")
        print(f"基础数据质量: ok={result.silver_summary.get('quality_ok')} issues={result.silver_summary.get('issue_count')}")
        print("\n[Phase 2 - 因子指标]")
        for item in result.factor_results:
            print(f"  {item.get('name')}: ok={item.get('ok')} rows={item.get('rows')}")
            for msg in item.get("messages") or []:
                print(f"    - {msg}")
        print("\n[Phase 3 - 指标筛选]")
        print(
            f"  profile={result.screening.get('profile')} "
            f"input={result.screening.get('input_count')} "
            f"final={result.screening.get('final_count')}"
        )
        for row in (result.screening.get("final") or [])[:5]:
            print(f"  #{row.get('rank')} {row.get('name')}({row.get('code')}) score={row.get('score')}")
        print("\n[Phase 4 - 页面快照]")
        print(f"  snapshot={result.snapshot_paths.get('json') or '-'}")
        print(f"  analysis={result.analysis_path or '-'}")
        if result.warnings:
            print("\n[Warnings]")
            for warning in result.warnings:
                print(f"  - {warning}")
        print("=" * 70)


def setup_logging() -> Path:
    """配置日志输出。"""
    loguru.logger.remove()

    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    loguru.logger.add(
        sys.stdout,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        ),
        level="DEBUG",
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )
    loguru.logger.add(
        log_dir / "system.log",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
        level="DEBUG",
        backtrace=True,
        diagnose=True,
    )
    return log_dir


def run_backtest(start_date: str | None = None, end_date: str | None = None) -> None:
    """基于当前快照/筛选结果运行回测。"""
    from backtest.backtest_engine import BacktestConfig, BacktestEngine
    from backtest.performance_analyzer import PerformanceAnalyzer
    from backtest.plan_source import build_backtest_plan_dir
    from run_backtest import save_backtest_results

    end = end_date or datetime.now().strftime("%Y%m%d")
    start = start_date or (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    logger.info("=" * 60)
    logger.info(f"开始运行快照驱动回测: {start} 至 {end}")
    logger.info("=" * 60)

    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    plan_dir = build_backtest_plan_dir(start, end)
    config = BacktestConfig(
        initial_capital=100000.0,
        max_position_per_stock=0.20,
        max_total_position=0.80,
        stop_loss_pct=0.05,
        take_profit_pct=0.10,
    )
    result = BacktestEngine(dm, config).run_backtest(
        start_date=start,
        end_date=end,
        trade_plans_dir=str(plan_dir),
    )
    print("\n" + PerformanceAnalyzer().generate_performance_report(result))
    save_backtest_results(result, str(Path("output")))
    logger.info("回测完成")


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="A股短线情绪量化系统")
    parser.add_argument("--mode", choices=["analysis", "backtest", "risk", "position"], default="analysis", help="运行模式")
    parser.add_argument("--date", type=str, help="分析日期 (YYYYMMDD)，默认今日")
    parser.add_argument("--start-date", type=str, help="回测开始日期 (YYYYMMDD)")
    parser.add_argument("--end-date", type=str, help="回测结束日期 (YYYYMMDD)")
    args = parser.parse_args()

    print(">>> A股短线情绪量化系统启动...")
    print(f"模式: {args.mode}")
    print("提示: 首次运行请先配置 Tushare Token")
    print("-" * 60)

    try:
        if args.mode == "analysis":
            SentimentSystem().run_daily_analysis(args.date)
        elif args.mode == "backtest":
            run_backtest(args.start_date, args.end_date)
        elif args.mode == "risk":
            from run_backtest import run_risk_analysis_demo

            run_risk_analysis_demo()
        elif args.mode == "position":
            from run_backtest import run_position_sizing_demo

            run_position_sizing_demo()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"系统运行错误: {exc}")
        print(f"[X] 运行出错: {exc}")
        print("请检查依赖、Tushare Token 与网络连接。")
        raise


if __name__ == "__main__":
    main()
