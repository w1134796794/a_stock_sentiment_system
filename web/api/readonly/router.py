"""GET-only integration routes, gated by ReadonlyAccess before session middleware."""

from __future__ import annotations

import math
from enum import Enum

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from core.application.readonly_service import ReadonlyService, ReadUnavailable
from web.readonly_access import PREFIX


class LogKind(str, Enum):
    preopen = "preopen"
    postclose = "postclose"


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def create_readonly_router(service_factory=ReadonlyService):
    router = APIRouter(prefix=PREFIX, tags=["readonly-integration"])

    def response(operation):
        try:
            payload = _finite(operation(service_factory()))
            result = JSONResponse(payload)
            if len(result.body) > 2 * 1024 * 1024:
                raise ReadUnavailable("Response exceeds limit")
            return result
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid_parameters"}, status_code=400)
        except (ReadUnavailable, OSError, TypeError, KeyError, AttributeError):
            return JSONResponse({"ok": False, "error": "data_temporarily_unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"ok": False, "error": "data_temporarily_unavailable"}, status_code=503)

    @router.get("/health")
    def health():
        return response(lambda service: service.health())

    @router.get("/dates")
    def dates():
        return response(lambda service: service.dates())

    @router.get("/quotes")
    def quotes(codes: str = Query(..., min_length=6, max_length=200)):
        return response(lambda service: service.quotes(codes))

    @router.get("/candidates")
    def candidates(
        date: str = Query(..., min_length=8, max_length=8),
        code: str = Query("", max_length=9),
        offset: int = Query(0, ge=0, le=10000),
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(lambda service: service.candidates(date, code=code, offset=offset, limit=limit))

    @router.get("/signals")
    def signals(
        date: str = Query(..., min_length=8, max_length=8),
        code: str = Query("", max_length=9),
        offset: int = Query(0, ge=0, le=10000),
        limit: int = Query(50, ge=1, le=100),
    ):
        return response(lambda service: service.signals(date, code=code, offset=offset, limit=limit))

    @router.get("/minutes/{code}")
    def minutes(
        code: str,
        date: str = Query(..., min_length=8, max_length=8),
        offset: int = Query(0, ge=0, le=2000),
        limit: int = Query(500, ge=1, le=500),
    ):
        return response(lambda service: service.minutes(code, date, offset=offset, limit=limit))

    @router.get("/logs/summary")
    def logs(date: str = Query(..., min_length=8, max_length=8), kind: LogKind = LogKind.preopen):
        return response(lambda service: service.log_summary(date, kind.value))

    return router
