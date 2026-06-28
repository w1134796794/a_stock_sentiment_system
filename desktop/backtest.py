"""回测结果数据层：读取 output/backtest_results 下的 CSV，组装「模拟交易结果」与
「回撤分析」两个页面所需的数据，并生成内联 SVG 图表（离线可用，无需图表库）。

约定
====
run_backtest.py 每次回测保存三份同时间戳的 CSV：
  backtest_summary_{YYYYMMDD}_{HHMMSS}.csv  汇总指标（单行）
  backtest_nav_{YYYYMMDD}_{HHMMSS}.csv      每日净值曲线
  backtest_trades_{YYYYMMDD}_{HHMMSS}.csv   逐笔交易

本模块只用标准库 csv，避免在浏览页引入 pandas；回撤口径与
backtest/performance_analyzer.py 的 _calculate_risk_metrics 保持一致。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config.settings import OUTPUT_DIR
from core.screening.explanations import FACTOR_LABELS

RESULTS_DIR = Path(OUTPUT_DIR) / "backtest_results"
_RUN_RE = re.compile(r"^backtest_(summary|nav|trades)_(\d{8}_\d{6})\.csv$")

# 这些列必须按字符串保留，不能数值化：否则 000062 之类带前导零的股票代码会变成 62。
_STR_COLS = {
    "stock_code", "stock_name", "pattern_type", "action", "resonance_sectors", "entry_signal",
}


# ----------------------------------------------------------------------
# 基础读取
# ----------------------------------------------------------------------
def _num(v: Any) -> Any:
    """字符串 → int/float/bool/原样。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return s


def _read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:  # noqa: BLE001
        return []


def _path(kind: str, run: str) -> Path:
    return RESULTS_DIR / f"backtest_{kind}_{run}.csv"


def list_runs() -> List[str]:
    """返回全部回测时间戳（run id），按时间倒序。"""
    runs: set[str] = set()
    if RESULTS_DIR.exists():
        for p in RESULTS_DIR.glob("backtest_*.csv"):
            m = _RUN_RE.match(p.name)
            if m:
                runs.add(m.group(2))
    return sorted(runs, reverse=True)


def latest_run() -> Optional[str]:
    runs = list_runs()
    return runs[0] if runs else None


def _resolve(run: Optional[str]) -> Optional[str]:
    if run:
        return run
    return latest_run()


def load_summary(run: str) -> Dict[str, Any]:
    rows = _read_csv(_path("summary", run))
    if not rows:
        return {}
    return {k: _num(v) for k, v in rows[0].items()}


