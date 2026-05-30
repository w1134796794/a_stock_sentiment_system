"""Sprint B-2：批量回填历史 factor_results JSON

用途
====
``output/factor_results/`` 是 Layer5 「历史 N 天回溯」、Sprint D「周期 × 模式
胜率矩阵」、Sprint E「相似日匹配」等多个功能的**数据地基**。

但日常只有今天的一份 JSON，没有历史样本。本脚本批量跑过去 N 个交易日的
``SentimentSystem.run_daily_analysis``，把每日 factor_results JSON 落到磁盘，
为后续历史复盘建立样本库。

用法
====

::

    # 回填近 60 个交易日（默认）
    python scripts/backfill_factor_results.py

    # 显式指定窗口
    python scripts/backfill_factor_results.py --start 20260301 --end 20260526

    # 只回填缺失的日期（默认开启）
    python scripts/backfill_factor_results.py --skip-existing

    # 全量重跑（覆盖既有 JSON）
    python scripts/backfill_factor_results.py --force

行为
====
* **顺序执行**：一个日期跑完再跑下一个，避免触发 Tushare 限流
* **跳过已有**：默认跳过 ``factor_results_YYYYMMDD.json`` 已存在的日期（``--force`` 覆盖）
* **失败容忍**：单日跑失败不阻塞后续日期，最后打印失败列表
* **缓存复用**：DataManager 缓存让重复运行的日期极快（首次 ~140s，再次 ~15s）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

# 加项目根到 sys.path，便于直接 ``python scripts/backfill_factor_results.py``
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import loguru
from config.settings import OUTPUT_DIR
from core.utils.date_utils import DateUtils

logger = loguru.logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="批量回填 factor_results 历史 JSON",
    )
    parser.add_argument(
        "--start", default=None,
        help="开始日期 YYYYMMDD（含）。默认 = end - 60 个交易日",
    )
    parser.add_argument(
        "--end", default=None,
        help="结束日期 YYYYMMDD（含）。默认 = 今日所在最近交易日",
    )
    parser.add_argument(
        "--lookback", type=int, default=60,
        help="未指定 --start 时回填的交易日数（默认 60）",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="即使 JSON 已存在也重新跑（默认跳过已有日期）",
    )
    return parser.parse_args()


def _resolve_date_range(args: argparse.Namespace) -> List[str]:
    """根据参数解析需要回填的交易日列表。"""
    du = DateUtils()

    if args.end:
        end = du.get_nearest_trade_date(args.end)
    else:
        end = du.get_nearest_trade_date(datetime.now().strftime("%Y%m%d"))

    if args.start:
        start = du.get_nearest_trade_date(args.start)
        # 取 [start, end] 之间所有交易日
        trade_dates = du.get_last_n_trade_dates(10_000, end_date=end)
        dates = [d for d in trade_dates if start <= d <= end]
    else:
        dates = du.get_last_n_trade_dates(args.lookback, end_date=end)

    return sorted(dates)


def _json_exists(trade_date: str) -> bool:
    """检查该日期的 factor_results JSON 是否已存在且非空。"""
    fpath = Path(OUTPUT_DIR) / "factor_results" / f"factor_results_{trade_date}.json"
    return fpath.exists() and fpath.stat().st_size > 1000  # 1KB 以下视为损坏


def _run_one(system, trade_date: str) -> Tuple[bool, str, float]:
    """跑单日。返回 ``(success, error_msg, elapsed_seconds)``。"""
    t0 = time.perf_counter()
    try:
        system.run_daily_analysis(trade_date)
        elapsed = time.perf_counter() - t0
        return True, "", elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return False, f"{type(e).__name__}: {e}", elapsed


def main():
    args = _parse_args()
    dates = _resolve_date_range(args)
    if not dates:
        logger.warning("没有需要回填的日期")
        return

    # 过滤已存在的（除非 --force）
    if not args.force:
        existing = [d for d in dates if _json_exists(d)]
        if existing:
            logger.info(f"[backfill] 跳过已有 JSON 的日期 {len(existing)} 个: {existing[:3]}...{existing[-3:]}")
        dates = [d for d in dates if not _json_exists(d)]

    if not dates:
        logger.info("[backfill] 所有日期 JSON 都已存在，无需回填（使用 --force 强制重跑）")
        return

    logger.info("=" * 70)
    logger.info(f"[backfill] 计划回填 {len(dates)} 个交易日：{dates[0]} ~ {dates[-1]}")
    logger.info("=" * 70)

    # 延迟 import SentimentSystem，避免脚本启动时间过长
    from main import SentimentSystem
    system = SentimentSystem()

    success: List[Tuple[str, float]] = []
    failed: List[Tuple[str, str, float]] = []

    overall_t0 = time.perf_counter()
    for i, trade_date in enumerate(dates, 1):
        logger.info(
            f"[backfill] >>> ({i}/{len(dates)}) 开始回填 {trade_date}"
        )
        ok, err, elapsed = _run_one(system, trade_date)
        if ok:
            success.append((trade_date, elapsed))
            logger.info(f"[backfill] <<< ({i}/{len(dates)}) {trade_date} 成功 ({elapsed:.1f}s)")
        else:
            failed.append((trade_date, err, elapsed))
            logger.error(f"[backfill] <<< ({i}/{len(dates)}) {trade_date} 失败: {err}")

        # 简单进度估算
        avg = (time.perf_counter() - overall_t0) / i
        remaining = (len(dates) - i) * avg
        logger.info(
            f"[backfill] 进度: {i}/{len(dates)} ({i/len(dates):.0%}) | "
            f"已用 {time.perf_counter()-overall_t0:.0f}s | 预计剩余 {remaining:.0f}s"
        )

    total_elapsed = time.perf_counter() - overall_t0
    logger.info("=" * 70)
    logger.info(f"[backfill] 全部完成: 成功 {len(success)}, 失败 {len(failed)}, 总耗时 {total_elapsed:.0f}s")
    if failed:
        logger.warning("[backfill] 失败日期清单：")
        for date, err, _ in failed:
            logger.warning(f"  - {date}: {err}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
