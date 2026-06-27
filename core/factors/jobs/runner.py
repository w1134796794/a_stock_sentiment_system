"""Runner for Phase 2 batch factor jobs."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, List, Optional

from loguru import logger

from core.factors.jobs.gold_utils import FactorJobResult, connect_duckdb
from core.factors.jobs.market_factor_job import MarketFactorJob
from core.factors.jobs.sector_factor_job import SectorFactorJob
from core.factors.jobs.stock_factor_job import StockFactorJob


class FactorJobRunner:
    """Run market/sector/stock factor jobs against a DuckDB warehouse."""

    JOBS = {
        "market": MarketFactorJob,
        "sector": SectorFactorJob,
        "stock": StockFactorJob,
    }

    def __init__(self, duckdb_path: Optional[Path] = None):
        if duckdb_path is None:
            from config.settings import FACTOR_DB_PATH

            duckdb_path = FACTOR_DB_PATH
        self.duckdb_path = Path(duckdb_path)

    def run(self, trade_date: str, *, jobs: Optional[Iterable[str]] = None) -> List[FactorJobResult]:
        selected = list(jobs or ("market", "sector", "stock"))
        results: List[FactorJobResult] = []
        connect_started = time.monotonic()
        logger.info(f"[Phase2] 打开因子仓库: {self.duckdb_path}")
        with connect_duckdb(self.duckdb_path) as con:
            logger.info(f"[Phase2] 因子仓库已打开, 耗时={time.monotonic() - connect_started:.1f}s")
            for job_name in selected:
                job_cls = self.JOBS.get(str(job_name))
                if job_cls is None:
                    result = FactorJobResult(name=str(job_name), trade_date=str(trade_date), ok=False)
                    result.add_message(f"未知 factor job: {job_name}")
                    results.append(result)
                    continue
                job = job_cls()
                job_started = time.monotonic()
                logger.info(f"[Phase2][{job_name}] 开始: {trade_date}")
                try:
                    result = job.run(con, str(trade_date))
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"[FactorJobRunner] {job_name} 运行失败: {e}")
                    result = FactorJobResult(name=job.name, trade_date=str(trade_date), ok=False)
                    result.add_message(str(e))
                results.append(result)
                logger.info(
                    f"[Phase2][{job_name}] 完成: {trade_date}, ok={result.ok}, "
                    f"耗时={time.monotonic() - job_started:.1f}s, rows={result.rows}"
                )
        return results


def run_factor_jobs(
    trade_date: str,
    *,
    duckdb_path: Optional[Path] = None,
    jobs: Optional[Iterable[str]] = None,
) -> List[dict]:
    runner = FactorJobRunner(duckdb_path=duckdb_path)
    return [result.to_dict() for result in runner.run(trade_date, jobs=jobs)]
