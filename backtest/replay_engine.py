"""
历史重演引擎（B-2）

与旧 ``BacktestEngine``（计划回放 + 单点随机价）不同，``ReplayEngine`` 是一个
**真实撮合的执行模拟器**：

- 账本用 R-1 的 ``PortfolioState``（实盘 / 回测共用，规则不漂移）。
- 撮合用 B-1 的 ``matching_rules``（涨跌停 / 一字板 / 停牌 / 滑点）。
- 行情用 B-2a 的 point-in-time 价格源（``AsOfPriceProvider`` / ``StaticPriceProvider``）。
- 交易计划由 ``plan_provider`` 注入，两种实现：
    * ``PipelinePlanProvider``——以历史某日为"今天"重跑流水线（真·历史重演，需数据/token）；
    * ``CsvPlanProvider``——读取已落盘的（且已过 L4.5 风控闸门的）交易计划 CSV。

执行时序（严格 T+1）：在交易日 T，
  1. 先用 T 的行情对**已有持仓**做退出判定（止损/移动止盈/时间止损/分批止盈/止盈）；
  2. 再执行"T-1 收盘后制定"的买入计划（用 T 的行情撮合）；
  3. 收盘盯市，记录净值。

输出 dict 与 ``PerformanceAnalyzer`` 兼容（daily_nav / trade_history / max_drawdown ...）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import loguru

from backtest.backtest_engine import TradeRecord
from backtest.matching_rules import normalize_code, open_gap_pct, simulate_buy, simulate_sell
from backtest.trade_calendar import TradeCalendar
from risk.portfolio_state import PortfolioState
from risk.risk_config import RiskConfig

logger = loguru.logger


@dataclass
class ReplayPlan:
    """重演引擎消费的标准化买入计划（T-1 收盘后制定，T 执行）。"""
    code: str
    name: str = ""
    pattern: str = ""
    target_price: float = 0.0          # 期望买入价（0 表示按开盘价）
    stop_price: float = 0.0            # 绝对止损价（0 表示用配置 hard_stop_loss）
    take_profit_price: float = 0.0     # 绝对止盈价（0 表示用配置 take_profit）
    position_pct: float = 0.0          # 建议仓位（占总权益）
    sectors: List[str] = field(default_factory=list)
    hot_resonance: bool = False

    @property
    def code6(self) -> str:
        return normalize_code(self.code)

    @property
    def sector(self) -> str:
        return self.sectors[0] if self.sectors else "未知"


# plan_provider 协议：传入"制定日"，返回当日收盘后产出的买入计划列表
PlanProvider = Callable[[str], List[ReplayPlan]]


class ReplayEngine:
    """历史重演执行引擎。"""

    def __init__(self,
                 plan_provider: PlanProvider,
                 price_provider,
                 config: Optional[RiskConfig] = None,
                 calendar: Optional[TradeCalendar] = None,
                 sizing_fn: Optional[Callable[["ReplayPlan"], float]] = None):
        self.cfg = config or RiskConfig()
        self.plan_provider = plan_provider
        self.price = price_provider
        self.calendar = calendar or TradeCalendar()
        # 可选仓位钩子：给定计划返回仓位比例（用于闭环标定 / 凯利 / walk-forward）
        self.sizing_fn = sizing_fn

        self.account = PortfolioState.new(self.cfg.initial_capital)
        self.trade_history: List[TradeRecord] = []
        self.daily_nav: List[Dict] = []
        # 每只持仓的退出元数据：止损/止盈价、峰值、分批标记
        self._exit_meta: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    def run(self, start_date: str, end_date: str) -> Dict:
        dates = self.calendar.get_trade_dates(start_date, end_date)
        logger.info(f"[ReplayEngine] 回测 {start_date}~{end_date}，共 {len(dates)} 个交易日")

        for t in dates:
            day_px = self.price.day_prices(t) or {}
            self.account.reset_daily()
            self._process_exits(t, day_px)

            prev = self.calendar.prev(t)
            plans = self.plan_provider(prev) or []
            self._process_entries(t, plans, day_px)

            close_map = {c: o.get("close", 0.0) for c, o in day_px.items()
                         if o.get("close", 0) > 0}
            equity = self.account.mark_to_market(close_map, t)
            self.daily_nav.append({
                "date": t,
                "cash": round(self.account.cash, 2),
                "position_value": round(self.account.position_value(close_map), 2),
                "total_value": round(equity, 2),
                "position_count": len(self.account.positions),
            })

        report = self._build_report()
        logger.info(
            f"[ReplayEngine] 完成：最终权益 {report['final_capital']:,.0f}，"
            f"收益 {report['total_return']:.2%}，成交 {report['total_trades']} 笔"
        )
        return report

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------
    def _process_exits(self, date: str, day_px: Dict[str, Dict]):
        for code in list(self.account.positions.keys()):
            pos = self.account.positions[code]
            ohlc = day_px.get(code)
            if not ohlc or ohlc.get("close", 0) <= 0 or ohlc.get("high", 0) <= 0:
                continue  # 停牌 / 无行情：当日不可交易，持有

            # T+1：买入当日不卖
            holding_days = self.calendar.holding_days(pos.entry_date, date)
            if holding_days < max(1, self.cfg.min_holding_days):
                # 仍要更新峰值
                self._exit_meta.setdefault(code, {})["peak"] = max(
                    self._exit_meta.get(code, {}).get("peak", pos.avg_cost),
                    ohlc.get("high", pos.avg_cost),
                )
                continue

            meta = self._exit_meta.setdefault(code, {})
            entry = pos.avg_cost
            high = ohlc.get("high", 0.0)
            low = ohlc.get("low", 0.0)
            close = ohlc.get("close", 0.0)
            openp = ohlc.get("open", close)
            pre_close = ohlc.get("pre_close", 0.0) or pos.last_price
            peak = max(meta.get("peak", entry), high)
            meta["peak"] = peak

            stop_price = meta.get("stop_price") or entry * (1 - self.cfg.hard_stop_loss)
            tp_price = meta.get("tp_price") or entry * (1 + self.cfg.take_profit)

            intended = None
            reason = None
            partial_shares = 0

            # 1) 硬止损（最高优先）
            if low <= stop_price:
                intended = min(stop_price, openp)  # 跳空低开则按开盘
                reason = "stop_loss"
            # 2) 移动止盈
            elif (self.cfg.trailing_stop and peak > entry
                  and (peak - entry) / entry >= self.cfg.trailing_activation
                  and (peak - close) / peak >= self.cfg.trailing_stop):
                intended = close
                reason = "trailing_stop"
            # 3) 时间止损
            elif (holding_days >= self.cfg.time_stop_days
                  and (close - entry) / entry < self.cfg.time_stop_profit_threshold):
                intended = close
                reason = "time_stop"
            # 4) 分批止盈（第一段）
            elif (not meta.get("partial1") and high >= entry * 1.08):
                partial_shares = (pos.shares // 2 // 100) * 100
                if partial_shares >= 100:
                    intended = max(entry * 1.08, openp)
                    reason = "partial_first"
                    meta["partial1"] = True
            # 5) 基础止盈
            if intended is None and high >= tp_price:
                intended = max(tp_price, openp)
                reason = "take_profit"

            if intended is None or reason is None:
                continue

            shares = partial_shares if partial_shares >= 100 else pos.shares
            self._sell(code, date, ohlc, pre_close, intended, reason, shares)

    def _sell(self, code, date, ohlc, pre_close, intended_price, reason, shares):
        pos = self.account.positions.get(code)
        if pos is None or shares <= 0:
            return
        shares = min(shares, pos.shares)
        ok, fill, why = simulate_sell(
            intended_price, ohlc, pre_close, code, pos.name, slippage=self.cfg.slippage
        )
        if not ok:
            logger.debug(f"[{date}] {pos.name}({code}) 退出受阻({reason}): {why}")
            return

        gross = fill * shares
        fee = gross * (self.cfg.commission_rate + self.cfg.stamp_duty_rate)
        entry_price = pos.avg_cost
        cost = entry_price * shares
        realized = self.account.apply_sell(code, fill, shares, fee=fee)
        holding_days = self.calendar.holding_days(pos.entry_date, date)

        meta = self._exit_meta.get(code, {})
        self.trade_history.append(TradeRecord(
            date=date, stock_code=code, stock_name=pos.name,
            pattern_type=meta.get("pattern", ""),
            action="SELL_PARTIAL" if reason.startswith("partial") else "SELL",
            entry_price=entry_price, exit_price=fill, shares=shares,
            position_size=cost, pnl=realized,
            pnl_pct=(realized / cost if cost > 0 else 0.0),
            holding_days=holding_days,
            hot_resonance=meta.get("hot_resonance", False),
            resonance_sectors=meta.get("sector", ""),
            stop_loss_triggered=(reason == "stop_loss"),
            take_profit_triggered=reason.startswith(("take_profit", "partial")),
        ))
        logger.info(f"[{date}] 卖出 {pos.name}({code}) {shares}股 @ {fill:.2f} "
                    f"[{reason}] 盈亏 {realized:,.0f}")
        if code not in self.account.positions:
            self._exit_meta.pop(code, None)

    # ------------------------------------------------------------------
    # 买入
    # ------------------------------------------------------------------
    def _process_entries(self, date: str, plans: List[ReplayPlan], day_px: Dict[str, Dict]):
        if not plans:
            return
        equity = self.account.total_equity()
        if equity <= 0:
            return

        # 当前组合状态（随买入累计收紧）
        cur_total = self.account.position_value()
        sector_val: Dict[str, float] = {}
        for c, p in self.account.positions.items():
            sec = p.sector or "未知"
            sector_val[sec] = sector_val.get(sec, 0.0) + p.market_value()

        # 风控总开关关闭：不施加组合层约束（仅受现金限制），用于对比"无风控"模拟
        risk_on = getattr(self.cfg, "enabled", True)
        if risk_on:
            total_budget = equity * min(self.cfg.max_total_position, 1 - self.cfg.min_cash_ratio)
            per_stock_cap = equity * self.cfg.max_position_per_stock
            sector_cap = equity * self.cfg.max_sector_concentration
        else:
            total_budget = per_stock_cap = sector_cap = float("inf")

        for plan in sorted(plans, key=lambda p: -p.position_pct):
            code = plan.code6
            if code in self.account.positions:
                continue  # 已持仓不加仓
            if risk_on and len(self.account.positions) >= self.cfg.max_positions:
                break
            ohlc = day_px.get(code)
            if not ohlc:
                continue  # 停牌 / 无行情
            pre_close = ohlc.get("pre_close", 0.0)
            gap = open_gap_pct(ohlc, pre_close)
            if gap is None:
                logger.debug(f"[{date}] {plan.name}({code}) 无法确认早盘是否高开，放弃买入")
                continue
            if gap <= 0:
                label = "低开" if gap < 0 else "平开"
                logger.info(f"[{date}] {plan.name}({code}) {label}{gap:.2%}，未高开，放弃竞价买点")
                continue

            position_pct = self.sizing_fn(plan) if self.sizing_fn else plan.position_pct
            if position_pct <= 0:
                continue  # 仓位钩子判定该计划不建仓（如凯利负边）
            desired = equity * position_pct
            allowed = min(desired, per_stock_cap, max(total_budget - cur_total, 0.0))
            sec = plan.sector
            if sec != "未知":
                allowed = min(allowed, max(sector_cap - sector_val.get(sec, 0.0), 0.0))
            if allowed < equity * 0.005:
                continue  # 无有效额度

            ok, fill, why = simulate_buy(
                plan.target_price, ohlc, pre_close, code, plan.name, slippage=self.cfg.slippage
            )
            if not ok:
                logger.debug(f"[{date}] {plan.name}({code}) 未成交: {why}")
                continue

            shares = int(allowed / fill / 100) * 100
            if shares < 100:
                continue
            fee = fill * shares * self.cfg.commission_rate
            if fill * shares + fee > self.account.cash:
                shares = int((self.account.cash * 0.999) / fill / 100) * 100
                if shares < 100:
                    continue
                fee = fill * shares * self.cfg.commission_rate

            self.account.apply_buy(
                code, fill, shares, fee=fee, name=plan.name,
                sector=sec, pattern=plan.pattern, date=date,
            )
            self._exit_meta[code] = {
                "peak": fill,
                "stop_price": plan.stop_price or 0.0,
                "tp_price": plan.take_profit_price or 0.0,
                "pattern": plan.pattern,
                "sector": ",".join(plan.sectors) if plan.sectors else "",
                "hot_resonance": plan.hot_resonance,
            }
            value = fill * shares
            cur_total += value
            sector_val[sec] = sector_val.get(sec, 0.0) + value
            logger.info(f"[{date}] 买入 {plan.name}({code}) {shares}股 @ {fill:.2f} "
                        f"[{plan.pattern}] 仓位 {value / equity:.0%}")

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------
    def _build_report(self) -> Dict:
        initial = self.cfg.initial_capital
        final = self.daily_nav[-1]["total_value"] if self.daily_nav else initial
        total_return = (final - initial) / initial if initial > 0 else 0.0

        max_dd = 0.0
        if self.daily_nav:
            nav = pd.DataFrame(self.daily_nav)
            nav["cummax"] = nav["total_value"].cummax()
            nav["dd"] = (nav["total_value"] - nav["cummax"]) / nav["cummax"]
            max_dd = float(nav["dd"].min())

        sells = [t for t in self.trade_history if t.action in ("SELL", "SELL_PARTIAL")]
        wins = [t for t in sells if t.pnl > 0]
        win_rate = (len(wins) / len(sells)) if sells else 0.0

        return {
            "initial_capital": initial,
            "final_capital": final,
            "total_return": total_return,
            "max_drawdown": max_dd,
            "win_rate": win_rate,
            "total_trades": len(sells),
            "daily_nav": self.daily_nav,
            "trade_history": self.trade_history,
        }


__all__ = ["ReplayEngine", "ReplayPlan", "PlanProvider"]
