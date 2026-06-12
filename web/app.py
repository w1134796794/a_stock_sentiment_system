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
    """数值统一保留两位小数；整数/布尔/非数值（字符串/列表/字典）原样返回。

    用于表格单元格，避免 14.658499999999998 这类浮点尾数直接显示。
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return f"{value:.2f}"
    return value


templates.env.filters["num2"] = _num2


# ----------------------------------------------------------------------
# 表头英文列名 → 中文展示名（只翻译显示，底层数据 key 不变，无需重生成快照）。
# 未收录的列（含已是中文的列）原样返回。
# ----------------------------------------------------------------------
COLUMN_LABELS: Dict[str, str] = {
    # 策略信号 / 交易计划 / 风控闸门
    "pattern_type": "模式类型",
    "stock_code": "股票代码",
    "stock_name": "股票名称",
    "ts_code": "代码",
    "name": "名称",
    "type": "类型",
    "confidence": "置信度",
    "description": "信号描述",
    "key_metrics": "关键指标",
    "validation_rules": "校验规则",
    "entry_price": "入场价",
    "stop_loss": "止损价",
    "take_profit": "止盈价",
    "position_size": "建议仓位",
    "l2_industry": "二级行业",
    "is_dual_resonance": "双线共振",
    "action": "风控动作",
    "reason_text": "决策原因",
    "original_position_pct": "原始仓位%",
    "final_position_pct": "风控后仓位%",
    # 板块题材（热点概念 / 热点行业 / 持续性）
    "rank": "排名",
    "pct_change": "涨跌幅",
    "limit_up_count": "涨停家数",
    "limit_cons_count": "连板数",
    "member_count": "成分股数",
    "amount": "成交额",
    "avg_amount": "平均成交额",
    "vol": "成交量",
    "composite_score": "综合评分",
    "momentum_score": "动量评分",
    "price_score": "价格评分",
    "amount_score": "成交额评分",
    "limit_score": "涨停评分",
    "amount_rank": "成交额排名",
    "limit_cpt_rank": "涨停概念排名",
    "limit_cpt_score": "涨停概念评分",
    "is_hot": "是否热点",
    "is_hot_concept": "是否热点概念",
    "is_hot_industry": "是否热点行业",
    "_date_list": "上榜日期",
    # 因子原始数据（与导出报表命名一致）
    "tech_D1_n_day_high_low": "D1N日新高低",
    "tech_D2_vol_price_coord": "D2量价配合",
    "tech_D3_seal_strength": "D3封板强度",
    "tech_D4_turnover_health": "D4换手健康",
    "tech_D5_ma_bull_align": "D5均线多头",
    "mf_E1_main_net_ratio": "E1主力净占比",
    "mf_E2_retail_net_ratio": "E2散户净占比",
    "mf_E3_large_buy_ratio": "E3大单买入占比",
    "mf_E4_moneyflow_trend": "E4资金趋势",
    # 置信度扣分制因子（confidence_rules.yaml 的 breakdown.factor）
    "seal_ratio": "封单强度",
    "gap_ratio": "次日高开",
    "gap_pct": "竞价高开",
    "first_board_score": "首板质量分",
    "is_fast": "快速封板",
    "auction_vol_ratio": "竞价量比",
    "flexible_score": "弹性评分",
    "weakening_type": "走弱类型",
    "breakout_type": "突破类型",
    "volume_ratio_excess": "量能超额",
    "break_count": "开板次数",
    "early_seal": "早盘秒封",
    "sector_score": "板块效应",
    "max_boards": "最高连板",
    "days_since_peak": "距高点天数",
    "layer2_clean": "L2干净",
}


def _col_label(col: Any) -> str:
    """列名 → 中文展示名；未收录或已是中文的原样返回。"""
    s = "" if col is None else str(col)
    return COLUMN_LABELS.get(s, s)


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


@app.get("/intraday", response_class=HTMLResponse)
def intraday_page(request: Request) -> Any:
    """盘中转强：实时观测走弱池个股是否盘中涨幅转强（手动跑一轮）。"""
    from core.execution.intraday_recovery import IntradayRecoveryMonitor

    view = IntradayRecoveryMonitor().view()
    return templates.TemplateResponse(request, "intraday.html", {"rt": view})


@app.post("/api/intraday/run")
def api_intraday_run(payload: dict = Body(default={})) -> Any:
    """手动跑一轮盘中转强观测，合并落盘后返回最新结果。"""
    from core.execution.intraday_recovery import IntradayRecoveryMonitor

    date = (payload or {}).get("date") or None
    threshold = (payload or {}).get("threshold")
    monitor = IntradayRecoveryMonitor()
    try:
        monitor.run_once(date_str=date, threshold=threshold)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"{e}"}, status_code=500)
    return JSONResponse({"ok": True, "view": monitor.view(date)})


@app.get("/api/intraday/status")
def api_intraday_status(date: Optional[str] = None) -> Any:
    from core.execution.intraday_recovery import IntradayRecoveryMonitor

    return JSONResponse(IntradayRecoveryMonitor().view(date))


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
    rows = []
    for _, r in df.iterrows():
        ts = _date_to_ts(r.get("date") or r.get("trade_date") or trade_date, r.get("time") or "09:30:00")
        value = r.get("close")
        if not ts or value is None:
            continue
        rows.append({"time": ts, "value": float(value)})
    return rows


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


# ----------------------------------------------------------------------
# Phase 4：因子面板（因子开关 / profile / 各策略置信度模式）
# 写入复用 /api/config（apply_updates），本页仅提供友好读取 + 编排。
# ----------------------------------------------------------------------
def _latest_active_profile() -> str:
    """从最新快照 meta 读取实际生效的因子 profile（仅展示）。"""
    try:
        latest = reader.latest()
        snap = reader.load(latest) if latest else None
        meta = (snap or {}).get("meta", {}) or {}
        return str(meta.get("factor_profile") or "")
    except Exception:  # noqa: BLE001
        return ""


@app.get("/factors", response_class=HTMLResponse)
def factors_page(request: Request) -> Any:
    """因子面板：因子启用开关 + 情绪周期 profile + 各策略置信度模式。"""
    from web.factor_panel import build_factor_state

    state = build_factor_state(active_profile=_latest_active_profile())
    return templates.TemplateResponse(request, "factors.html", {"state": state})


@app.get("/api/factors/state")
def api_factors_state() -> Any:
    from web.factor_panel import build_factor_state

    return JSONResponse(build_factor_state(active_profile=_latest_active_profile()))


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


# ----------------------------------------------------------------------
# 手机竖屏 UI（独立 /m 入口）：复用桌面数据源与 API，不影响现有横屏控制台。
# ----------------------------------------------------------------------
@app.get("/m", response_class=HTMLResponse)
def mobile_index(request: Request) -> Any:
    """手机概览页：核心状态 + 快捷入口。"""
    from desktop.status import overview

    return templates.TemplateResponse(request, "mobile/overview.html", {"ov": overview()})


@app.get("/m/data", response_class=HTMLResponse)
def mobile_data_home() -> Any:
    latest = reader.latest()
    return RedirectResponse(url=f"/m/data/strategy/{latest}" if latest else "/m")


@app.get("/m/data/{cat}", response_class=HTMLResponse)
def mobile_data_index(cat: str) -> Any:
    if cat not in _CATEGORY_BY_KEY:
        return RedirectResponse(url="/m")
    latest = reader.latest()
    return RedirectResponse(url=f"/m/data/{cat}/{latest}" if latest else "/m")


@app.get("/m/data/{cat}/{date}", response_class=HTMLResponse)
def mobile_data_browse(request: Request, cat: str, date: str) -> Any:
    """手机数据浏览：分类 Tab + 单列 section 卡片。"""
    category = _CATEGORY_BY_KEY.get(cat)
    if category is None:
        return HTMLResponse('<div class="p-6 text-slate-400">无此分类</div>', status_code=404)
    snapshot = reader.load(date)
    return templates.TemplateResponse(
        request,
        "mobile/data_browse.html",
        {
            "category": category,
            "cat": cat,
            "categories": DATA_CATEGORIES,
            "date": date,
            "dates": reader.list_dates(),
            "sections": (snapshot or {}).get("sections", []),
            "snapshot": snapshot,
        },
    )


@app.get("/m/dragon", response_class=HTMLResponse)
def mobile_dragon_page(request: Request) -> Any:
    """手机龙头池：单列卡片浏览。"""
    from desktop.status import dragon_pools

    return templates.TemplateResponse(request, "mobile/dragon.html", {"dp": dragon_pools()})


@app.get("/m/intraday", response_class=HTMLResponse)
def mobile_intraday_page(request: Request) -> Any:
    """手机盘中转强：单列展示走弱池实时转强信号。"""
    from core.execution.intraday_recovery import IntradayRecoveryMonitor

    return templates.TemplateResponse(
        request, "mobile/intraday.html", {"rt": IntradayRecoveryMonitor().view()}
    )


@app.get("/m/recap", response_class=HTMLResponse)
def mobile_recap_index() -> Any:
    latest = reader.latest()
    return RedirectResponse(url=f"/m/recap/{latest}" if latest else "/m")


@app.get("/m/recap/{date}", response_class=HTMLResponse)
def mobile_recap_page(request: Request, date: str, tab: str = "show") -> Any:
    """手机复盘统一页：演出播放器 + 图文素材（合规文案/卡片/保存）。"""
    from recap.thermometer import build_thermometer

    snapshot = reader.load(date)
    pack = build_thermometer(snapshot) if snapshot else None
    return templates.TemplateResponse(
        request, "mobile/recap.html",
        {"date": date, "dates": reader.list_dates(), "pack": pack,
         "tab": "material" if tab == "material" else "show"},
    )


@app.get("/m/content", response_class=HTMLResponse)
def mobile_content_index() -> Any:
    return RedirectResponse(url="/m/recap")


@app.get("/m/content/{date}", response_class=HTMLResponse)
def mobile_content_page(date: str) -> Any:
    return RedirectResponse(url=f"/m/recap/{date}?tab=material")


@app.get("/m/run", response_class=HTMLResponse)
def mobile_run_page(request: Request) -> Any:
    """手机运行分析：复用运行控制器与状态 API。"""
    from desktop.runner import CONTROLLER

    return templates.TemplateResponse(request, "mobile/run.html", {"run": CONTROLLER.status()})


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
    """启动一次回测（重新生成净值/交易/回撤）。

    body: {start_date?, end_date?, initial_capital?, risk_control?}
    risk_control 缺省时回退到全局 RiskConfig.enabled。
    """
    from desktop.runner import BACKTEST_CONTROLLER

    p = payload or {}
    ok, msg = BACKTEST_CONTROLLER.start(
        p.get("start_date"), p.get("end_date"), p.get("initial_capital"),
        risk_control=p.get("risk_control"))
    return JSONResponse({"started": ok, "message": msg})


@app.get("/api/backtest/run/status")
def api_backtest_run_status(since: int = 0) -> Any:
    from desktop.runner import BACKTEST_CONTROLLER

    return JSONResponse(BACKTEST_CONTROLLER.status(since))


@app.get("/report/{date}/section/{idx}", response_class=HTMLResponse)
def section_fragment(request: Request, date: str, idx: int, view: str = "table") -> Any:
    """HTMX 片段：返回某个 section 的 HTML。

    view=detail → 折叠主从卡片（紧凑列表 + 点击展开全字段，避免横向滚动），
    用于字段很多的策略信号 / 板块题材；其余分类默认 view=table 宽表。
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
# 复盘短视频分镜脚本（storyboard）：供演出视图 / HyperFrames 复用
# ----------------------------------------------------------------------
def _recap_payload(date: Optional[str], refresh: bool) -> Any:
    from recap.storyboard import build_and_save, load_recap

    target = date or reader.latest()
    if not target:
        return JSONResponse({"error": "no snapshot"}, status_code=404)
    if not refresh:
        cached = load_recap(target)
        if cached:
            return JSONResponse(cached)
    snapshot = reader.load(target)
    if snapshot is None:
        return JSONResponse({"error": "not found", "date": target}, status_code=404)
    return JSONResponse(build_and_save(target, snapshot))


