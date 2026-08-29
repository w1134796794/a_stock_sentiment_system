"""Phase 0-3 trading workbench contracts."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from core.application.workbench_service import WorkbenchService
from web.api.workbench.router import create_workbench_router
from web.permissions import permission_key_for_path, visible_menu_groups


class _RepositoryStub:
    @staticmethod
    def list_dates() -> list[str]:
        return ["20260731", "20260730"]


class _ReadServiceStub:
    repository = _RepositoryStub()

    @staticmethod
    def dashboard(trade_date: str = "") -> dict:
        return {
            "trade_date": trade_date or "20260731",
            "market": {
                "regime_label": "强市",
                "emotion_phase": "情绪活跃",
                "market_score": 79.7,
                "limit_up_count": 68,
                "limit_down_count": 4,
                "broken_rate": 18.5,
                "position_scale": 0.6,
                "risk_flags": ["昨日首板溢价不足"],
            },
            "groups": {
                "重点确认": [{"code": "000001", "name": "测试股份"}],
                "盘中观察": [{"code": "000002", "name": "观察股份"}],
                "暂不参与": [{"code": "000003", "name": "回避股份"}],
            },
            "generated_at": "2026-07-31T18:30:00",
            "data_status": "ready",
            "data_completeness": 100.0,
        }

    @staticmethod
    def candidate_detail(code: str, trade_date: str = "") -> dict:
        if code != "000001":
            return {}
        return {
            "code": code,
            "name": "测试股份",
            "trade_date": trade_date or "20260731",
            "action_group": "重点确认",
        }

    @staticmethod
    def leaders(trade_date: str = "", lookback: int = 10, limit: int = 30) -> dict:
        return {
            "trade_date": trade_date,
            "counts": {"核心龙头": 1},
            "role_counts": {"板块龙头": 1},
            "rows": [{
                "code": "000001",
                "name": "测试股份",
                "pool_type": "核心龙头",
                "primary_role": "板块龙头",
                "leader_roles": ["板块龙头"],
                "leader_score": 82.5,
                "lifecycle_state": "确认龙头",
                "leader_time_label": "当日核心龙头",
                "primary_sector": "机器人",
                "sector_status_score": 75.0,
                "pct_chg": 10.0,
            }],
            "generated_at": "2026-07-31T18:31:00",
        }

    @staticmethod
    def limitup(trade_date: str = "") -> dict:
        return {
            "trade_date": trade_date,
            "limit_up_count": 1,
            "limit_down_count": 0,
            "max_board_height": 2,
            "echelon": [{
                "board_height": 2,
                "count": 1,
                "stocks": [{"code": "000001", "name": "测试股份", "pct_chg": 10.0}],
            }],
        }

    @staticmethod
    def lhb(trade_date: str = "") -> dict:
        return {
            "summary": {"stock_count": 1},
            "stocks": [{"code": "000001", "name": "测试股份", "net_buy_yuan": 1e8}],
            "actors": [{"name": "测试席位", "net_buy_yuan": 5e7, "stocks": [{"code": "000001"}]}],
        }

    @staticmethod
    def realtime(candidate_date: str = "", market_date: str = "", limit: int = 20) -> dict:
        return {
            "trade_date": candidate_date,
            "market_date": market_date,
            "generated_at": "2026-07-31T10:00:00",
            "rows": [{
                "code": "000001",
                "name": "测试股份",
                "last_price": 11.0,
                "pct_chg": 10.0,
                "confirm_status": "confirmed",
                "entry_mode_text": "弱转强",
            }],
        }

    @staticmethod
    def stock(code: str, trade_date: str = "") -> dict:
        return {"code": code, "name": "测试股份", "trade_date": trade_date}

    @staticmethod
    def daily_candles(code: str, trade_date: str = "", limit: int = 120) -> dict:
        return {
            "code": code,
            "trade_date": trade_date,
            "items": [{"trade_date": trade_date, "open": 10, "high": 11, "low": 9, "close": 11}],
        }


def _endpoint(service: WorkbenchService, path: str):
    router = create_workbench_router(service)
    return next(route.endpoint for route in router.routes if route.path == path)


def test_workbench_builds_action_summary_and_market_brief():
    service = WorkbenchService(
        _ReadServiceStub(),
        market_context_loader=lambda date: {
            "date": date,
            "indices": [{"name": "上证", "close": 3300, "pct": 0.5}],
            "up_count": 3100,
            "down_count": 2100,
            "vol_word": "放量",
            "vol_pct": 8.9,
            "promotion": {
                "overall": 23.38,
                "rate_1to2": 16.39,
                "rate_2to3": 37.5,
                "rate_3to4": 50.0,
                "rate_high": 62.5,
            },
            "promotion_trend": {
                "score": 64.2,
                "label": "接力升温",
                "slope": 4.8,
                "sample_days": 5,
                "history": [
                    {"trade_date": "20260730", "rate_1to2": 12.0},
                    {"trade_date": "20260731", "rate_1to2": 16.39},
                ],
            },
            "profit_effect": {
                "score": 68.4,
                "label": "赚钱效应较好",
                "trend": "赚钱效应扩散",
                "change_3d": 9.2,
                "up_ratio": 61.0,
                "prev_limit_up_premium": 1.8,
                "promotion_rate": 35.0,
                "promotion_success": 7,
                "promotion_sample": 20,
                "broken_rate": 22.0,
            },
        },
    )
    data = service.dashboard("20260731")

    assert data["available_dates"] == ["20260731", "20260730"]
    assert data["decision_summary"] == {
        "total": 3,
        "actionable": 2,
        "focus": 1,
        "watch": 1,
        "avoid": 1,
    }
    assert data["market_brief"] == "强市，情绪活跃，市场分 80；注意昨日首板溢价不足。"
    assert data["market_context"]["up_count"] == 3100
    assert data["market_context"]["promotion"]["overall"] == 23.38
    assert data["market_context"]["promotion"]["rate_3to4"] == 50.0
    assert data["market_context"]["promotion_trend"]["label"] == "接力升温"
    assert data["market_context"]["profit_effect"]["score"] == 68.4
    assert data["market_context"]["profit_effect"]["promotion_sample"] == 20


def test_workbench_reuses_generated_data_within_cache_window():
    class CountingReadService(_ReadServiceStub):
        def __init__(self) -> None:
            self.dashboard_calls = 0
            self.candidate_calls = 0
            self.realtime_calls = 0

        def dashboard(self, trade_date: str = "") -> dict:
            self.dashboard_calls += 1
            return super().dashboard(trade_date)

        def candidate_detail(self, code: str, trade_date: str = "") -> dict:
            self.candidate_calls += 1
            return super().candidate_detail(code, trade_date)

        def realtime(self, candidate_date: str = "", market_date: str = "", limit: int = 20) -> dict:
            self.realtime_calls += 1
            return super().realtime(candidate_date, market_date, limit)

    read_service = CountingReadService()
    service = WorkbenchService(read_service)

    service.dashboard("20260731")
    service.dashboard("20260731")
    service.candidate_detail("000001", "20260731")
    service.candidate_detail("000001", "20260731")
    service.realtime("20260731", "20260731")
    service.realtime("20260731", "20260731")

    assert read_service.dashboard_calls == 1
    assert read_service.candidate_calls == 1
    assert read_service.realtime_calls == 1


def test_workbench_api_uses_stable_envelope_and_not_found_error():
    service = WorkbenchService(_ReadServiceStub())
    dashboard = _endpoint(service, "/api/v1/workbench")
    result = dashboard(date="20260731")
    assert result["ok"] is True
    assert result["data"]["decision_summary"]["focus"] == 1
    assert result["meta"]["trade_date"] == "20260731"

    detail = _endpoint(service, "/api/v1/workbench/candidates/{code}")
    missing = detail(code="999999", date="20260731")
    assert missing.status_code == 404
    assert json.loads(missing.body)["error"]["code"] == "CANDIDATE_NOT_FOUND"


def test_workbench_menu_and_api_share_permission_key():
    user = {"role": "viewer"}
    menu_items = [
        item
        for group in visible_menu_groups(user)
        for item in group.get("items", [])
    ]

    assert any(item["key"] == "workbench" for item in menu_items)
    assert permission_key_for_path("/workspace") == "workbench"
    assert permission_key_for_path("/api/v1/workbench") == "workbench"
    assert permission_key_for_path("/api/v1/workbench/candidates/000001") == "workbench"


def test_phase4_intelligence_combines_mainline_leader_limitup_and_lhb():
    service = WorkbenchService(_ReadServiceStub())
    data = service.intelligence("20260731")

    assert data["limitup"]["max_board_height"] == 2
    assert data["leaders"]["rows"][0]["primary_role"] == "板块龙头"
    assert data["lhb"]["stocks"][0]["net_buy_yuan"] == 1e8
    assert data["mainlines"][0]["name"] == "机器人"
    assert data["mainlines"][0]["leader_count"] == 1


def test_phase5_realtime_refreshes_only_during_trade_session():
    service = WorkbenchService(
        _ReadServiceStub(),
        clock=lambda: datetime(2026, 7, 31, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    service._calendar = type("Calendar", (), {"is_trade_date": staticmethod(lambda _date: True)})()
    live = service.realtime("20260730", "20260731")
    assert live["refresh_policy"]["auto_refresh"] is True
    assert live["rows"][0]["confirm_status"] == "confirmed"

    service._clock = lambda: datetime(2026, 7, 31, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    closed = service.realtime("20260730", "20260731")
    assert closed["refresh_policy"]["auto_refresh"] is False
    assert closed["refresh_policy"]["session_label"] == "已收盘"


def test_phase6_stock_workspace_and_new_routes_use_stable_envelopes():
    service = WorkbenchService(_ReadServiceStub())
    stock = service.stock_workspace("000001", "20260731")
    assert stock["name"] == "测试股份"
    assert stock["candles"][0]["close"] == 11

    intelligence = _endpoint(service, "/api/v1/workbench/intelligence")
    assert intelligence(date="20260731")["ok"] is True

    realtime = _endpoint(service, "/api/v1/workbench/realtime")
    assert realtime(candidate_date="20260731", market_date="20260731", limit=20)["ok"] is True

    stock_endpoint = _endpoint(service, "/api/v1/workbench/stocks/{code}")
    assert stock_endpoint(code="000001", date="20260731", limit=120)["ok"] is True
