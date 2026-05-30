"""
风控复核（R-5）：把 ``--mode risk`` 从"演示数据"升级为"真实双腿分析"。

两条腿共用同一套 ``RiskConfig`` / ``PortfolioState`` / ``CircuitBreaker`` /
``RiskGateLayer``，规则不再三套漂移：

A. 账户持仓风控（基于虚拟账户当前真实持仓）
   - 读取虚拟账户 ``PortfolioState``（默认 ``data/cache/portfolio_state.json``），盯市估值
   - 组合层：板块集中度 / 单票占比 / 总仓位 vs 配置上限（``RiskAnalyzer`` + ``RiskConfig``）
   - 账户层：``CircuitBreaker`` 单日亏损 / 回撤冷静期 / 情绪冰点熔断

B. 当日交易计划过闸（基于 Layer4 落盘的 ``交易计划_{date}.csv``）
   - 重建 ``TradePlanResult``，以"当前账户"为起点逐条过 L4.5 风控闸门
   - 输出 PASS / DOWNGRADE / REJECT + 可解释理由

本模块只放**可复用、可单测的纯逻辑 + 格式化**；编排（取数 / 落盘路径）在 ``main.py``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import loguru

from core.pipeline.layer4_trade_plan import (
    PositionLevel,
    TradePlan,
    TradePlanResult,
)

logger = loguru.logger

# Layer4 落盘 CSV 的 仓位 字段（light/medium/heavy）→ 仓位比例 / 仓位等级。
# 与 ``backtest.plan_providers._POSITION_PCT`` 保持一致。
_POSITION_PCT = {"light": 0.10, "medium": 0.15, "heavy": 0.20}
_LEVEL_BY_KEY = {
    "light": PositionLevel.LIGHT,
    "medium": PositionLevel.NORMAL,
    "heavy": PositionLevel.HEAVY,
}


def _clean_str(value) -> str:
    """把 CSV 单元格安全转成字符串：None / 空 / pandas NaN 一律归一为空串。

    关键坑：pandas 读到的空单元格是 ``float('nan')``，``str(nan)`` 会得到字面量
    ``"nan"``——若直接当板块名，会把一堆"无板块"的票误并成同一个假板块而触发集中度上限。
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def _as_bool(value) -> bool:
    """CSV 里的"热点共振"既可能是 True/False，也可能是字符串。"""
    if isinstance(value, bool):
        return value
    s = _clean_str(value).lower()
    return s in {"true", "1", "是", "yes", "y"}


def _parse_date_from_name(csv_path) -> str:
    """从 ``交易计划_YYYYMMDD.csv`` / ``trade_plans_YYYYMMDD.csv`` 取日期。"""
    stem = Path(csv_path).stem
    for sep in ("交易计划_", "trade_plans_"):
        if sep in stem:
            return stem.split(sep, 1)[1]
    return ""


