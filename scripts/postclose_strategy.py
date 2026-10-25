"""Post-close factors and selection consume persisted Silver inputs only."""
from __future__ import annotations


def run_strategy(date):
    from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR
    from core.etl.daily_pipeline import ETLDailyPipeline
    from core.etl.stage_status import fetch_status
    state = fetch_status(date, db_path=FACTOR_DB_PATH, web_data_dir=WEB_DATA_DIR)
    if not state.get("ready"):
        raise RuntimeError("等待独立盘后采集完成；策略禁止补取行情：" + str(state.get("message") or state.get("missing")))
    # No provider is supplied: accidental data-manager use fails instead of fetching.
    pipeline = ETLDailyPipeline(None)
    factors = pipeline.compute_factors(date)
    if not factors.ok:
        raise RuntimeError("本地因子计算未完成")
    screening = pipeline.run_screening(date, archive_cases=False)
    if not screening.ok:
        raise RuntimeError("本地选股未完成")
    return {"factors": True, "screening": True}
