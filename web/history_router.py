"""Admin-only historical job controls; handlers never fetch market data."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse


def history_router(factory, templates):
    api = APIRouter(prefix="/api/admin/history-workflows")

    def respond(operation):
        try:
            return {"ok": True, "data": operation()}
        except FileNotFoundError:
            return JSONResponse({"ok": False, "message": "历史任务或报告不存在"}, status_code=404)
        except (ValueError, TypeError, TimeoutError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)

    @api.get("")
    def recent():
        return respond(lambda: factory().recent())

    @api.post("")
    def create(payload: dict = Body(default={})):
        return respond(
            lambda: factory().create(
                str(payload.get("start") or ""), str(payload.get("end") or ""), payload.get("capital")
            )
        )

    @api.get("/{job_id}")
    def get(job_id: str):
        return respond(lambda: factory().get(job_id))

    @api.post("/{job_id}/{phase}")
    def start(job_id: str, phase: str):
        return respond(lambda: factory().cancel(job_id) if phase == "cancel" else factory().start(job_id, phase))

    @api.get("/{job_id}/report/{kind}")
    def report(job_id: str, kind: str):
        from core.portfolio.history_workflow import read_json

        def load():
            if kind == "log":
                path = factory().directory(job_id) / "worker.log"
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 16000))
                    return stream.read().decode("utf-8", errors="replace")
            if kind not in {"validation", "result", "reconciliation"}:
                raise ValueError("未知报告类型")
            manager = factory()
            state = manager.get(job_id)
            report = read_json(manager.directory(job_id) / f"{kind}.json")
            if kind in {"result", "reconciliation"}:
                stage = "run" if kind == "result" else "reconcile"
                if state["stages"].get(stage) != "complete" or report.get("frozen_version") != state.get(
                    "frozen_version"
                ):
                    raise ValueError("报告不属于当前已完成的冻结阶段")
            return report

        return respond(load)

    router = APIRouter()
    router.include_router(api)

    @router.get("/market-backfill")
    def backfill_page(request: Request):
        from config.settings import PAPER_INITIAL_CAPITAL

        return templates.TemplateResponse(
            request, "market_backfill.html", {"backfill_view": True, "paper_initial_capital": PAPER_INITIAL_CAPITAL}
        )

    return router
