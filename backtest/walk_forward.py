"""
Walk-forward 滚动验证（C-3）

防过拟合的核心护栏：把回测区间切成若干"训练窗口 → 紧邻的样本外(OOS)测试窗口"，
在**训练窗口**上用 ``compute_strategy_stats`` + ``KellySizer`` 标定出分模式仓位，
再把这套仓位**应用到从未见过的测试窗口**，只统计样本外表现。

同时跑一条"基线"（不标定、用计划原始仓位）的 OOS，给出"标定 vs 基线"的对比——
如果标定只在样本内好看、样本外打不过基线，就说明是过拟合。

非重叠 OOS：测试窗口按 ``test_size`` 平铺，互不重叠。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import loguru

from backtest.replay_engine import ReplayEngine, ReplayPlan
from backtest.strategy_stats import compute_strategy_stats
from backtest.trade_calendar import TradeCalendar
from risk.kelly_sizer import KellySizer
from risk.risk_config import RiskConfig

logger = loguru.logger


def _make_sizing_fn(table: Dict[str, Dict]):
    """把分模式标定表包成 ReplayEngine 的 sizing_fn。"""
    def fn(plan: ReplayPlan) -> float:
        entry = table.get(plan.pattern) or table.get("__overall__")
        if not entry:
            return plan.position_pct
        return float(entry.get("position_pct", plan.position_pct))
    return fn


@dataclass
class WalkForwardResult:
    folds: List[Dict] = field(default_factory=list)
    aggregate: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {"folds": self.folds, "aggregate": self.aggregate}


class WalkForwardValidator:
    def __init__(self,
                 plan_provider,
                 price_provider,
                 config: Optional[RiskConfig] = None,
                 calendar: Optional[TradeCalendar] = None,
                 train_size: int = 40,
                 test_size: int = 20,
                 base_position_pct: float = 0.10):
        self.plan_provider = plan_provider
        self.price = price_provider
        self.cfg = config or RiskConfig()
        self.calendar = calendar or TradeCalendar()
        self.train_size = train_size
        self.test_size = test_size
        self.base_position_pct = base_position_pct
        self.kelly = KellySizer(self.cfg)

    def _run(self, dates: List[str], sizing_fn=None) -> Dict:
        eng = ReplayEngine(self.plan_provider, self.price, config=self.cfg,
                           calendar=self.calendar, sizing_fn=sizing_fn)
        return eng.run(dates[0], dates[-1])

    def run(self, start_date: str, end_date: str) -> WalkForwardResult:
        dates = self.calendar.get_trade_dates(start_date, end_date)
        result = WalkForwardResult()
        if len(dates) < self.train_size + self.test_size:
            logger.warning(
                f"[WalkForward] 交易日不足：{len(dates)} < "
                f"train{self.train_size}+test{self.test_size}，无法验证"
            )
            return result

        i = 0
        cal_calib, cal_base, win_rates = [], [], []
        worst_dd = 0.0
        while i + self.train_size + self.test_size <= len(dates):
            train = dates[i:i + self.train_size]
            test = dates[i + self.train_size:i + self.train_size + self.test_size]

            train_report = self._run(train)
            stats = compute_strategy_stats(train_report["trade_history"])
            table = self.kelly.build_pattern_table(stats, self.base_position_pct)

            calib = self._run(test, sizing_fn=_make_sizing_fn(table))
            base = self._run(test)  # 基线：原始仓位

            cal_calib.append(calib["total_return"])
            cal_base.append(base["total_return"])
            win_rates.append(calib["win_rate"])
            worst_dd = min(worst_dd, calib["max_drawdown"])

            result.folds.append({
                "train": [train[0], train[-1]],
                "test": [test[0], test[-1]],
                "oos_return_calibrated": round(calib["total_return"], 4),
                "oos_return_baseline": round(base["total_return"], 4),
                "oos_win_rate": round(calib["win_rate"], 4),
                "oos_max_drawdown": round(calib["max_drawdown"], 4),
                "oos_trades": calib["total_trades"],
                "sizing_table": {k: v.get("position_pct") for k, v in table.items()},
            })
            i += self.test_size

        def _compound(rs: List[float]) -> float:
            acc = 1.0
            for r in rs:
                acc *= (1 + r)
            return acc - 1

        n = len(result.folds)
        result.aggregate = {
            "folds": n,
            "oos_compound_return_calibrated": round(_compound(cal_calib), 4),
            "oos_compound_return_baseline": round(_compound(cal_base), 4),
            "oos_avg_win_rate": round(sum(win_rates) / n, 4) if n else 0.0,
            "oos_worst_drawdown": round(worst_dd, 4),
            "calibrated_beats_baseline": _compound(cal_calib) > _compound(cal_base),
        }
        logger.info(
            f"[WalkForward] {n} 折 OOS：标定 {result.aggregate['oos_compound_return_calibrated']:.2%} "
            f"vs 基线 {result.aggregate['oos_compound_return_baseline']:.2%}"
        )
        return result


__all__ = ["WalkForwardValidator", "WalkForwardResult"]
