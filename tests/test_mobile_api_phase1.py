"""Phase 0-1 mobile API contracts and local-read guarantees."""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import duckdb
from fastapi import FastAPI

from core.application.mobile_repository import MobileReadRepository
from core.application.mobile_services import MobileReadService
from web.api.mobile.responses import mobile_error_response
from web.api.mobile.router import create_mobile_router
from web.permissions import permission_key_for_path, requires_admin


def _repository(tmp_path: Path) -> MobileReadRepository:
    webdata = tmp_path / "webdata"
    decision_dir = webdata / "screening" / "decision_pool"
    decision_dir.mkdir(parents=True)
    (decision_dir / "decision_pool_20260727.json").write_text(
        json.dumps(
            {
                "trade_date": "20260727",
                "generated_at": "2026-07-27T18:30:00",
                "regime": "strong",
                "regime_label": "强市",
                "market_score": 66.0,
                "market_risk_labels": ["炸板率偏高"],
                "crowding_summary": [
                    {"cluster": "机器人", "count": 3, "ratio_pct": 60.0, "level": "拥挤"}
                ],
                "cluster_limits": {"focus_per_cluster": 2, "active_per_cluster": 3},
                "rows": [
                    {
                        "code": "000001",
                        "name": "测试股份",
                        "行动分组": "重点确认",
                        "命中策略": ["主线龙头", "弱转强修复"],
                        "策略共识数": 2,
                        "策略总数": 3,
                        "所属主线": "机器人",
                        "相关题材": ["机器人", "人工智能"],
                        "板块强度": 72.5,
                        "明日入场模式": "弱转强确认",
                        "一句话结论": "等待次日分钟买点。",
                        "次日确认条件": "站上分时均价",
                        "失效条件": "板块转弱",
                        "建议仓位": "确认后参考10%",
                        "执行仓位上限%": 10,
                        "规则等级": "B",
                        "expected_gross_return_pct": 2.6,
                        "expected_excess_return_pct": 0.8,
                        "主题簇": "机器人",
                        "主题候选数": 3,
                        "主题占比%": 60.0,
                        "拥挤等级": "拥挤",
                        "拥挤说明": "机器人主题集中度偏高",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    db_path = webdata / "factors.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE factor_market_wide (
            trade_date VARCHAR, market_score DOUBLE, limit_up_count INTEGER,
            limit_down_count INTEGER, broken_rate DOUBLE, amount_yuan DOUBLE,
            emotion_phase_label VARCHAR, computed_at VARCHAR
        )
        """
    )
    con.execute(
        "INSERT INTO factor_market_wide VALUES ('20260727', 66, 51, 7, 21.5, 123456789, '活跃', '2026-07-27T18:31:00')"
    )
    for table in ("limit_up_pool_silver", "limit_down_pool_silver"):
        con.execute(
            f"""
            CREATE TABLE {table} (
                trade_date VARCHAR, code VARCHAR, ts_code VARCHAR, name VARCHAR,
                pct_chg DOUBLE, first_time VARCHAR, last_time VARCHAR,
                open_times INTEGER, limit_times INTEGER, fd_amount DOUBLE,
                float_mv DOUBLE, turnover_ratio DOUBLE
            )
            """
        )
    con.execute(
        "INSERT INTO limit_up_pool_silver VALUES "
        "('20260727','000001','000001.SZ','测试股份',10,'09:31','14:55',0,2,100,50,8)"
    )
    con.execute(
        """
        CREATE TABLE factor_stock_wide (
            trade_date VARCHAR, code VARCHAR, primary_sector_name VARCHAR,
            resonance_sectors VARCHAR
        )
        """
    )
    con.execute("INSERT INTO factor_stock_wide VALUES ('20260727','000001','机器人','[\"\"人工智能\"\"]')")
    con.execute(
        """
        CREATE TABLE stock_daily_silver (
            trade_date VARCHAR, code VARCHAR, ts_code VARCHAR, name VARCHAR,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
            pct_chg DOUBLE, vol_hand DOUBLE, amount_yuan DOUBLE
        )
        """
    )
    con.execute(
        "INSERT INTO stock_daily_silver VALUES "
        "('20260727','000001','000001.SZ','测试股份',10,11,9.8,10.8,10,8,1000,1000000)"
    )
    con.close()
    return MobileReadRepository(
        web_data_dir=webdata,
        factor_db_path=db_path,
        cache_dir=tmp_path / "cache",
    )


def _endpoint(service: MobileReadService, path: str):
    router = create_mobile_router(service)
    return next(route.endpoint for route in router.routes if route.path == path)


def test_mobile_service_reads_generated_local_data(tmp_path, monkeypatch):
    repository = _repository(tmp_path)
    service = MobileReadService(repository)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in {
            "tushare",
            "adata",
            "easyquotation",
            "pqquotation",
        }:
            raise AssertionError(f"mobile request imported external provider: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    dashboard = service.dashboard("20260727")
    candidates = service.candidates("20260727")
    limitup = service.limitup("20260727")

    assert dashboard["market"]["market_score"] == 66.0
    assert dashboard["market"]["risk_flags"] == ["炸板率偏高"]
    assert dashboard["crowding_summary"][0]["cluster"] == "机器人"
    assert dashboard["cluster_limits"]["active_per_cluster"] == 3
    assert candidates["items"][0]["mainline"] == "机器人"
    assert candidates["items"][0]["expected_excess_return_pct"] == 0.8
    assert candidates["items"][0]["confirmation"] == "站上分时均价"
    assert candidates["items"][0]["crowding_level"] == "拥挤"
    assert limitup["limit_up_count"] == 1
    assert limitup["max_board_height"] == 2


def test_candidate_detail_backfills_exclusion_reasons_from_legacy_pool():
    class LegacyRepository:
        @staticmethod
        def load_decision_pool(_trade_date: str) -> dict:
            return {
                "trade_date": "20260904",
                "rows": [{
                    "code": "600371",
                    "name": "测试候选",
                    "行动分组": "暂不参与",
                    "_blocked_reasons": ["优先级未进入今日8只决策池"],
                    "penalty_reasons": ["板块同步证据不足"],
                    "失效条件": "规则优势或增强证据不足",
                }],
            }

    detail = MobileReadService(LegacyRepository()).candidate_detail("600371", "20260904")

    assert detail["evidence"]["exclusion_reasons"] == [
        "优先级未进入今日8只决策池",
        "板块同步证据不足",
        "规则优势或增强证据不足",
    ]


def test_mobile_api_success_pagination_and_contract(tmp_path):
    service = MobileReadService(
        _repository(tmp_path),
        realtime_loader=lambda *_: {
            "trade_date": "20260727",
            "market_date": "20260728",
            "rows": [{"code": "000001", "price": 10.8}],
            "generated_at": "2026-07-28T09:35:05",
            "cache_age_seconds": 3.2,
        },
    )
    candidates = _endpoint(service, "/api/v1/mobile/candidates")
    body = candidates(
        date="20260727",
        group="",
        limit=1,
        offset=0,
    )
    assert body["ok"] is True
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["code"] == "000001"
    assert body["meta"]["trade_date"] == "20260727"
    assert body["error"] is None
    assert body["meta"]["request_id"]

    realtime_endpoint = _endpoint(service, "/api/v1/mobile/realtime")
    realtime = realtime_endpoint(candidate_date="", market_date="", limit=20)
    assert realtime["meta"]["is_realtime"] is True
    assert realtime["meta"]["cache_age_seconds"] == 3.2
    assert realtime["data"]["status"] == "cached"


def test_mobile_api_errors_use_stable_envelope(tmp_path):
    service = MobileReadService(_repository(tmp_path))
    candidate_detail = _endpoint(
        service,
        "/api/v1/mobile/candidates/{code}",
    )
    missing = candidate_detail(code="999999", date="20260727")
    assert missing.status_code == 404
    assert json.loads(missing.body)["error"]["code"] == "CANDIDATE_NOT_FOUND"

    invalid = mobile_error_response(
        "VALIDATION_ERROR",
        "请求参数不正确",
        status_code=422,
        details={"errors": [{"loc": ["query", "limit"]}]},
    )
    assert invalid.status_code == 422
    assert json.loads(invalid.body)["error"]["code"] == "VALIDATION_ERROR"

    app = FastAPI()
    app.include_router(create_mobile_router(service))
    openapi = app.openapi()
    operation = openapi["paths"]["/api/v1/mobile/candidates"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]


def test_mobile_permission_paths_reuse_existing_capabilities():
    assert permission_key_for_path("/api/v1/mobile/dashboard") == "overview"
    assert permission_key_for_path("/api/v1/mobile/candidates/000001") == "strategy"
    assert permission_key_for_path("/api/v1/mobile/stocks/000001/daily") == "strategy"
    assert permission_key_for_path("/api/v1/mobile/realtime") == "realtime"
    assert permission_key_for_path("/api/v1/mobile/leaders") == "dragon"
    assert permission_key_for_path("/api/v1/mobile/limitup") == "limitup"
    assert permission_key_for_path("/api/v1/mobile/lhb") == "lhb"
    assert requires_admin("/api/v1/mobile/candidates", "GET") is False
    assert requires_admin("/api/v1/mobile/candidates", "POST") is True
