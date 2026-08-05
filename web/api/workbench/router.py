"""Read-only API used by the Web trading workbench."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from core.application.workbench_service import WorkbenchService
from web.api.mobile.responses import mobile_error_response, mobile_payload


def create_workbench_router(service: WorkbenchService | None = None) -> APIRouter:
    read_service = service or WorkbenchService()
    router = APIRouter(prefix="/api/v1/workbench", tags=["workbench"])

    @router.get("")
    def dashboard(date: str = "") -> Dict[str, Any]:
        data = read_service.dashboard(date)
        return mobile_payload(
            data,
            trade_date=str(data.get("trade_date") or ""),
            generated_at=str(data.get("generated_at") or ""),
            source_status=str(data.get("data_status") or "ready"),
        )

    @router.get("/candidates/{code}")
    def candidate_detail(code: str, date: str = ""):
        data = read_service.candidate_detail(code, date)
        if not data:
            return mobile_error_response(
                "CANDIDATE_NOT_FOUND",
                "未找到该交易日的候选记录",
                status_code=404,
            )
        return mobile_payload(
            data,
            trade_date=str(data.get("trade_date") or date or ""),
            source_status="ready",
        )

    @router.get("/intelligence")
    def intelligence(date: str = "") -> Dict[str, Any]:
        data = read_service.intelligence(date)
        return mobile_payload(
            data,
            trade_date=str(data.get("trade_date") or date or ""),
            generated_at=str(data.get("generated_at") or ""),
            source_status=str(data.get("data_status") or "ready"),
        )

    @router.get("/realtime")
    def realtime(
        candidate_date: str = "",
        market_date: str = "",
        limit: int = Query(default=50, ge=1, le=50),
    ) -> Dict[str, Any]:
        data = read_service.realtime(candidate_date, market_date, limit)
        return mobile_payload(
            data,
            trade_date=str(data.get("trade_date") or candidate_date or ""),
            generated_at=str(data.get("generated_at") or ""),
            source_status=str(data.get("status") or "cache_empty"),
            is_realtime=True,
            cache_age_seconds=data.get("cache_age_seconds"),
        )

    @router.get("/stocks/{code}")
    def stock_workspace(
        code: str,
        date: str = "",
        limit: int = Query(default=120, ge=20, le=300),
    ):
        data = read_service.stock_workspace(code, date, limit)
        if not data.get("name") and not data.get("daily") and not data.get("candles"):
            return mobile_error_response(
                "STOCK_NOT_FOUND",
                "未找到该股票的本地行情数据",
                status_code=404,
            )
        return mobile_payload(
            data,
            trade_date=str(data.get("trade_date") or date or ""),
            source_status="ready",
        )

    return router


__all__ = ["create_workbench_router"]
