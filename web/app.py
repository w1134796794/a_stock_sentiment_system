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


def _money(value: Any, signed: bool = False) -> str:
    """千分位金额格式化（Jinja 的 % 格式化不支持逗号分组，故用自定义过滤器）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{v:+,.0f}" if signed else f"{v:,.0f}"


templates.env.filters["money"] = _money

# ----------------------------------------------------------------------
# 数据浏览分类：把原「明日计划 · 数据浏览」里的 section 按主题拆成独立菜单。
# signals=True 表示纳入 kind=="signals" 的策略模式 section（弱转强/二板定龙/…）。
# 注：龙头池 / 走弱池 由专门的「龙头池」页（/dragon）承载，这里不重复。
# ----------------------------------------------------------------------
DATA_CATEGORIES: List[Dict[str, Any]] = [
    {"key": "strategy", "label": "策略信号", "signals": True, "names": []},
    {"key": "sector", "label": "板块题材", "signals": False,
     "names": ["热点概念", "热点行业", "概念持续性", "行业持续性", "主线主题"]},
    {"key": "limitup", "label": "涨停梯队", "signals": False,
     "names": ["涨停梯队", "概念连板梯队"]},
    {"key": "capital", "label": "龙虎榜资金", "signals": False,
     "names": ["龙虎榜", "资金流向", "筹码结构"]},
    {"key": "review", "label": "复盘统计", "signals": False,
     "names": ["复盘总结", "周期模式胜率", "因子原始数据", "交易计划", "风控闸门"]},
]
_CATEGORY_BY_KEY = {c["key"]: c for c in DATA_CATEGORIES}

app = FastAPI(title="A股情绪系统 · 明日计划看板", docs_url="/api/docs")

_static_dir = BASE / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Any:
    """概览页：健康检查 + 关键产物统计（管理工具首页）。"""
    from desktop.status import overview

    return templates.TemplateResponse(request, "overview.html", {"ov": overview()})


@app.get("/report", response_class=HTMLResponse)
def report_index() -> Any:
    latest = reader.latest()
    return RedirectResponse(url=f"/report/{latest}" if latest else "/")


@app.get("/dragon", response_class=HTMLResponse)
def dragon_page(request: Request) -> Any:
    """龙头池 / 走弱池浏览（直读 dragon_pools.json）。"""
    from desktop.status import dragon_pools

    return templates.TemplateResponse(request, "dragon.html", {"dp": dragon_pools()})


@app.get("/run", response_class=HTMLResponse)
def run_page(request: Request) -> Any:
    """运行分析页：一键执行收盘分析并实时查看日志。"""
    from desktop.runner import CONTROLLER

    return templates.TemplateResponse(request, "run.html", {"run": CONTROLLER.status(0)})


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "logs.html", {})


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> Any:
    from desktop.status import overview

    return templates.TemplateResponse(request, "about.html", {"ov": overview()})


# ----------------------------------------------------------------------
# 管理工具 JSON API（运行 / 日志 / 概览）
# ----------------------------------------------------------------------
@app.post("/api/run")
def api_run(payload: dict = Body(default={})) -> Any:
    from desktop.runner import CONTROLLER

    date = (payload or {}).get("date")
    ok, msg = CONTROLLER.start(date)
    return JSONResponse({"started": ok, "message": msg})


@app.get("/api/run/status")
def api_run_status(since: int = 0) -> Any:
    from desktop.runner import CONTROLLER

    return JSONResponse(CONTROLLER.status(since))


@app.get("/api/logs")
def api_logs(lines: int = 500) -> Any:
    from desktop.status import tail_log

    return JSONResponse({"text": tail_log(lines)})


@app.get("/api/overview")
def api_overview() -> Any:
    from desktop.status import overview

    return JSONResponse(overview())


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


@app.get("/data/{cat}", response_class=HTMLResponse)
def data_index(cat: str) -> Any:
    """数据浏览分类入口：跳转到最新交易日。"""
    if cat not in _CATEGORY_BY_KEY:
        return RedirectResponse(url="/")
    latest = reader.latest()
    return RedirectResponse(url=f"/data/{cat}/{latest}" if latest else "/")


@app.get("/data/{cat}/{date}", response_class=HTMLResponse)
def data_browse(request: Request, cat: str, date: str) -> Any:
    """某分类下的 section 浏览页（复用 /report/{date}/section/{idx} 片段）。"""
    category = _CATEGORY_BY_KEY.get(cat)
    if category is None:
        return HTMLResponse('<div class="p-6 text-slate-400">无此分类</div>', status_code=404)
    snapshot = reader.load(date)
    return templates.TemplateResponse(
        request,
        "data_browse.html",
        {
            "category": category,
            "cat": cat,
            "date": date,
            "dates": reader.list_dates(),
            "sections": (snapshot or {}).get("sections", []),
            "snapshot": snapshot,
        },
    )


@app.get("/ask", response_class=HTMLResponse)
def ask_index(request: Request) -> Any:
    """问 AI 入口：默认以最新交易日为上下文。"""
    latest = reader.latest()
    if latest:
        return RedirectResponse(url=f"/ask/{latest}")
    return templates.TemplateResponse(request, "ask.html", {"date": None, "dates": []})


@app.get("/ask/{date}", response_class=HTMLResponse)
def ask_page(request: Request, date: str) -> Any:
    return templates.TemplateResponse(
        request, "ask.html", {"date": date, "dates": reader.list_dates()}
    )


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request, run: Optional[str] = None) -> Any:
    """模拟交易结果：汇总指标 + 净值曲线 + 逐笔交易 + 模式表现。"""
    from desktop.backtest import backtest_overview

    return templates.TemplateResponse(
        request, "backtest.html", {"bt": backtest_overview(run)}
    )


@app.get("/drawdown", response_class=HTMLResponse)
def drawdown_page(request: Request, run: Optional[str] = None) -> Any:
    """回撤分析：水下回撤曲线 + 最大回撤 + 回撤区间 + 最差交易。"""
    from desktop.backtest import drawdown_overview

    return templates.TemplateResponse(
        request, "drawdown.html", {"dd": drawdown_overview(run)}
    )


@app.get("/api/backtest/runs")
def api_backtest_runs() -> Any:
    from desktop.backtest import runs_meta

    return JSONResponse({"runs": runs_meta()})


@app.post("/api/backtest/run")
def api_backtest_run(payload: dict = Body(default={})) -> Any:
    """启动一次回测（重新生成净值/交易/回撤）。body: {start_date?, end_date?, initial_capital?}"""
    from desktop.runner import BACKTEST_CONTROLLER

    p = payload or {}
    ok, msg = BACKTEST_CONTROLLER.start(
        p.get("start_date"), p.get("end_date"), p.get("initial_capital"))
    return JSONResponse({"started": ok, "message": msg})


@app.get("/api/backtest/run/status")
def api_backtest_run_status(since: int = 0) -> Any:
    from desktop.runner import BACKTEST_CONTROLLER

    return JSONResponse(BACKTEST_CONTROLLER.status(since))


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

