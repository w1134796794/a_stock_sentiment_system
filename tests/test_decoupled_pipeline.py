from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from starlette.requests import Request

from core.data.data_manager_base import DataManagerBase
from core.etl.daily_pipeline import ETLDailyPipeline, ETLDailyResult
from core.etl.stage_status import (
    POST_CLOSE_SILVER_TABLES,
    POST_CLOSE_SOURCES,
    factor_status,
    fetch_status,
    post_close_data_ready,
    require_stage,
    write_fetch_manifest,
)
from desktop.runner import RunController
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


def test_fetch_result_requires_ready_silver_not_only_quality_report():
    result = ETLDailyResult(
        trade_date=DATE,
        stage="fetch",
        silver_summary={"ready": False, "quality_ok": True},
    )

    assert result.ok is False


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


def test_factor_stage_never_trains_models_implicitly(tmp_path, monkeypatch):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"
    _write_quality(web_data)
    _seed_partition(
        db_path,
        ("stock_daily_silver", "sector_daily_silver", "index_daily_silver"),
    )

    pipeline = ETLDailyPipeline(
        object(), duckdb_path=db_path, web_data_dir=web_data,
        snapshot_dir=tmp_path / "snapshots", app_db_path=tmp_path / "app.sqlite",
    )
    monkeypatch.setattr(
        "core.factors.jobs.runner.FactorJobRunner.run", lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline, "_refresh_factor_models",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("factor stage trained models")),
    )

    result = pipeline.compute_factors(DATE, "20260702")

    assert result.stage == "factors"
    assert result.factor_results == []


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


def test_post_close_ready_time_protects_current_day(monkeypatch):
    monkeypatch.setattr("config.settings.POST_CLOSE_DATA_READY_TIME", "18:00")
    current_date = "20260819"

    assert post_close_data_ready(
        current_date, now=datetime(2026, 8, 19, 2, 0),
    ) is False
    assert post_close_data_ready(
        current_date, now=datetime(2026, 8, 19, 18, 1),
    ) is True
    assert post_close_data_ready(
        "20260818", now=datetime(2026, 8, 19, 2, 0),
    ) is True


def test_force_fetch_invalidates_trade_date_cache(tmp_path, monkeypatch):
    db_path = tmp_path / "factors.duckdb"
    web_data = tmp_path / "webdata"

    class FakeDataManager:
        def __init__(self):
            self.invalidated = []

        def invalidate_trade_date_cache(self, trade_date):
            self.invalidated.append(trade_date)
            return {"disk_files": 3}

        @staticmethod
        def get_limit_up_pool(_trade_date):
            return pd.DataFrame()

    dm = FakeDataManager()
    pipeline = ETLDailyPipeline(
        dm, duckdb_path=db_path, web_data_dir=web_data,
        snapshot_dir=tmp_path / "snapshots", app_db_path=tmp_path / "app.sqlite",
    )
    pipeline.data_prep = SimpleNamespace(
        build=lambda *_args, **_kwargs: SimpleNamespace(
            meta={"source_fetch_status": {}, "silver_persist": {"writes": {}}},
        ),
    )
    statuses = iter([
        {"complete": True, "premature_fetch": False},
        {"ready": False, "source_missing": [], "write_missing": []},
    ])
    monkeypatch.setattr(
        "core.etl.stage_status.fetch_status", lambda *_args, **_kwargs: next(statuses),
    )

    result = pipeline.fetch_data(
        DATE, "20260702", skip_existing=True, force_refresh=True,
    )

    assert dm.invalidated == [DATE]
    assert result.silver_summary["force_refresh"] is True
    assert result.silver_summary["cache_reset"]["disk_files"] == 3


def test_trade_date_cache_invalidation_removes_files_and_summary_rows(tmp_path):
    manager = DataManagerBase("", tmp_path / "cache")
    stale = manager.market_dir / "daily_basic" / f"{DATE}.csv"
    stale.write_text("trade_date,value\n20260703,1\n", encoding="utf-8")
    summary = manager.summary_dir / "limit_up_stocks.csv"
    pd.DataFrame([
        {"trade_date": DATE, "代码": "000001.SZ"},
        {"trade_date": "20260702", "代码": "000002.SZ"},
    ]).to_csv(summary, index=False)
    manager._set_memory_cache(f"daily_basic:{DATE}", pd.DataFrame([{"value": 1}]))

    result = manager.invalidate_trade_date_cache(DATE)

    assert result["disk_files"] == 1
    assert result["summary_rows"] == 1
    assert not stale.exists()
    remaining = pd.read_csv(summary)
    assert remaining["trade_date"].astype(str).tolist() == ["20260702"]


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
