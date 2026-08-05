"""生成 factors.duckdb 中的 factor_stock_wide 表（供回测引擎使用）。"""
from __future__ import annotations

import sys
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from core.factors.jobs.runner import FactorJobRunner
from config.settings import FACTOR_DB_PATH


def main(trade_date: str | None = None) -> None:
    """运行因子计算任务。

    Args:
        trade_date: 交易日，格式 YYYYMMDD。不传则自动取最近交易日。
    """
    if trade_date is None:
        # 自动取最近交易日
        from core.data.data_manager import DataManger
        dm = DataManger()
        trade_date = dm.get_recent_trade_date()
        logger.info(f"自动检测到最近交易日: {trade_date}")

    logger.info(f"开始运行因子计算任务: {trade_date}")
    logger.info(f"目标数据库: {FACTOR_DB_PATH}")

    runner = FactorJobRunner(FACTOR_DB_PATH)
    results = runner.run(trade_date, jobs=["market", "stock"])  # 只跑 market 和 stock，节省时间

    for r in results:
        status = "✓" if r.ok else "✗"
        logger.info(f"  {status} {r.name}: {r.message or 'OK'}")

    logger.info(f"因子计算完成。请检查 {FACTOR_DB_PATH} 是否已生成。")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="交易日，格式 YYYYMMDD")
    args = parser.parse_args()
    main(args.date)