def load_nav(run: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in _read_csv(_path("nav", run)):
        out.append({k: _num(v) for k, v in r.items()})
    return out


def load_trades(run: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in _read_csv(_path("trades", run)):
        out.append({
            k: (str(v).strip() if k in _STR_COLS else _num(v))
            for k, v in r.items()
        })
    return out


def load_positions(run: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in _read_csv(_path("positions", run)):
        out.append({
            k: (str(v).strip() if k in _STR_COLS else _num(v))
            for k, v in r.items()
        })
    return out


def load_table(kind: str, run: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in _read_csv(_path(kind, run)):
        out.append({k: _num(v) for k, v in r.items()})
    return out


def _fmt_money(value: Any) -> str:
    try:
        return f"{float(value):+,.0f}"
    except (TypeError, ValueError):
        return ""


def _fmt_price(value: Any, *, blank_zero: bool = False) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return ""
    if blank_zero and price == 0:
        return ""
    return f"{price:.2f}"


def _fmt_pct(value: Any, signed: bool = False) -> str:
    try:
        fmt = "{:+.1f}%" if signed else "{:.1f}%"
        return fmt.format(float(value) * 100)
    except (TypeError, ValueError):
        return ""


def runs_meta() -> List[Dict[str, Any]]:
    """供下拉选择：每个 run 的标签（时间 + 总收益）。"""
    meta: List[Dict[str, Any]] = []
    for run in list_runs():
        s = load_summary(run)
        ret = s.get("total_return")
        label = run.replace("_", " ")
        mode = str(s.get("run_mode") or "")
        if mode == "daily":
            label += f" · 接力 {s.get('trade_date') or ''}".rstrip()
        elif mode == "range":
            start = s.get("start_date") or ""
            end = s.get("end_date") or ""
            label += f" · 区间 {start}~{end}".rstrip("~")
        if isinstance(ret, (int, float)):
            label += f" · 收益 {ret * 100:+.1f}%"
        meta.append({"run": run, "label": label})
    return meta


# ----------------------------------------------------------------------
# 回撤计算（与 performance_analyzer 口径一致）
# ----------------------------------------------------------------------
def compute_drawdown(nav: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从净值曲线计算回撤序列、最大回撤、持续时间、回撤区间。"""
    series: List[Dict[str, Any]] = []
    peak = None
    max_dd = 0.0
    max_dd_idx = -1
    for i, row in enumerate(nav):
        val = row.get("total_value")
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        peak = val if peak is None else max(peak, val)
        dd = (val - peak) / peak if peak else 0.0
        if dd < max_dd:
            max_dd = dd
            max_dd_idx = i
        series.append({"date": str(row.get("date", "")), "value": val,
                       "peak": peak, "dd": dd})

    # 最长回撤持续天数 + 回撤区间（连续 dd<0 的段）
    episodes: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    max_dur = 0
    for i, p in enumerate(series):
        if p["dd"] < -1e-9:
            if cur is None:
                cur = {"start": p["date"], "start_idx": i, "trough_dd": p["dd"],
                       "trough_date": p["date"]}
            elif p["dd"] < cur["trough_dd"]:
                cur["trough_dd"] = p["dd"]
                cur["trough_date"] = p["date"]
        else:
            if cur is not None:
                cur["end"] = p["date"]
                cur["days"] = i - cur["start_idx"]
                max_dur = max(max_dur, cur["days"])
                episodes.append(cur)
                cur = None
    if cur is not None:  # 收盘仍在回撤中
        cur["end"] = "(未恢复)"
        cur["days"] = len(series) - cur["start_idx"]
        max_dur = max(max_dur, cur["days"])
        episodes.append(cur)

    episodes.sort(key=lambda e: e["trough_dd"])  # 最深在前
    trough_date = series[max_dd_idx]["date"] if max_dd_idx >= 0 else ""
    return {
        "series": series,
        "max_drawdown": max_dd,
        "max_dd_duration": max_dur,
        "trough_date": trough_date,
        "episodes": episodes,
    }


# ----------------------------------------------------------------------
# 内联 SVG 图表（折线 + 面积填充）
# ----------------------------------------------------------------------
_W, _H, _PAD = 1000.0, 260.0, 12.0


def _chart(values: List[float], fill: str = "bottom") -> Optional[Dict[str, Any]]:
    """把数值序列映射成 SVG 坐标。fill='bottom'（净值，向下填充）/'top'（回撤，向上填充到 0 线）。"""
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return None
    vmin, vmax = min(vals), max(vals)
    span = (vmax - vmin) or 1.0
    inner_w = _W - 2 * _PAD
    inner_h = _H - 2 * _PAD

    def x_at(i: int) -> float:
        return _PAD + (inner_w * (i / (n - 1)) if n > 1 else inner_w / 2)

    def y_at(v: float) -> float:
        return _PAD + (1 - (v - vmin) / span) * inner_h

    pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    baseline_y = (_H - _PAD) if fill == "bottom" else _PAD
    fx, lx = pts[0][0], pts[-1][0]
    area = f"{fx:.1f},{baseline_y:.1f} {line} {lx:.1f},{baseline_y:.1f}"

    # value=0 在当前值域内时给一条 0 轴参考线（净值图一般用不到）
    zero_y = y_at(0.0) if vmin <= 0 <= vmax else None
    return {
        "w": _W, "h": _H,
        "line": line, "area": area,
        "vmin": vmin, "vmax": vmax,
        "first_value": vals[0], "last_value": vals[-1],
        "zero_y": zero_y,
    }


# ----------------------------------------------------------------------
# 页面数据组装
# ----------------------------------------------------------------------
def _has_results() -> bool:
    return bool(list_runs())


def backtest_overview(run: Optional[str]) -> Dict[str, Any]:
    """「模拟交易结果」页：汇总指标 + 净值曲线 + 逐笔交易 + 模式表现。"""
    run = _resolve(run)
    base: Dict[str, Any] = {
        "exists": run is not None,
        "run": run,
        "runs": runs_meta(),
        "results_dir": str(RESULTS_DIR),
    }
    if run is None:
        return base

    summary = load_summary(run)
    nav = load_nav(run)
    trades = load_trades(run)
    positions = load_positions(run)

    initial = summary.get("initial_capital") or (nav[0]["total_value"] if nav else 0)
    final = summary.get("final_capital") or (nav[-1]["total_value"] if nav else 0)

    # 卖出类交易才计盈亏
    closed = [t for t in trades if str(t.get("action", "")).upper().startswith("SELL")]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) < 0]

    # 按模式聚合
    pat: Dict[str, Dict[str, Any]] = {}
    for t in closed:
        name = str(t.get("pattern_type") or "未知")
        d = pat.setdefault(name, {"模式": name, "笔数": 0, "盈利": 0, "总盈亏": 0.0})
        d["笔数"] += 1
        if (t.get("pnl") or 0) > 0:
            d["盈利"] += 1
        d["总盈亏"] += float(t.get("pnl") or 0)
    pattern_rows = []
    for d in pat.values():
        wr = d["盈利"] / d["笔数"] if d["笔数"] else 0
        pattern_rows.append({
            "模式": d["模式"], "笔数": d["笔数"],
            "胜率": f"{wr * 100:.1f}%",
            "总盈亏": f"{d['总盈亏']:+,.0f}",
            "_pnl": d["总盈亏"],
        })
    pattern_rows.sort(key=lambda r: r["_pnl"], reverse=True)
    for r in pattern_rows:
        r.pop("_pnl", None)

    factor_feedback_rows = []
    for r in load_table("factor_feedback", run):
        factor_id = str(r.get("factor_id") or "")
        factor_feedback_rows.append({
            "因子": FACTOR_LABELS.get(factor_id) or r.get("factor_name") or factor_id,
            "样本": r.get("sample_count", 0),
            "总体胜率": _fmt_pct(r.get("win_rate")),
            "强项样本": r.get("strong_count", 0),
            "强项盈亏": _fmt_money(r.get("strong_total_pnl")),
            "强项胜率": _fmt_pct(r.get("strong_win_rate")),
            "弱项样本": r.get("weak_count", 0),
            "弱项盈亏": _fmt_money(r.get("weak_total_pnl")),
            "弱项止损率": _fmt_pct(r.get("weak_stop_loss_rate")),
            "强弱差": _fmt_money(r.get("strong_minus_weak_pnl")),
            "均分": f"{float(r.get('avg_score') or 0):.1f}",
            "结论": r.get("feedback") or "",
            "_weak_pnl": float(r.get("weak_total_pnl") or 0),
            "_strong_pnl": float(r.get("strong_total_pnl") or 0),
        })
    factor_feedback_rows.sort(key=lambda r: (r["_weak_pnl"], -r["_strong_pnl"]))
    for r in factor_feedback_rows:
        r.pop("_weak_pnl", None)
        r.pop("_strong_pnl", None)

    rank_feedback_rows = []
    for r in load_table("rank_feedback", run):
        total_pnl = float(r.get("total_pnl") or 0)
        rank_feedback_rows.append({
            "排名": r.get("plan_rank", ""),
            "样本": r.get("sample_count", 0),
            "胜率": _fmt_pct(r.get("win_rate")),
            "总盈亏": _fmt_money(r.get("total_pnl")),
            "平均收益": _fmt_pct(r.get("avg_pnl_pct"), signed=True),
            "止损率": _fmt_pct(r.get("stop_loss_rate")),
            "止盈率": _fmt_pct(r.get("take_profit_rate")),
            "均分": f"{float(r.get('avg_score') or 0):.1f}",
            "_pnl": total_pnl,
        })
    rank_feedback_rows.sort(key=lambda row: int(row.get("排名") or 999999))
    next_rank = 1
    for row in rank_feedback_rows:
        rank = int(row.get("排名") or 0)
        if rank == next_rank and row["_pnl"] > 0:
            next_rank += 1
        elif rank <= next_rank:
            break
    rank_suggestion = 3 if not rank_feedback_rows else max(1, min(next_rank - 1, 3))
    for row in rank_feedback_rows:
        row.pop("_pnl", None)

    walk_forward_rows = []
    for r in load_table("walk_forward", run):
        walk_forward_rows.append({
            "折": int(float(r.get("fold") or 0)),
            "训练区间": f"{r.get('train_start') or ''} - {r.get('train_end') or ''}",
            "验证区间": f"{r.get('validation_start') or ''} - {r.get('validation_end') or ''}",
            "入场组合": r.get("selected_profile") or "",
            "训练样本": int(float(r.get("train_samples") or 0)),
            "训练胜率": _fmt_pct(r.get("train_win_rate")),
            "验证样本": int(float(r.get("validation_samples") or 0)),
            "验证胜率": _fmt_pct(r.get("validation_win_rate")),
            "验证盈亏": _fmt_money(r.get("validation_total_pnl")),
            "验证止损率": _fmt_pct(r.get("validation_stop_rate")),
        })
    walk_summary_raw = load_table("walk_forward_summary", run)
    walk_forward_summary = None
    if walk_summary_raw:
        r = walk_summary_raw[0]
        walk_forward_summary = {
            "折数": int(float(r.get("folds") or 0)),
            "样本外样本": int(float(r.get("oos_samples") or 0)),
            "样本外胜率": _fmt_pct(r.get("oos_win_rate")),
            "样本外平均收益": _fmt_pct(r.get("oos_avg_return"), signed=True),
            "样本外总盈亏": _fmt_money(r.get("oos_total_pnl")),
            "样本外止损率": _fmt_pct(r.get("oos_stop_rate")),
        }

    # 逐笔展示原始 BUY/SELL；FIFO 仅用于识别哪些 BUY 仍处于持仓中。
    open_lots: Dict[str, List[Dict[str, Any]]] = {}
    for index, trade in enumerate(trades):
        action = str(trade.get("action") or "").upper()
        code = str(trade.get("stock_code") or "")
        shares = int(float(trade.get("shares") or 0))
        if action == "BUY":
            open_lots.setdefault(code, []).append({"index": index, "remaining": shares})
            continue
        if not action.startswith("SELL"):
            continue

        lots = open_lots.get(code) or []
        remaining_to_match = shares
        while lots and remaining_to_match > 0:
            lot = lots[0]
            matched = min(int(lot["remaining"]), remaining_to_match)
            lot["remaining"] -= matched
            remaining_to_match -= matched
            if lot["remaining"] <= 0:
                lots.pop(0)

    open_buy_indices = {
        int(lot["index"])
        for lots in open_lots.values()
        for lot in lots
        if int(lot.get("remaining") or 0) > 0
    }
    position_map = {
        (str(row.get("stock_code") or ""), str(row.get("entry_date") or "")): row
        for row in positions
    }

    trade_rows = []
    for index, record in enumerate(trades):
        action = str(record.get("action") or "").upper()
        is_buy = action == "BUY"
        is_sell = action.startswith("SELL")
        is_open = is_buy and index in open_buy_indices
        entry_date = str(record.get("entry_date") or record.get("date") or "")
        code = str(record.get("stock_code") or "")
        position = position_map.get((code, entry_date)) or (position_map.get((code, "")) if is_open else None)
        pnl = position.get("unrealized_pnl") if is_open and position else record.get("pnl") if is_sell else None
        pnl_pct = (
            position.get("unrealized_pnl_pct") if is_open and position
            else record.get("pnl_pct") if is_sell else None
        )
        action_label = "买入" if is_buy else "部分卖出" if action == "SELL_PARTIAL" else "卖出"
        status = "持仓中" if is_open else "买入成交" if is_buy else str(record.get("exit_reason") or "卖出成交")
        trade_rows.append({
            "日期": record.get("date") or "",
            "动作": action_label,
            "名称": record.get("stock_name", ""),
            "代码": code,
            "模式": record.get("pattern_type", ""),
            "买入价": _fmt_price(record.get("entry_price")),
            "卖出价": _fmt_price(record.get("exit_price"), blank_zero=True) if is_sell else "",
            "现价": _fmt_price(position.get("current_price"), blank_zero=True) if is_open and position else "",
            "股数": int(float(record.get("shares") or 0)),
            "盈亏": (f"{float(pnl):+,.0f}" if isinstance(pnl, (int, float)) else ""),
            "盈亏%": (f"{float(pnl_pct) * 100:+.2f}%" if isinstance(pnl_pct, (int, float)) else ""),
            "持仓天数": position.get("holding_days") if is_open and position else record.get("holding_days", ""),
            "止损": "是" if record.get("stop_loss_triggered") else "",
            "止盈": "是" if record.get("take_profit_triggered") else "",
            "排名": record.get("plan_rank", ""),
            "评分": (f"{float(record.get('plan_score')):.2f}"
                   if isinstance(record.get("plan_score"), (int, float)) else ""),
            "买入信号": position.get("entry_signal") if is_open and position else record.get("entry_signal", ""),
            "状态/退出": status,
            "_sort_date": str(record.get("date") or ""),
            "_sort_index": index,
        })
    trade_rows.sort(key=lambda row: (row.get("_sort_date") or "", row.get("_sort_index") or 0), reverse=True)
    for row in trade_rows:
        row.pop("_sort_date", None)
        row.pop("_sort_index", None)

    has_position_snapshot = _path("positions", run).exists()
    open_count = len(positions) if has_position_snapshot else len(open_buy_indices)

    base.update({
        "summary": summary,
        "initial": initial,
        "final": final,
        "profit": (final or 0) - (initial or 0),
        "total_return": summary.get("total_return"),
        "max_drawdown": summary.get("max_drawdown"),
        "sharpe": summary.get("sharpe_ratio"),
        "win_rate": summary.get("win_rate"),
        "pl_ratio": summary.get("profit_loss_ratio"),
        "total_trades": summary.get("total_trades") if summary.get("total_trades") is not None else len(closed),
        "closed_count": len(closed),
        "buy_count": sum(1 for t in trades if str(t.get("action", "")).upper() == "BUY"),
        "execution_count": len(trades),
        "open_count": open_count,
        "win_count": len(wins),
        "loss_count": len(losses),
        "nav_start": nav[0]["date"] if nav else "",
        "nav_end": nav[-1]["date"] if nav else "",
        "nav_days": len(nav),
        "equity_chart": _chart([r["total_value"] for r in nav], fill="bottom") if nav else None,
        "pattern_rows": pattern_rows,
        "factor_feedback_rows": factor_feedback_rows,
        "rank_feedback_rows": rank_feedback_rows,
        "rank_suggestion": rank_suggestion,
        "walk_forward_rows": walk_forward_rows,
        "walk_forward_summary": walk_forward_summary,
        "transaction_view": True,
        "trade_rows": trade_rows,
        "trade_columns": ["日期", "动作", "名称", "代码", "模式", "买入价", "卖出价", "现价",
                          "股数", "盈亏", "盈亏%", "持仓天数", "止损", "止盈", "排名", "评分",
                          "买入信号", "状态/退出"],
    })
    return base


def drawdown_overview(run: Optional[str]) -> Dict[str, Any]:
    """「回撤分析」页：回撤水下曲线 + 最大回撤 + 回撤区间 + 最差交易。"""
    run = _resolve(run)
    base: Dict[str, Any] = {
        "exists": run is not None,
        "run": run,
        "runs": runs_meta(),
        "results_dir": str(RESULTS_DIR),
    }
    if run is None:
        return base

    summary = load_summary(run)
    nav = load_nav(run)
    dd = compute_drawdown(nav)
    series = dd["series"]

    # 净值新高次数（peak 抬升的天数）
    new_highs = 0
    last_peak = None
    for p in series:
        if last_peak is None or p["peak"] > last_peak + 1e-9:
            new_highs += 1
            last_peak = p["peak"]

    # 回撤区间表（最深前 8 段）
    episode_rows = []
    for e in dd["episodes"][:8]:
        episode_rows.append({
            "起始": e.get("start", ""),
            "最低点": e.get("trough_date", ""),
            "结束/恢复": e.get("end", ""),
            "最大回撤": f"{e.get('trough_dd', 0) * 100:.2f}%",
            "持续(交易日)": e.get("days", 0),
        })

    # 最差交易（按盈亏升序前 12）
    closed = [t for t in load_trades(run)
              if str(t.get("action", "")).upper().startswith("SELL")
              and isinstance(t.get("pnl"), (int, float))]
    closed.sort(key=lambda t: t.get("pnl") or 0)
    worst_rows = []
    for t in closed[:12]:
        worst_rows.append({
            "日期": t.get("date", ""),
            "名称": t.get("stock_name", ""),
            "代码": t.get("stock_code", ""),
            "模式": t.get("pattern_type", ""),
            "盈亏": f"{float(t.get('pnl') or 0):+,.0f}",
            "盈亏%": (f"{float(t.get('pnl_pct')) * 100:+.2f}%"
                      if isinstance(t.get("pnl_pct"), (int, float)) else ""),
            "止损触发": "是" if t.get("stop_loss_triggered") else "",
        })

    base.update({
        "summary": summary,
        "max_drawdown": dd["max_drawdown"],
        "summary_max_dd": summary.get("max_drawdown"),
        "max_dd_duration": dd["max_dd_duration"],
        "trough_date": dd["trough_date"],
        "new_highs": new_highs,
        "nav_days": len(series),
        "nav_start": series[0]["date"] if series else "",
        "nav_end": series[-1]["date"] if series else "",
        "dd_chart": _chart([p["dd"] for p in series], fill="top") if series else None,
        "episode_rows": episode_rows,
        "episode_count": len(dd["episodes"]),
        "worst_rows": worst_rows,
    })
    return base
