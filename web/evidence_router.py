"""Read-only evidence endpoints; authentication and route permissions live in app middleware."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse


def evidence_router(views_factory, templates, date_factory):
    router = APIRouter()

    def response(operation):
        try:
            return operation()
        except (ValueError, TypeError) as exc:
            return JSONResponse(dict(ok=False, message=str(exc)), status_code=400)
        except Exception:
            from loguru import logger

            logger.exception("Read-only evidence storage unavailable")
            return JSONResponse(dict(ok=False, message="证据存储暂不可读"), status_code=503)

    @router.get("/market-evidence")
    def page(request: Request):
        return templates.TemplateResponse(request, "market_evidence.html", dict(market_date=date_factory()))

    @router.get("/api/market-evidence/stock/{code}")
    def stock(code: str, market_date: str):
        return response(lambda: views_factory().stock(market_date, code))

    @router.get("/api/admin/market-evidence/research/{code}/{kind}")
    def research(
        code: str,
        kind: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        as_of: str | None = None,
    ):
        return response(lambda: views_factory().research_rows(code, kind, offset=offset, limit=limit, as_of=as_of))

    @router.get("/api/market-evidence/boards")
    def boards(market_date: str):
        return response(lambda: views_factory().board(market_date))

    @router.get("/api/admin/market-evidence/diagnostics")
    def diagnostics(market_date: str):
        return response(lambda: views_factory().diagnostics(market_date))

    @router.get("/api/admin/market-evidence/raw/{code}/{kind}")
    def raw(code: str, kind: str, market_date: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        return response(lambda: views_factory().raw(market_date, code, kind, offset=offset, limit=limit))

    @router.get("/api/admin/market-evidence/experiments")
    def experiments():
        return response(lambda: views_factory().experiments())

    return router
