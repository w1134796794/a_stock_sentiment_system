"""
FastAPI 应用（P1）—— 明日计划看板 + 18-sheet 浏览（只读）。

启动：
    python run_web.py
或：
    uvicorn web.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import json

from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.settings import SNAPSHOT_DIR, KB_DB_PATH, APP_DB_PATH, WINRATE_PATH
from snapshot.reader import SnapshotReader

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
reader = SnapshotReader(SNAPSHOT_DIR)

app = FastAPI(title="A股情绪系统 · 明日计划看板", docs_url="/api/docs")

_static_dir = BASE / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    latest = reader.latest()
    if latest:
        return RedirectResponse(url=f"/report/{latest}")
    # 无快照：返回空状态页
    return HTMLResponse(_empty_state_html())


@app.get("/report/{date}", response_class=HTMLResponse)
def report(request: Request, date: str) -> Any:
    snapshot = reader.load(date)
    dates = reader.list_dates()
    if snapshot is None:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"snapshot": None, "date": date, "dates": dates},
            status_code=404,
        )
    market = snapshot.get("market", {}) or {}
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "snapshot": snapshot,
            "date": date,
            "dates": dates,
            "plans": snapshot.get("trade_plans", {}).get("rows", []),
            "market": market,
            "risk_gate": snapshot.get("risk_gate"),
            "sections": snapshot.get("sections", []),
            "winrate": _load_winrate(),
            "cur_cycle": market.get("cycle_name") or "",
        },
    )


def _load_winrate() -> Optional[Dict]:
    try:
        if WINRATE_PATH.exists():
            return json.loads(WINRATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> Any:
    """参数配置页：把全系统可调参数开放到网页编辑。"""
    from config.config_registry import build_registry

    return templates.TemplateResponse(
        request,
        "config.html",
        {"registry": build_registry(), "dates": reader.list_dates()},
    )


@app.get("/api/config")
def api_config_get() -> Any:
    from config.config_registry import build_registry

    return JSONResponse(build_registry())


@app.post("/api/config")
def api_config_save(payload: dict = Body(...)) -> Any:
    """保存一批参数改动。body: {updates: [{scope, path, value}, ...]}"""
    from config.config_registry import apply_updates

    updates = (payload or {}).get("updates", [])
    if not isinstance(updates, list):
        return JSONResponse({"error": "updates 必须是数组"}, status_code=400)
    result = apply_updates(updates)
    return JSONResponse(result)


@app.post("/api/config/reset")
def api_config_reset(payload: dict = Body(default={})) -> Any:
    """重置参数。body: {scope?, path?}；都不传则清空全部覆盖。"""
    from config.config_registry import reset

    scope = (payload or {}).get("scope")
    path = (payload or {}).get("path")
    return JSONResponse(reset(scope=scope, path=path))


@app.get("/report/{date}/section/{idx}", response_class=HTMLResponse)
def section_fragment(request: Request, date: str, idx: int) -> Any:
    """HTMX 片段：返回某个 section 的表格 HTML。"""
    snapshot = reader.load(date)
    sections: List[Dict] = (snapshot or {}).get("sections", [])
    if not snapshot or idx < 0 or idx >= len(sections):
        return HTMLResponse('<div class="p-6 text-slate-400">无此数据</div>', status_code=404)
    section = sections[idx]
    return templates.TemplateResponse(
        request,
        "partials/section.html",
        {"section": section},
    )


# ----------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------
@app.get("/api/dates")
def api_dates() -> Any:
    return JSONResponse({"dates": reader.list_dates(), "latest": reader.latest()})


@app.get("/api/winrate")
def api_winrate() -> Any:
    data = _load_winrate()
    if data is None:
        return JSONResponse({"error": "not built", "hint": "运行 python scripts/build_winrate.py"},
                            status_code=404)
    return JSONResponse(data)


@app.get("/api/snapshot/{date}")
def api_snapshot(date: str) -> Any:
    snapshot = reader.load(date)
    if snapshot is None:
        return JSONResponse({"error": "not found", "date": date}, status_code=404)
    return JSONResponse(snapshot)


# ----------------------------------------------------------------------
# P3：AI 每日解读 + 问答
# ----------------------------------------------------------------------
@app.get("/report/{date}/brief", response_class=HTMLResponse)
def brief_fragment(request: Request, date: str) -> Any:
    """HTMX 片段：当日 AI 解读（缓存优先；未配置 key 时返回提示）。"""
    from kb.brief import generate_brief

    snapshot = reader.load(date)
    if snapshot is None:
        return HTMLResponse('<div class="text-slate-500 text-sm">无快照</div>')
    result = generate_brief(snapshot, KB_DB_PATH)
    return templates.TemplateResponse(request, "partials/brief.html", {"brief": result})


@app.get("/api/daily-brief/{date}")
def api_daily_brief(date: str, force: bool = False) -> Any:
    from kb.brief import generate_brief

    snapshot = reader.load(date)
    if snapshot is None:
        return JSONResponse({"error": "not found", "date": date}, status_code=404)
    return JSONResponse(generate_brief(snapshot, KB_DB_PATH, force=force))


@app.post("/api/chat")
def api_chat(payload: dict = Body(...)) -> Any:
    """RAG 问答，SSE 流式返回。body: {question, date?}"""
    from kb.embeddings import get_embedder
    from kb.llm_client import get_llm_client
    from kb.qa import build_chat_messages

    question = (payload or {}).get("question", "").strip()
    date = (payload or {}).get("date")
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    messages, _debug = build_chat_messages(
        question, KB_DB_PATH, APP_DB_PATH, embedder=get_embedder(), date=date)
    client = get_llm_client()

    def event_stream():
        for piece in client.chat_stream(messages):
            yield f"data: {json.dumps({'delta': piece}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _empty_state_html() -> str:
    return (
        "<html><head><meta charset='utf-8'><title>暂无数据</title>"
        "<script src='https://cdn.tailwindcss.com'></script></head>"
        "<body class='bg-slate-950 text-slate-200 flex items-center justify-center h-screen'>"
        "<div class='text-center'>"
        "<div class='text-2xl font-semibold mb-2'>暂无快照数据</div>"
        "<div class='text-slate-400'>请先运行一次收盘分析（main.py）生成 Excel，"
        "系统会同步落出每日快照。</div>"
        "</div></body></html>"
    )
