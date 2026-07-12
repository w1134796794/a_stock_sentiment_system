from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest

from core.etl.daily_pipeline import ETLDailyPipeline
from core.etl.stage_status import (
    POST_CLOSE_SOURCES,
    POST_CLOSE_SILVER_TABLES,
    factor_status,
    fetch_status,
    require_stage,
    write_fetch_manifest,
)
from desktop.runner import RunController
from starlette.requests import Request
from web.app import fetch_page, run_page, screening_run_page


DATE = "20260703"


def _seed_partition(db_path, tables):
    with duckdb.connect(str(db_path)) as con:
        for table in tables:
            frame = pd.DataFrame([{"trade_date": DATE, "value": 1}])
            con.register("_seed", frame)
            con.execute(f'CREATE TABLE "{table}" AS SELECT * FROM _seed')
            con.unregister("_seed")


def _write_quality(web_data_dir, ok=True):
    path = web_data_dir / "etl_quality" / f"quality_{DATE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trade_date": DATE, "ok": ok}), encoding="utf-8")


def test_stage_status_requires_local_partitions(tmp_path):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"
    _write_quality(web_data)
    _seed_partition(
        db_path,
        ("stock_daily_silver", "sector_daily_silver", "index_daily_silver"),
    )

    status = fetch_status(DATE, db_path=db_path, web_data_dir=web_data)
    assert status["ready"] is True
    require_stage(status)

    missing = factor_status(DATE, db_path=db_path)
    assert missing["ready"] is False
    with pytest.raises(RuntimeError, match="请先运行因子计算"):
        require_stage(missing)


def test_fetch_skips_complete_date_without_touching_data_manager(tmp_path):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"
    _write_quality(web_data)
    _seed_partition(
        db_path,
        ("stock_daily_silver", "sector_daily_silver", "index_daily_silver"),
    )

    class ForbiddenDataManager:
        def __getattr__(self, name):
            raise AssertionError(f"完整日期不应访问数据接口: {name}")

    pipeline = ETLDailyPipeline(
        ForbiddenDataManager(),
        duckdb_path=db_path,
        web_data_dir=web_data,
        snapshot_dir=tmp_path / "snapshots",
        app_db_path=tmp_path / "app.sqlite",
    )
    result = pipeline.fetch_data(DATE, "20260702", skip_existing=True)

    assert result.ok is True
    assert result.stage == "fetch"
    assert result.silver_summary["skipped"] is True


def test_interface_manifest_distinguishes_core_ready_from_all_sources_complete(tmp_path):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"
    _write_quality(web_data)
    _seed_partition(
        db_path,
        ("stock_daily_silver", "sector_daily_silver", "index_daily_silver"),
    )
    sources = {source: {"ok": True, "rows": 0, "error": ""} for source in POST_CLOSE_SOURCES}
    sources["hm_detail"] = {"ok": False, "rows": 0, "error": "timeout"}
    writes = {table: {"duckdb": True} for table in POST_CLOSE_SILVER_TABLES}
    write_fetch_manifest(DATE, web_data_dir=web_data, sources=sources, writes=writes)

    status = fetch_status(DATE, db_path=db_path, web_data_dir=web_data)
    assert status["ready"] is True
    assert status["complete"] is False
    assert status["source_missing"] == ["hm_detail"]


def test_silver_duckdb_write_failure_blocks_factor_stage(tmp_path):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"
    _write_quality(web_data)
    _seed_partition(
        db_path,
        ("stock_daily_silver", "sector_daily_silver", "index_daily_silver"),
    )
    sources = {source: {"ok": True, "rows": 0, "error": ""} for source in POST_CLOSE_SOURCES}
    writes = {table: {"duckdb": True} for table in POST_CLOSE_SILVER_TABLES}
    writes["stock_attention_silver"] = {"duckdb": False}
    write_fetch_manifest(DATE, web_data_dir=web_data, sources=sources, writes=writes)

    status = fetch_status(DATE, db_path=db_path, web_data_dir=web_data)
    assert status["core_ready"] is True
    assert status["ready"] is False
    assert status["write_missing"] == ["stock_attention_silver"]
    with pytest.raises(RuntimeError, match="请先运行盘后取数"):
        require_stage(status)


def test_stage_controllers_are_independent():
    fetch = RunController("fetch")
    factors = RunController("factors")
    screening = RunController("screening")

    assert fetch.stage_method == "fetch_post_close_data"
    assert factors.stage_method == "run_factor_calculation"
    assert screening.stage_method == "run_screening_strategy"
    assert len({fetch._store.task_name, factors._store.task_name, screening._store.task_name}) == 3


@pytest.mark.parametrize(
    ("path", "view", "heading", "api"),
    [
        ("/fetch", fetch_page, "盘后取数", "/api/fetch"),
        ("/run", run_page, "因子计算", "/api/run"),
        ("/screening-run", screening_run_page, "选股策略", "/api/screening-run"),
    ],
)
def test_pipeline_stage_pages_render(path, view, heading, api):
    request = Request({"type": "http", "method": "GET", "path": path, "headers": []})
    response = view(request)
    body = response.body.decode("utf-8")

    assert heading in body
    assert api in body
    assert "单日运行" in body
    assert "历史批量" in body
