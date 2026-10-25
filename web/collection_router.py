"""One collection workbench; strategy controls deliberately live elsewhere."""
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse


def collection_router(templates):
    router = APIRouter()

    @router.get("/api/admin/market-collection/defaults")
    def defaults():
        from backtest.trade_calendar import TradeCalendar
        from core.etl.stage_status import post_close_data_ready
        from core.realtime.trading_session import shanghai_now
        calendar, now = TradeCalendar(), shanghai_now()
        today = now.strftime("%Y%m%d")
        after_close = calendar.is_trade_date(today) and post_close_data_ready(today, now=now)
        last = today if after_close else calendar.prev(today)
        return {"ok": True, "data": {"today": today, "postclose": last,
            "preopen": calendar.next(last), "intraday": today, "calendar_verified": calendar.is_real}}

    @router.get("/market-collection")
    def page(request: Request):
        from config.settings import PAPER_INITIAL_CAPITAL
        return templates.TemplateResponse(request, "market_backfill.html", {
            "backfill_view": True, "paper_initial_capital": PAPER_INITIAL_CAPITAL})

    @router.get("/api/admin/market-collection/status")
    def status(phase: str, date: str):
        from core.operations.collection_center import collection_status
        try:
            return {"ok": True, "data": collection_status(phase, date)}
        except (ValueError, OSError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)

    @router.post("/api/admin/market-collection/preopen")
    def prepare(payload: dict = Body(default={})):
        from core.operations.collection_center import start_preopen
        try:
            return {"ok": True, "data": start_preopen(payload.get("date"))}
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)

    @router.get("/api/admin/market-collection/log")
    def log(date: str, phase: str = "preopen"):
        from core.operations.collection_center import preopen_log
        try:
            return {"ok": True, "data": preopen_log(date, phase)}
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)

    return router