def load_trade_plan_result_from_csv(
    csv_path,
    *,
    position_pct_map: Optional[Dict[str, float]] = None,
    trade_date: str = "",
) -> TradePlanResult:
    """把 ``交易计划_{date}.csv`` 重建成 ``TradePlanResult``，供 L4.5 闸门复核。

    只读取闸门真正需要的字段：代码 / 名称 / 模式 / 仓位 / 共振板块 / 优先级 /
    综合评分 / 热点共振。``max_position_pct`` 固定为 1.0——复核是"以配置上限为准
    的 what-if 体检"，不再叠加历史大盘环境建议（那是出计划当时的临时上限）。
    """
    pct_map = position_pct_map or _POSITION_PCT
    path = Path(csv_path)
    td = trade_date or _parse_date_from_name(path)
    result = TradePlanResult(trade_date=td, max_position_pct=1.0)

    if not path.exists():
        logger.warning(f"[RiskReview] 交易计划文件不存在: {path}")
        return result

    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:  # 退化：无 BOM
        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(f"[RiskReview] 读取 {path} 失败: {e}")
            return result

    if df is None or df.empty:
        return result
    if "动作" in df.columns:
        df = df[df["动作"].astype(str).str.contains("买入", na=False)]

    plans: List[TradePlan] = []
    for _, row in df.iterrows():
        sectors_raw = _clean_str(row.get("共振板块")) or _clean_str(row.get("所属板块"))
        sectors = [s.strip() for s in sectors_raw.split(",") if s.strip()]
        pos_key = (_clean_str(row.get("仓位")) or "light").lower()
        try:
            priority = int(float(row.get("优先级", 99) or 99))
        except (TypeError, ValueError):
            priority = 99
        try:
            score = float(row.get("综合评分", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0

        plan = TradePlan(
            stock_code=_clean_str(row.get("代码")),
            stock_name=_clean_str(row.get("名称")),
            pattern_type=_clean_str(row.get("模式")),
            priority=priority,
            composite_score=score,
            position_level=_LEVEL_BY_KEY.get(pos_key, PositionLevel.LIGHT),
            position_pct=pct_map.get(pos_key, 0.10),
            hot_resonance=_as_bool(row.get("热点共振", False)),
            resonance_sectors=sectors,
        )
        plans.append(plan)

    result.plans = plans
    return result


def portfolio_risk_positions(
    portfolio_state,
    price_map: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict]:
    """把 ``PortfolioState`` 投影成 ``RiskAnalyzer.generate_risk_report`` 期望的结构。

    比 ``PortfolioState.to_risk_positions`` 多带 ``stock_name`` / ``pnl_pct`` /
    ``hot_resonance``，让风险热力图能给出"亏损较大 / 仓位过大"等因子。
    """
    price_map = price_map or {}
    out: Dict[str, Dict] = {}
    for code, pos in portfolio_state.positions.items():
        px = price_map.get(code)
        if not (px and px > 0):
            px = pos.last_price if pos.last_price > 0 else pos.avg_cost
        pnl_pct = (px - pos.avg_cost) / pos.avg_cost if pos.avg_cost > 0 else 0.0
        out[code] = {
            "stock_name": pos.name or code,
            "sector": pos.sector or "未知",
            "market_value": pos.market_value(px),
            "shares": pos.shares,
            "avg_cost": pos.avg_cost,
            "last_price": px,
            "pnl_pct": pnl_pct,
            "hot_resonance": False,
        }
    return out


def build_price_map(
    price_provider,
    codes: Iterable[str],
    as_of: str,
) -> Dict[str, float]:
    """尽力为 ``codes`` 取 ``as_of`` 当日收盘价；失败/缺失则跳过（上层回退持仓价）。

    返回 dict 的 key 用调用方传入的原始代码（如 ``000001.SZ``），与
    ``PortfolioState.positions`` 的 key 对齐。
    """
    out: Dict[str, float] = {}
    if price_provider is None or not as_of:
        return out
    for code in codes:
        try:
            ohlc = price_provider.ohlc(code, as_of)
        except Exception as e:  # pragma: no cover - 取数容错
            logger.debug(f"[RiskReview] 取 {code}@{as_of} 行情失败: {e}")
            ohlc = None
        if ohlc and float(ohlc.get("close", 0) or 0) > 0:
            out[code] = float(ohlc["close"])
    return out


# ----------------------------------------------------------------------
# 格式化（控制台展示，返回字符串便于单测）
# ----------------------------------------------------------------------
def _pct(x: float) -> str:
    return f"{x:.2%}"


def _flag(ok: bool) -> str:
    return "OK" if ok else "超限!"


def format_account_risk(
    portfolio_state,
    cfg,
    risk_report: Optional[Dict],
    cb_status,
    price_map: Optional[Dict[str, float]] = None,
    as_of: str = "",
) -> str:
    """A 腿：账户持仓风控可读报告。"""
    price_map = price_map or {}
    ps = portfolio_state
    equity = ps.total_equity(price_map)
    pos_value = ps.position_value(price_map)
    pos_ratio = ps.total_position_ratio(price_map)
    cash_ratio = (ps.cash / equity) if equity > 0 else 0.0
    dd = ps.drawdown(price_map)

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append(f"【A. 账户持仓风控】 估值日: {as_of or '最新'} | 持仓 {len(ps.positions)} 只")
    lines.append("=" * 60)

    if not ps.positions:
        lines.append("当前虚拟账户为空仓（无持仓）。")
        lines.append(f"  现金: {ps.cash:,.0f}  权益: {equity:,.0f}")
        lines.append("  提示: 先用 `--mode replay` 或实盘对账写入持仓，再来看持仓风控。")
    else:
        lines.append("账户概览:")
        lines.append(f"  总权益: {equity:,.0f}   现金: {ps.cash:,.0f}   持仓市值: {pos_value:,.0f}")
        lines.append(f"  当日已实现盈亏: {ps.realized_pnl_today:,.0f}   峰值权益: {ps.peak_equity:,.0f}")
        lines.append(
            f"  总仓位: {_pct(pos_ratio)} / 上限 {_pct(cfg.max_total_position)}  [{_flag(pos_ratio <= cfg.max_total_position + 1e-9)}]"
        )
        lines.append(
            f"  现金比例: {_pct(cash_ratio)} / 底线 {_pct(cfg.min_cash_ratio)}  [{_flag(cash_ratio >= cfg.min_cash_ratio - 1e-9)}]"
        )
        lines.append(
            f"  持仓数: {len(ps.positions)} / 上限 {cfg.max_positions} 只  [{_flag(len(ps.positions) <= cfg.max_positions)}]"
        )
        lines.append(
            f"  当前回撤: {_pct(dd)} / 回撤熔断线 -{_pct(cfg.max_drawdown)}  [{_flag(dd > -cfg.max_drawdown)}]"
        )

        # 板块暴露
        sector_exp = ps.sector_exposure(price_map)
        if sector_exp:
            lines.append("板块暴露 (占总权益):")
            for sec, ratio in sorted(sector_exp.items(), key=lambda x: x[1], reverse=True):
                lines.append(
                    f"  - {sec}: {_pct(ratio)} / 上限 {_pct(cfg.max_sector_concentration)}  "
                    f"[{_flag(ratio <= cfg.max_sector_concentration + 1e-9)}]"
                )

        # 个股风险热力（来自 RiskAnalyzer）
        if risk_report:
            summary = risk_report.get("summary", {})
            lines.append(
                f"组合风险等级: {summary.get('overall_risk_level', 'n/a')}  "
                f"(高风险因子 {summary.get('high_risk_count', 0)} 项)"
            )
            heatmap = risk_report.get("risk_heatmap", {})
            stock_risks = heatmap.get("stock_risks", [])
            if stock_risks:
                lines.append("个股风险热力 (风险分高→低):")
                for s in stock_risks:
                    factors = "、".join(s.get("risk_factors", [])) or "无显著风险"
                    lines.append(
                        f"  {s.get('stock_name', '')}({s.get('stock_code', '')}) "
                        f"占比{_pct(s.get('position_ratio', 0))} 风险分{s.get('risk_score', 0)} | {factors}"
                    )
            for rec in risk_report.get("recommendations", []):
                lines.append(f"  建议: {rec}")

    # 账户级熔断
    lines.append("-" * 60)
    if cb_status is not None and cb_status.triggers:
        lines.append(f"账户级熔断: {cb_status.level} | 允许总仓位上限 {_pct(cb_status.position_cap)} | "
                     f"今日{'禁止' if cb_status.halt_new_buys else '允许'}开新仓")
        for t in cb_status.triggers:
            lines.append(f"  - {t}")
    else:
        lines.append("账户级熔断: NORMAL（单日亏损 / 回撤 / 情绪冰点 均未触发）")

    return "\n".join(lines)


def format_gate_review(result, *, show_all: bool = True) -> str:
    """B 腿：交易计划过闸结果可读报告（含逐条决策）。"""
    action_label = {"PASS": "通过", "DOWNGRADE": "降级", "REJECT": "拒绝"}
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append(f"【B. 当日交易计划过闸】 计划日: {result.trade_date or '?'} | 计划 {len(result.decisions)} 条")
    lines.append("=" * 60)

    if not result.decisions:
        lines.append("当日无交易计划（或计划文件不存在 / 为空）。")
        lines.append("  提示: 先用 `--mode analysis` 生成当日 交易计划_{date}.csv。")
        return "\n".join(lines)

    cb = result.cb_status
    if cb is not None and cb.triggers:
        lines.append(f"账户级熔断: {cb.level}")
        for t in cb.triggers:
            lines.append(f"  - {t}")
    else:
        lines.append("账户级熔断: NORMAL（无触发）")
    lines.append(
        f"有效总仓位上限: {_pct(result.effective_total_cap)} | "
        f"开闸前持仓: {_pct(result.total_position_before)} | "
        f"评估权益: {result.equity:,.0f}"
    )
    lines.append(
        f"决策汇总: 通过 {result.passed} / 降级 {result.downgraded} / 拒绝 {result.rejected}"
    )
    lines.append("逐条决策:")
    for d in result.decisions:
        if not show_all and d.action == "PASS":
            continue
        lines.append(
            f"  [{action_label.get(d.action, d.action)}] {d.stock_name}({d.stock_code}) "
            f"{_pct(d.original_position_pct)}→{_pct(d.final_position_pct)} | {d.reason_text}"
        )
    return "\n".join(lines)


__all__ = [
    "load_trade_plan_result_from_csv",
    "portfolio_risk_positions",
    "build_price_map",
    "format_account_risk",
    "format_gate_review",
]