@app.get("/api/recap")
def api_recap_latest(refresh: bool = False) -> Any:
    """最新交易日的复盘分镜脚本。"""
    return _recap_payload(None, refresh)


@app.get("/api/recap/{date}")
def api_recap(date: str, refresh: bool = False) -> Any:
    """某交易日的复盘分镜脚本（缓存优先；refresh=1 重建并覆盖落盘）。"""
    return _recap_payload(date, refresh)


# ----------------------------------------------------------------------
# 复盘（统一功能）：演出视图 + 图文素材整合到 /recap，单一入口、同源驱动。
#   /recap/{date}            统一页（演出 / 图文素材 两个选项卡）
#   /recap/{date}/cards      统一图文导出（mode=full 全量分镜 / mode=compliant 合规4卡）
# 旧 /show、/content 路由保留为重定向，确保历史链接不失效。
# ----------------------------------------------------------------------
@app.get("/recap", response_class=HTMLResponse)
def recap_index() -> Any:
    latest = reader.latest()
    return RedirectResponse(url=f"/recap/{latest}" if latest else "/")


@app.get("/recap/{date}", response_class=HTMLResponse)
def recap_page(request: Request, date: str, tab: str = "show") -> Any:
    """复盘统一页：演出播放器 + 合规图文素材，共享日期 / 同一快照。"""
    from recap.thermometer import build_thermometer

    snapshot = reader.load(date)
    pack = build_thermometer(snapshot) if snapshot else None
    return templates.TemplateResponse(
        request, "recap.html",
        {"date": date, "dates": reader.list_dates(), "pack": pack,
         "tab": "material" if tab == "material" else "show"},
    )


