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

from config.settings import SNAPSHOT_DIR, KB_DB_PATH, APP_DB_PATH, WINRATE_PATH, TUSHARE_TOKEN, CACHE_DIR
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


def _num2(value: Any) -> Any:
    """数值统一保留两位小数；整数、布尔与非数值（字符串/列表/字典等）原样返回。

    用于表格单元格：避免 14.658499999999998 这类浮点尾数直接显示。
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


templates.env.filters["num2"] = _num2

# ----------------------------------------------------------------------
# 表头英文列名 → 中文展示名。
# 快照里部分 section（策略信号 / 板块题材 / 风控闸门 / 因子原始数据 …）的列名是英文，
# 这里只翻译「表头显示」，底层数据键（row[col]）保持不变，故无需重生成快照即可生效。
# 因子类（D*/E*）命名与导出报表（report_generator_v2）保持一致。
# ----------------------------------------------------------------------
COLUMN_LABELS: Dict[str, str] = {
    # 策略信号
    "pattern_type": "模式类型",
    "stock_code": "股票代码",
    "stock_name": "股票名称",
    "confidence": "置信度",
    "description": "信号描述",
    "key_metrics": "关键指标",
    "entry_price": "入场价",
    "stop_loss": "止损价",
    "take_profit": "止盈价",
    "position_size": "建议仓位",
    "validation_rules": "校验规则",
    "l2_industry": "二级行业",
    "is_dual_resonance": "双重共振",
    # 风控闸门
    "original_position_pct": "原始仓位",
    "final_position_pct": "风控后仓位",
    "action": "风控动作",
    "reason_text": "风控原因",
    # 板块题材（热点概念 / 热点行业）
    "ts_code": "代码",
    "name": "名称",
    "type": "类型",
    "pct_change": "涨跌幅",
    "amount": "成交额",
    "vol": "成交量",
    "member_count": "成分股数",
    "limit_cpt_rank": "涨停概念排名",
    "limit_cpt_score": "涨停概念评分",
    "limit_up_count": "涨停家数",
    "limit_cons_count": "连板家数",
    "rank": "排名",
    "price_score": "价格评分",
    "avg_amount": "平均成交额",
    "amount_rank": "成交额排名",
    "amount_score": "成交额评分",
    "limit_score": "涨停评分",
    "momentum_score": "动量评分",
    "composite_score": "综合评分",
    "is_hot": "是否热点",
    "is_hot_concept": "是否热点概念",
    "is_hot_industry": "是否热点行业",
    # 因子原始数据
    "tech_D1_n_day_high_low": "D1 N日新高",
    "tech_D2_vol_price_coord": "D2 量价配合",
    "tech_D3_seal_strength": "D3 封板强度",
    "tech_D4_turnover_health": "D4 换手健康",
    "tech_D5_ma_bull_align": "D5 均线多头",
    "mf_E1_main_net_ratio": "E1 主力净占比",
    "mf_E2_retail_net_ratio": "E2 散户净占比",
    "mf_E3_large_buy_ratio": "E3 大单买入占比",
    "mf_E4_moneyflow_trend": "E4 资金趋势",
    # 其它
    "_date_list": "日期明细",
}


def _col_label(col: Any) -> str:
    """表头英文列名 → 中文展示名；未收录的列原样返回。"""
    return COLUMN_LABELS.get(str(col), str(col))


templates.env.filters["col_label"] = _col_label

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


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock_detail_page(request: Request, code: str, date: Optional[str] = None) -> Any:
    """个股详情：分时、日K、竞价摘要、信号与风控上下文。"""
    latest = date or reader.latest()
    snapshot = reader.load(latest) if latest else None
    context = _find_stock_context(snapshot, code)
    return templates.TemplateResponse(
        request,
        "stock_detail.html",
        {
            "code": _normalize_stock_code(code),
            "date": latest,
            "dates": reader.list_dates(),
            "stock_name": context.get("name") or "",
            "context": context,
        },
    )


@app.get("/api/stock/{code}/chart")
def api_stock_chart(code: str, date: Optional[str] = None, daily_count: int = 120) -> Any:
    """Chart payload for stock detail page."""
    from core.data.data_manager_main import DataManager
    from core.utils.stock_code_utils import StockCodeUtils

    trade_date = date or reader.latest()
    if not trade_date:
        return JSONResponse({"error": "no snapshot date"}, status_code=404)

    ts_code = StockCodeUtils.standardize_code(code, add_suffix=True)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
    intraday_df = dm.get_stock_tick(ts_code, trade_date)
    daily_df = dm.get_kline(ts_code, period="day", count=max(20, min(int(daily_count or 120), 300)))
    auction = dm.get_auction_data(ts_code, trade_date)

    snapshot = reader.load(trade_date)
    context = _find_stock_context(snapshot, ts_code)

    return JSONResponse({
        "code": StockCodeUtils.standardize_code(ts_code, add_suffix=False),
        "ts_code": ts_code,
        "name": context.get("name") or "",
        "date": trade_date,
        "intraday": _df_to_intraday_line(intraday_df, trade_date),
        "daily": _df_to_daily_candles(daily_df),
        "auction": auction or {},
        "plans": context.get("plans") or [],
        "signals": context.get("signals") or [],
        "risk": context.get("risk") or [],
    })


def _confirm_date_options() -> tuple[List[str], Optional[str]]:
    """可确认日(T+1) = 每个复盘快照(T)的「次一交易日」。

    下拉框列的是确认日而非快照日：例如最近复盘是 20260605(周五)，则默认确认日
    为 20260608(周一)——即「次日开盘要确认的那天」。计划由引擎按 prev_trade_date 自动回取。
    """
    snaps = reader.list_dates()  # 已按日期倒序
    if not snaps:
        return [], None
    try:
        from core.utils.date_utils import get_next_trade_date
        options = sorted({get_next_trade_date(d) for d in snaps}, reverse=True)
    except Exception:  # noqa: BLE001 - 交易日历缺失时退回快照日，至少可用
        options = snaps
    return options, (options[0] if options else None)


@app.get("/auction", response_class=HTMLResponse)
def auction_confirm_page(request: Request, date: Optional[str] = None) -> Any:
    """盘前竞价确认：用昨日(T)复盘计划比对今日(T+1)真实集合竞价，给操作建议。"""
    options, default = _confirm_date_options()
    selected = date or default
    return templates.TemplateResponse(
        request,
        "auction_confirm.html",
        {"date": selected, "dates": options},
    )


@app.get("/api/auction-confirm")
def api_auction_confirm(date: Optional[str] = None) -> Any:
    """对 date(=T+1) 做盘前竞价确认；计划取自前一交易日(T)的快照。"""
    from core.data.data_manager_main import DataManager
    from core.execution.auction_confirm import AuctionConfirmer

    confirm_date = date or _confirm_date_options()[1]
    if not confirm_date:
        return JSONResponse({"error": "no snapshot date"}, status_code=404)
    try:
        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
        result = AuctionConfirmer(dm, reader).confirm(confirm_date)
        return JSONResponse(result)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


def _load_winrate() -> Optional[Dict]:
    try:
        if WINRATE_PATH.exists():
            return json.loads(WINRATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _normalize_stock_code(code: str) -> str:
    from core.utils.stock_code_utils import StockCodeUtils

    return StockCodeUtils.standardize_code(code, add_suffix=False)


def _find_stock_context(snapshot: Optional[Dict], code: str) -> Dict[str, Any]:
    """Find plan/signal/risk rows for the stock from a snapshot."""
    if not snapshot:
        return {"name": "", "plans": [], "signals": [], "risk": []}
    pure = _normalize_stock_code(code)
    plans = []
    signals = []
    risk = []
    name = ""

    for row in (snapshot.get("trade_plans", {}) or {}).get("rows", []) or []:
        row_code = _normalize_stock_code(row.get("股票代码") or row.get("stock_code") or "")
        if row_code == pure:
            plans.append(row)
            name = name or row.get("股票名称") or row.get("stock_name") or ""

    for section in snapshot.get("sections", []) or []:
        rows = section.get("rows") or []
        for row in rows:
            row_code = _normalize_stock_code(
                row.get("股票代码") or row.get("stock_code") or row.get("代码") or ""
            )
            if row_code != pure:
                continue
            if section.get("kind") == "signals":
                item = dict(row)
                item["_section"] = section.get("name")
                signals.append(item)
            if section.get("name") == "风控闸门":
                risk.append(row)
            name = name or row.get("股票名称") or row.get("stock_name") or row.get("名称") or ""

    return {"name": name, "plans": plans, "signals": signals, "risk": risk}


def _date_to_ts(date_str: Any, time_str: Any = "15:00:00") -> int:
    from datetime import datetime

    date_s = str(date_str or "").replace("-", "")
    if len(date_s) != 8:
        return 0
    time_s = str(time_str or "15:00:00")
    if len(time_s) == 5:
        time_s += ":00"
    try:
        dt = datetime.strptime(f"{date_s} {time_s[:8]}", "%Y%m%d %H:%M:%S")
        return int(dt.timestamp())
    except Exception:
        return 0


def _df_to_daily_candles(df) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        date = r.get("trade_date")
        ts = _date_to_ts(date, r.get("time") or "15:00:00")
        if not ts:
            continue
        rows.append({
            "time": ts,
            "open": float(r.get("open") or 0),
            "high": float(r.get("high") or 0),
            "low": float(r.get("low") or 0),
            "close": float(r.get("close") or 0),
        })
    return [r for r in rows if r["open"] or r["close"]]


def _df_to_intraday_line(df, trade_date: str) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    import calendar
    from datetime import datetime

    rows: Dict[int, Dict[str, Any]] = {}
    for _, r in df.iterrows():
        date_s = str(r.get("date") or r.get("trade_date") or trade_date or "").replace("-", "")
        time_s = str(r.get("time") or "09:30:00")
        if len(time_s) == 5:
            time_s += ":00"
        if len(date_s) != 8:
            continue
        value = r.get("close")
        if value is None:
            continue
        try:
            dt = datetime.strptime(f"{date_s} {time_s[:8]}", "%Y%m%d %H:%M:%S")
            # 视作 UTC（timegm），让 lightweight-charts 的 UTC 轴直接显示北京交易时段(09:30–15:00)
            ts = calendar.timegm(dt.timetuple())
            v = float(value)
        except (TypeError, ValueError):
            continue
        # 按时间去重（lightweight-charts 要求时间唯一且递增）
        rows[ts] = {"time": ts, "value": v}
    return [rows[t] for t in sorted(rows)]


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
def section_fragment(request: Request, date: str, idx: int, view: str = "table") -> Any:
    """HTMX 片段：返回某个 section 的明细 HTML。

    view="table"  普通宽表（默认，兼容原有调用）；
    view="detail" 主从视图：紧凑列表 + 点击行展开「全字段卡」，用于字段过多的分类
                  （策略信号 / 板块题材），避免横向滚动。
    """
    snapshot = reader.load(date)
    sections: List[Dict] = (snapshot or {}).get("sections", [])
    if not snapshot or idx < 0 or idx >= len(sections):
        return HTMLResponse('<div class="p-6 text-slate-400">无此数据</div>', status_code=404)
    section = sections[idx]
    return templates.TemplateResponse(
        request,
        "partials/section.html",
        {"section": section, "view": view},
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

