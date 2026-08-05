"""Read-only mobile API, shared by the future mini program and Web clients."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query, Request

from core.application.mobile_services import MobileReadService
from web.api.mobile.auth_router import create_mobile_auth_router
from web.api.mobile.responses import mobile_error_response, mobile_payload
from web.api.mobile.schemas import (
    CandidateEnvelope,
    CandidateListEnvelope,
    DashboardEnvelope,
    LeaderEnvelope,
    LhbEnvelope,
    LimitupEnvelope,
    MobileEnvelope,
    RealtimeEnvelope,
)
from web.mobile_auth_service import MobileAuthService
from web.permissions import can_access_path

_CAPABILITY_PATHS = {
    "dashboard": "/api/v1/mobile/dashboard",
    "candidates": "/api/v1/mobile/candidates",
    "realtime": "/api/v1/mobile/realtime",
    "leaders": "/api/v1/mobile/leaders",
    "limitup": "/api/v1/mobile/limitup",
    "lhb": "/api/v1/mobile/lhb",
    "stocks": "/api/v1/mobile/stocks/000000",
}


def _date_meta(data: Any) -> tuple[str, str, str]:
    if not isinstance(data, dict):
        return "", "", "ready"
    return (
        str(data.get("trade_date") or ""),
        str(data.get("generated_at") or ""),
        str(data.get("data_status") or data.get("status") or "ready"),
    )


def create_mobile_router(
    service: MobileReadService,
    auth_service: MobileAuthService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/mobile", tags=["mobile"])
    router.include_router(
        create_mobile_auth_router(auth_service or MobileAuthService()),
        tags=["mobile-auth"],
    )

    def response(data: Any, *, realtime: bool = False):
        trade_date, generated_at, status = _date_meta(data)
        return mobile_payload(
            data,
            trade_date=trade_date,
            generated_at=generated_at,
            source_status=status,
            is_realtime=realtime,
            cache_age_seconds=(data.get("cache_age_seconds") if realtime and isinstance(data, dict) else None),
        )

    @router.get("/bootstrap", response_model=MobileEnvelope)
    def bootstrap(request: Request) -> Dict[str, Any]:
        user = dict(request.state.user or {})
        data = service.bootstrap(user)
        data["capabilities"] = [
            capability for capability, path in _CAPABILITY_PATHS.items() if can_access_path(user, path, "GET")
        ]
        return response(data)

    @router.get("/dashboard", response_model=DashboardEnvelope)
    def dashboard(date: str = "") -> Dict[str, Any]:
        return response(service.dashboard(date))

    @router.get("/candidates", response_model=CandidateListEnvelope)
    def candidates(
        date: str = "",
        group: str = "",
        limit: int = Query(default=20, ge=1, le=50),
        offset: int = Query(default=0, ge=0),
    ) -> Dict[str, Any]:
        return response(service.candidates(date, group=group, limit=limit, offset=offset))

    @router.get("/candidates/{code}", response_model=CandidateEnvelope)
    def candidate_detail(code: str, date: str = ""):
        data = service.candidate_detail(code, date)
        if not data:
            return mobile_error_response("CANDIDATE_NOT_FOUND", "未找到该交易日的候选记录", status_code=404)
        return response(data)

    @router.get("/realtime", response_model=RealtimeEnvelope)
    def realtime(
        candidate_date: str = "",
        market_date: str = "",
        limit: int = Query(default=20, ge=1, le=50),
    ) -> Dict[str, Any]:
        return response(
            service.realtime(candidate_date, market_date, limit),
            realtime=True,
        )

    @router.get("/leaders", response_model=LeaderEnvelope)
    def leaders(
        date: str = "",
        lookback: int = Query(default=10, ge=1, le=20),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> Dict[str, Any]:
        return response(service.leaders(date, lookback, limit))

    @router.get("/limitup", response_model=LimitupEnvelope)
    def limitup(date: str = "") -> Dict[str, Any]:
        return response(service.limitup(date))

    @router.get("/lhb", response_model=LhbEnvelope)
    def lhb(date: str = "") -> Dict[str, Any]:
        return response(service.lhb(date))

    @router.get("/stocks/{code}", response_model=MobileEnvelope)
    def stock(code: str, date: str = ""):
        data = service.stock(code, date)
        if not data.get("name") and not data.get("daily") and not data.get("candidate"):
            return mobile_error_response("STOCK_NOT_FOUND", "未找到个股数据", status_code=404)
        return response(data)

    @router.get("/stocks/{code}/daily", response_model=MobileEnvelope)
    def daily(
        code: str,
        date: str = "",
        limit: int = Query(default=120, ge=1, le=500),
    ) -> Dict[str, Any]:
        return response(service.daily_candles(code, date, limit))

    return router


__all__ = ["create_mobile_router"]