@app.get("/recap/{date}/cards", response_class=HTMLResponse)
def recap_cards(request: Request, date: str, mode: str = "full") -> Any:
    """统一图文导出。

    - ``mode=full``（默认）：全量复盘分镜逐幕成图（含个股/策略，自用复盘）。
    - ``mode=compliant``：情绪温度计 4 张合规卡（无个股/收益/操作建议，可对外发布）。
    """
    snapshot = reader.load(date)
    if snapshot is None:
        return HTMLResponse('<div style="color:#94a3b8;padding:24px">无该日快照</div>', status_code=404)
    if mode == "compliant":
        from recap.thermometer import build_thermometer, render_cards_html
        return HTMLResponse(render_cards_html(build_thermometer(snapshot)))
    return templates.TemplateResponse(
        request, "cards.html",
        {"date": date, "title": f"A股复盘 {date} · 图文"},
    )


# ----------------------------------------------------------------------
# 演出播放器（被 /recap 页内嵌 iframe 复用；也可独立全屏放映 / 逐帧抓取）
# ----------------------------------------------------------------------
@app.get("/show", response_class=HTMLResponse)
def show_index() -> Any:
    return RedirectResponse(url="/recap")


@app.get("/show/{date}", response_class=HTMLResponse)
def show_page(request: Request, date: str, autoplay: bool = True) -> Any:
    """整片演出视图：读 /api/recap/{date}，逐幕自动播放（空格/方向键可控）。"""
    return templates.TemplateResponse(
        request, "show.html",
        {"date": date, "single": None, "autoplay": autoplay, "title": f"A股复盘 {date}"},
    )


@app.get("/show/{date}/scene/{key}", response_class=HTMLResponse)
def show_scene(request: Request, date: str, key: str) -> Any:
    """单场景静帧视图：锁定某一幕、无控件/无进度条，便于截帧或嵌入。"""
    return templates.TemplateResponse(
        request, "show.html",
        {"date": date, "single": key, "autoplay": False, "title": f"A股复盘 {date} · {key}"},
    )


@app.get("/show/{date}/cards", response_class=HTMLResponse)
def show_cards(date: str) -> Any:
    """兼容旧链接：跳到统一图文导出（全量分镜）。"""
    return RedirectResponse(url=f"/recap/{date}/cards?mode=full")


# ----------------------------------------------------------------------
# 图文素材 · 情绪温度计：已并入 /recap 的「图文素材」选项卡，旧链接重定向。
# ----------------------------------------------------------------------
@app.get("/content", response_class=HTMLResponse)
def content_index() -> Any:
    return RedirectResponse(url="/recap")


@app.get("/content/{date}", response_class=HTMLResponse)
def content_page(date: str) -> Any:
    return RedirectResponse(url=f"/recap/{date}?tab=material")


@app.get("/content/{date}/cards", response_class=HTMLResponse)
def content_cards(date: str) -> Any:
    return RedirectResponse(url=f"/recap/{date}/cards?mode=compliant")


@app.post("/api/content/{date}/save")
def api_content_save(date: str, payload: dict = Body(default={})) -> Any:
    """保存图文/分镜素材到 webdata/content/{date}/；with_video=true 时尝试出片。"""
    from recap.thermometer import save

    try:
        res = save(date, with_video=bool((payload or {}).get("with_video")))
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    out = {
        "ok": True,
        "date": res["date"],
        "dir": str(res["dir"]),
        "files": {"文案": str(res["文案"]), "卡片": str(res["卡片"]), "分镜": str(res["story"])},
    }
    if res.get("video"):
        v = res["video"]
        out["video"] = {"html": str(v.get("html")), "rendered": v.get("rendered"),
                        "mp4": str(v.get("mp4")) if v.get("mp4") else None,
                        "render_cmd": " ".join(v.get("render_cmd") or [])}
    return JSONResponse(out)


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