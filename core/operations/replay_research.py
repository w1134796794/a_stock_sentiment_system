"""Frozen, account-isolated replay comparisons; never publish production weights."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.evidence_policy import ReplayEvidenceBlocked
from backtest.trade_calendar import TradeCalendar
from config.settings import BASE_DIR, WEB_DATA_DIR
from core.operations.ledger import TradingLedger, stable_id
from core.signals.minute_amount_profile import MinuteAmountProfileRepository, validate_historical_profile
from core.utils.sector_names import normalize_sector_names
from core.utils.stock_code_utils import StockCodeUtils

VERSION = "frozen_replay_research_v1"
ARMS = {
    "baseline": {"name": "原34%三仓", "change": "可信基准"},
    "fixed_risk": {"name": "固定风险", "change": "仅仓位算法"},
    "timing": {"name": "持续确认", "change": "固定候选、仓位、退出，仅择时"},
    "selection": {"name": "候选前半组", "change": "固定择时、仓位、退出，仅选股"},
}


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(clean(value), ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(clean(value), ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    for attempt in range(8):
        try:
            pending.replace(path)
            break
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32} or attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


@contextmanager
def experiment_lock(path: Path):
    """OS advisory lock releases on process exit; a crashed run remains resumable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("同一冻结实验已在其他进程运行") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def implementation_version() -> str:
    paths = set((Path(BASE_DIR) / "backtest").glob("*.py"))
    paths.update((Path(BASE_DIR) / "core/signals").glob("*.py"))
    paths.update((Path(BASE_DIR) / "risk").glob("*.py"))
    paths.update(Path(BASE_DIR) / name for name in (
        "core/operations/replay_research.py", "core/operations/strategy_optimization.py",
        "core/operations/sizing_research.py", "core/utils/stock_eligibility.py",
        "core/utils/stock_code_utils.py", "core/utils/sector_names.py",
        "core/data/minute_quality.py", "core/data/data_manager_suspension.py",
    ))
    return digest({path.relative_to(BASE_DIR).as_posix(): hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                   for path in sorted(paths)})


@dataclass(frozen=True)
class ResearchSpec:
    train_days: int = 30
    validation_days: int = 10
    embargo_days: int = 5
    fixed_risk_per_trade: float = 0.005
    selection_fraction: float = 0.5
    sustain_minutes: int = 2
    min_train_closed: int = 20
    min_validation_closed: int = 30
    min_validation_folds: int = 3
    missing_evidence_policy: str = "strict"
    suite: str = "legacy"
    optimization: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.suite not in {"legacy", "confirmation_quality", "return_refinement", "in_sample_tuning", "position_risk"}:
            raise ValueError("未知研究对照方案")
        if self.suite == "position_risk":
            from core.operations.sizing_research import SizingParameters
            SizingParameters(**self.optimization)
        elif self.suite != "legacy":
            from core.operations.strategy_optimization import OptimizationParameters
            OptimizationParameters(**self.optimization)
        elif self.optimization:
            raise ValueError("基础四组对照不能混入优化参数")
        if any(not isinstance(x, int) or x < 1 for x in (
            self.train_days, self.validation_days, self.sustain_minutes,
            self.min_train_closed, self.min_validation_closed, self.min_validation_folds,
        )) or self.embargo_days < 0:
            raise ValueError("研究窗口、样本数和持续分钟必须为正整数")
        if not 0 < self.fixed_risk_per_trade <= 0.02 or not 0 < self.selection_fraction <= 1:
            raise ValueError("单笔风险或候选比例无效")
        if self.missing_evidence_policy not in {"strict", "skip_candidates"}:
            raise ValueError("独立研究仅支持严格模式或跳过缺证据候选")


def research_arms(spec):
    if spec.suite == "position_risk":
        from core.operations.sizing_research import SIZING_ARMS
        return SIZING_ARMS
    if spec.suite == "in_sample_tuning":
        from core.operations.strategy_optimization import TUNING_ARMS
        return TUNING_ARMS
    if spec.suite == "return_refinement":
        from core.operations.strategy_optimization import REFINEMENT_ARMS
        return REFINEMENT_ARMS
    if spec.suite == "confirmation_quality":
        from core.operations.strategy_optimization import OPTIMIZATION_ARMS
        return OPTIMIZATION_ARMS
    return ARMS


def research_engine(evidence, config, manifest, *, arm):
    if ResearchSpec(**manifest["spec"]).suite == "position_risk":
        from core.operations.sizing_research import SizingResearchEngine
        return SizingResearchEngine(evidence, config, manifest, arm=arm)
    if ResearchSpec(**manifest["spec"]).suite != "legacy":
        from core.operations.strategy_optimization import OptimizationEngine
        return OptimizationEngine(evidence, config, manifest, arm=arm)
    return ResearchEngine(evidence, config, manifest, arm=arm)


class FrozenCalendar:
    is_real = True

    def __init__(self, dates: list[str]):
        self.dates = dates

    def get_trade_dates(self, start: str, end: str) -> list[str]:
        return [d for d in self.dates if start <= d <= end]

    def is_trade_date(self, date: str) -> bool:
        return date in self.dates

    def prev(self, date: str) -> str:
        earlier = [d for d in self.dates if d < date]
        if not earlier:
            raise ValueError(f"冻结日历缺少 {date} 的前一交易日")
        return earlier[-1]

    def next(self, date: str) -> str:
        later = [d for d in self.dates if d > date]
        if not later:
            raise ValueError(f"冻结日历缺少 {date} 的下一交易日")
        return later[0]

    def holding_days(self, start: str, end: str) -> int:
        return max(0, len(self.get_trade_dates(start, end)) - 1)


class FrozenEvidence:
    """Read-only SQLite snapshot, independent of caches and market connections."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "evidence.sqlite"

    def read(self, kind: str, code: str, date: str) -> Any:
        code = str(code).split(".")[0].zfill(6)
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT payload FROM evidence WHERE kind=? AND code=? AND date=?",
                               (kind, code, date)).fetchone()
        return json.loads(row[0]) if row else ( [] if kind == "minutes" else {})

    def get_stock_tick(self, code: str, date: str) -> pd.DataFrame:
        return pd.DataFrame(self.read("minutes", code, date))

    def get_stock_daily_data(self, code: str, date: str) -> dict:
        return self.read("daily", code, date)

    def get_auction_data(self, code: str, date: str) -> dict:
        return self.read("auction", code, date)

    def get_suspension_status(self, code: str, date: str, **_) -> dict:
        return self.read("suspension", code, date)

    def get_minute_quality_report(self, code: str, date: str) -> dict:
        return {"reason": "冻结证据包中分钟数据缺失或残缺；补齐后须创建新实验"}


class ResearchEngine(BacktestEngine):
    def __init__(self, evidence, config, manifest, *, arm: str):
        super().__init__(evidence, config)
        self.calendar = FrozenCalendar(manifest["calendar"])
        self.frozen_peers = manifest["sector_peers"]
        self.amount_profiles = MinuteAmountProfileRepository(evidence.directory / "amount_profile.json")
        self.arm = arm
        self.spec = ResearchSpec(**manifest["spec"])
        self.research_entry_prices = {}

    def _execute_buy(self, plan, date, *, entry_decision=None):
        if entry_decision is not None and entry_decision[0]:
            self.research_entry_prices[str(plan["代码"]).zfill(6)] = entry_decision[1]
        return super()._execute_buy(plan, date, entry_decision=entry_decision)

    def _calculate_position_size(self, plan):
        if self.arm == "baseline":
            return super()._calculate_position_size(plan)
        code = str(plan["代码"]).zfill(6)
        entry = self.research_entry_prices.get(code, 0)
        exit_config = self._execution_exit_config(self._plan_execution(plan))
        distance = float(exit_config["hard_stop_loss"])
        structural = float(self._last_entry_meta.get(code, {}).get("structural_stop") or 0)
        if 0 < structural < entry:
            distance = min(distance, 1 - structural / entry)
        # Include entry/exit costs; the hard stop still cannot bound an overnight gap.
        risk = max(distance, 0) + 2 * self.config.commission_rate + self.config.stamp_duty_rate + self.config.slippage
        pct = min(self.config.max_position_per_stock, self.spec.fixed_risk_per_trade / max(risk, 0.001))
        self._last_sizing_meta = {"method": "research_fixed_risk", "position_pct": pct,
                                 "rationale": f"冻结单笔风险{self.spec.fixed_risk_per_trade:.2%}，含成本风险距离{risk:.2%}", "sample_size": 0}
        return self.total_capital * pct

    def _load_sector_peer_codes(self, date, sector_map, **_):
        return {code: list(peers) for code, peers in self.frozen_peers.get(date, {}).items()}

    def _load_exit_minute_bars(self, code, date):
        from core.data.minute_quality import inspect_minute_bars
        key = (date, code)
        if key not in self._exit_minute_frames:
            raw = self.dm.get_stock_tick(code, date)
            valid = inspect_minute_bars(raw, date, expected_code=code)["valid"]
            self._exit_minute_frames[key] = self._normalize_full_minute_bars(raw) if valid else pd.DataFrame()
        return self._exit_minute_frames[key]

    def _check_buy_conditions(self, plan, date, stock_code, stock_name):
        allowed, price = super()._check_buy_conditions(plan, date, stock_code, stock_name)
        if allowed:
            self.research_entry_prices[stock_code] = price
        if not allowed or self.arm != "timing":
            return allowed, price
        # A pre-registered post-confirmation wait, not a hindsight best entry.
        meta = self._last_entry_meta[stock_code]
        bars = self._minute_frames[(date, stock_code)]
        confirm = bars[bars["time"] == meta["confirm_time"]]
        following = bars[bars["time"] > meta["confirm_time"]]
        count = self.spec.sustain_minutes
        okay = not confirm.empty and len(following) > count
        if okay:
            rows = following.iloc[:count + 1]
            times = [meta["confirm_time"], *rows["time"].tolist()]
            seconds = [datetime.strptime(t, "%H:%M:%S") for t in times]
            okay = all((b - a).total_seconds() == 60 for a, b in zip(seconds, seconds[1:], strict=False))
            wait = rows.iloc[:count]
            floor = float(confirm.iloc[0]["close"])
            okay = okay and bool(((wait["close"] >= wait["vwap"]) & (wait["close"] >= floor)).all())
            execution = self._plan_execution(plan)
            from backtest.minute_entry import resolve_entry_deadline
            mode = self.entry_attempts[-1].get("entry_mode") or self._entry_mode_for_plan(plan, self._last_entry_gap[stock_code])
            deadline = resolve_entry_deadline(execution, mode,
                                              self.config.entry_confirm_deadline)
            fill = rows.iloc[-1]
            okay = okay and str(fill["time"]) <= deadline and float(fill["volume"]) > 0
            fill_price = float(fill["open"]) * (1 + self.config.slippage)
            from backtest.matching_rules import limit_up_price
            daily = self.dm.get_stock_daily_data(stock_code, date)
            limit = limit_up_price(float(daily.get("pre_close") or 0), stock_code, stock_name)
            okay = okay and fill_price > 0 and (not limit or fill_price < float(limit) * 0.998)
            checker = self._sector_sync_checker(plan, date, stock_code)
            okay = okay and checker(str(wait.iloc[-1]["time"])) is True
        if not okay:
            self.entry_attempts[-1].update(status="research_filtered", reason_code="sustain_not_confirmed")
            self._record_gate_attempt(plan, date, "research_timing", "sustain_not_confirmed",
                                      "原确认后未完成连续分钟承接或缺少可成交证据")
            self._clear_entry_state(stock_code)
            return False, 0.0
        meta["entry_time"] = str(fill["time"])
        meta["confirm_time"] = str(wait.iloc[-1]["time"])
        self.research_entry_prices[stock_code] = fill_price
        self.entry_attempts[-1].update(entry_time=meta["entry_time"], confirm_time=meta["confirm_time"],
                                       entry_price=float(fill["open"]), research_timing="sustained")
        return True, fill_price


def select_plans(frame: pd.DataFrame, fraction: float) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    selected = []
    for _, group in frame.groupby("策略ID", dropna=False, sort=False):
        group = group.copy()
        group["_score"] = pd.to_numeric(group["综合评分"], errors="coerce")
        group["_code"] = group["代码"].astype(str)
        group = group.sort_values(["_score", "_code"], ascending=[False, True], na_position="last")
        selected.append(group.head(max(1, math.ceil(len(group) * fraction))).drop(columns=["_score", "_code"]))
    return pd.concat(selected, ignore_index=True)


def account_metrics(state: dict, initial: float, *, start: str = "", end: str = "") -> dict:
    nav = [r for r in state.get("daily_nav", []) if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    earlier = [r for r in state.get("daily_nav", []) if start and r["date"] < start]
    initial = float(earlier[-1]["total_value"]) if earlier else initial
    closed = [r for r in state.get("trade_history", []) if str(r["action"]).startswith("SELL")
              and (not start or r.get("entry_date", "") >= start) and (not end or r["date"] <= end)]
    peak, worst, underwater, longest = initial, 0.0, 0, 0
    returns, previous = [], initial
    for row in nav:
        value = float(row["total_value"])
        returns.append(value / previous - 1)
        previous = value
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
        underwater = underwater + 1 if value < peak else 0
        longest = max(longest, underwater)
    pnl = [float(r["pnl"]) for r in closed]
    losses = [x for x in pnl if x < 0]
    wins = [x for x in pnl if x > 0]
    streak = longest_losses = 0
    for x in pnl:
        streak = streak + 1 if x < 0 else 0
        longest_losses = max(longest_losses, streak)
    relevant = [t for t in state.get("trade_history", []) if (not start or t["date"] >= start) and (not end or t["date"] <= end)]
    fees = sum(max(0, float(t["position_size"]) - float(t["entry_price"]) * float(t["shares"]))
               if t["action"] == "BUY" else max(0, float(t["exit_price"]) * float(t["shares"]) - float(t["position_size"]) - float(t["pnl"])) for t in relevant)
    return {"days": len(nav), "return_pct": (previous / initial - 1) * 100, "fees": fees,
            "max_drawdown_pct": -worst * 100, "worst_day_pct": min(returns, default=0) * 100,
            "drawdown_duration_days": longest, "closed_trades": len(closed),
            "win_rate_pct": len(wins) / len(closed) * 100 if closed else None,
            "net_closed_pnl": sum(pnl), "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "max_consecutive_losses": longest_losses,
            "worst_trade_pct": min((float(r["pnl_pct"]) * 100 for r in closed), default=None),
            "mean_mfe_pct": sum(float(r.get("mfe_pct") or 0) * 100 for r in closed) / len(closed) if closed else None,
            "mean_mae_pct": sum(float(r.get("mae_pct") or 0) * 100 for r in closed) / len(closed) if closed else None}


def grouped_metrics(state: dict, regimes: dict, *, start: str, end: str) -> list[dict]:
    attempts = [a for a in state.get("entry_attempts", []) if start <= a["date"] <= end]
    trades = [t for t in state.get("trade_history", []) if str(t["action"]).startswith("SELL")
              and start <= t.get("entry_date", "") <= t["date"] <= end]
    keys = {(a.get("strategy_id", "default"), regimes.get(a["date"], "unknown")) for a in attempts}
    keys.update((t.get("strategy_id", "default"), regimes.get(t["entry_date"], "unknown")) for t in trades)
    output = []
    for strategy, regime in sorted(keys):
        rows = [a for a in attempts if a.get("strategy_id", "default") == strategy and regimes.get(a["date"], "unknown") == regime]
        closed = [t for t in trades if t.get("strategy_id", "default") == strategy and regimes.get(t["entry_date"], "unknown") == regime]
        candidates = {(r["date"], r["stock_code"]) for r in rows}
        confirmed = {(r["date"], r["stock_code"]) for r in rows if r.get("status") == "filled"}
        buys = [t for t in state.get("trade_history", []) if t["action"] == "BUY" and start <= t["date"] <= end
                and t.get("strategy_id", "default") == strategy and regimes.get(t["date"], "unknown") == regime]
        output.append({"strategy_id": strategy, "regime": regime, "candidates": len(candidates),
                       "confirmed": len(confirmed), "filled": len(buys), "closed": len(closed),
                       "confirmation_rate_pct": len(confirmed) / len(candidates) * 100 if candidates else None,
                       "fill_rate_pct": len(buys) / len(confirmed) * 100 if confirmed else None,
                       "net_pnl": sum(float(t["pnl"]) for t in closed),
                       "win_rate_pct": sum(float(t["pnl"]) > 0 for t in closed) / len(closed) * 100 if closed else None,
                       "sample_status": "descriptive_only"})
    return output


class ReplayResearch:
    def __init__(self, *, root: Path | None = None, ledger: TradingLedger | None = None):
        self.root = Path(root or Path(WEB_DATA_DIR) / "replay_research")
        self.ledger = ledger or TradingLedger()

    def _directory(self, ident: str) -> Path:
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("研究实验ID无效")
        return self.root / ident

    def _publish(self, report: dict):
        atomic_json(self._directory(report["experiment_id"]) / "report.json", report)
        self.ledger.save_experiment(report["experiment_id"], report["frozen_at"],
                                   report.get("configuration_hash", ""), report.get("implementation_hash", ""), report)

    def freeze(self, *, plans_dir: Path, start: str, end: str, data_manager,
               calendar=None, spec: ResearchSpec | None = None, config: BacktestConfig | None = None,
               coverage_dates: list[str] | None = None, regimes: dict | None = None,
               profile_snapshot: dict | None = None, progress: Callable | None = None,
               factor_db_path: Path | None = None, amount_profile_path: Path | None = None) -> dict:
        spec = spec or ResearchSpec()
        if spec.suite == "position_risk":
            from core.operations.sizing_research import SizingParameters
            spec = replace(spec, optimization=asdict(SizingParameters(**spec.optimization)))
        elif spec.suite != "legacy":
            from core.operations.strategy_optimization import OptimizationParameters
            spec = replace(spec, optimization=asdict(OptimizationParameters(**spec.optimization)))
        arms = research_arms(spec)
        calendar = calendar or TradeCalendar()
        if not calendar.is_real:
            raise ValueError("可信研究需要真实交易日历")
        dates = calendar.get_trade_dates(start, end)
        if start > end or not dates or end > datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d"):
            raise ValueError("研究区间无效或包含未来日期")
        if not dates or dates[0] != start or dates[-1] != end:
            raise ValueError("开始、结束日期必须是有效交易日")
        plan_dates = {date: calendar.prev(date) for date in dates}
        history_dates = []
        if spec.suite != "legacy":
            history_dates = calendar.get_trade_dates("19000101", calendar.prev(start))[-spec.optimization["gap_lookback"]:]
            if len(history_dates) < spec.optimization["gap_min_samples"]:
                raise ValueError("冻结日历缺少区间前隔夜风险历史")
        frames = {}
        covered = set(coverage_dates or [])
        for day in sorted(set(plan_dates.values())):
            file = plans_dir / f"交易计划_{day}.csv"
            if not file.exists() and day not in covered:
                raise ValueError(f"{day} 缺少候选日覆盖证明，不能视为零候选")
            frame = pd.read_csv(file, dtype={"代码": str}) if file.exists() else pd.DataFrame(columns=["代码", "策略ID", "综合评分"])
            if not frame.empty:
                required = {"代码", "策略ID", "综合评分", "入场门控来源", "生产决策允许入场", "策略执行"}
                if not required.issubset(frame.columns) or not frame["入场门控来源"].eq("production_decision_v1").all():
                    raise ValueError(f"{day} 不是修复后生产决策计划，不能冻结旧回放为可信基准")
                if not frame["策略执行"].map(lambda text: bool(json.loads(text))).all():
                    raise ValueError(f"{day} 缺少冻结执行配置")
            frames[day] = frame
        base = replace(config or BacktestConfig(), account_position_pct=0.34, max_position_per_stock=0.34,
                       max_positions=3, max_total_position=1.0, max_sector_concentration=1.0,
                       entry_mode="hybrid", exit_minute_data_policy="cache_only", use_simulated_prices=False,
                       replay_missing_evidence_policy=spec.missing_evidence_policy, exit_policy_mode="strategy", risk_control=True)
        frozen_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="microseconds")
        ident = stable_id(VERSION, frozen_at, start, end)
        directory = self._directory(ident)
        directory.mkdir(parents=True)
        manifest = {"experiment_id": ident, "kind": "replay_research", "version": VERSION,
                    "frozen_at": frozen_at, "start": start, "end": end, "dates": dates,
                    "calendar": sorted(set(dates + list(plan_dates.values()) + history_dates)), "spec": asdict(spec),
                    "base_config": asdict(base), "profiles": profile_snapshot or {},
                    "classification": "historical_research", "regimes": regimes or {},
                    "sector_peers": {}, "files": {}, "status": "freezing", "arms": arms,
                    "production_changes": False, "release_allowed": False}
        if spec.suite != "legacy":
            manifest["classification"] = "exploratory_confirmation_quality"
            manifest["risk_history_dates"] = history_dates
            manifest["branch_selection"] = "持续确认分支预先固定；不以回踩实验收益选择后续基准"
            if spec.suite == "position_risk":
                manifest["classification"] = "exploratory_reused_history_sizing_comparison"
                manifest["branch_selection"] = "共同候选、确认、退出及三仓上限；仅改变压力仓位和历史风险档位"
                manifest["unseen_validation_required"] = True
                manifest["tier_calibration"] = "同策略/候选日阶段，前30交易日成熟基准成交，隔离5交易日；不足50笔轻仓"
            if spec.suite == "return_refinement":
                manifest["classification"] = "exploratory_reused_history_return_refinement"
                manifest["hypothesis_origin"] = "20261011_prior_eight_arm_diagnosis; history_already_examined"
                manifest["branch_selection"] = "三项独立假设；禁止按全区间收益组合成生产参数"
                manifest["unseen_validation_required"] = True
            if spec.suite == "in_sample_tuning":
                manifest["classification"] = "in_sample_fitted_parameter_search"
                manifest["hypothesis_origin"] = "20261011_user_authorized_profit_search; history_already_examined"
                manifest["branch_selection"] = "按已查看全区间净收益选择，仅为样本内拟合；不得标为独立样本外检验"
                manifest["unseen_validation_required"] = True
                manifest["search_policy"] = {"registered_variants": len(arms) - 1,
                    "objective": "highest_complete_full_history_net_return",
                    "minimum_closed_trades": 10, "maximum_drawdown_pct": 5,
                    "positive_return_required": True, "automatic_production_release": False}
                manifest["resolved_arm_parameters"] = {arm: asdict(OptimizationParameters(**{
                    **spec.optimization, **settings.get("parameter_overrides", {})})) for arm, settings in arms.items()}
        manifest["implementation_hash"] = implementation_version()
        manifest["configuration_hash"] = digest({"base_config": asdict(base), "spec": asdict(spec), "profiles": profile_snapshot or {}})
        self._publish(manifest)
        first_seen = {}
        for date in dates:
            frame = frames[plan_dates[date]]
            sectors = {str(r["代码"]).zfill(6): set(normalize_sector_names(r.get("共振板块") or r.get("所属板块")))
                       for r in frame.to_dict("records")}
            peers = BacktestEngine._load_sector_peer_codes(date, sectors, duckdb_path=factor_db_path)
            manifest["sector_peers"][date] = peers
            for code in set(sectors) | {p for values in peers.values() for p in values}:
                first_seen.setdefault(code, date)
        profile = MinuteAmountProfileRepository(amount_profile_path).load()
        # A model trained after the first research date cannot enter historical confirmations.
        trained = str(profile.get("trained_at") or "")[:10].replace("-", "")
        if amount_profile_path is not None:
            stock_dir = getattr(data_manager, "stock_dir", None)
            if stock_dir is None:
                raise ValueError("历史成交进度模型缺少原始缓存目录")
            validate_historical_profile(profile, Path(stock_dir) / "tick", start)
            manifest["amount_profile_scope"] = "reconstructed_from_prior_complete_minutes"
            manifest["amount_profile_data_end"] = profile["historical_training"]["end"]
            training_dir = directory / "amount_profile_sources"
            training_dir.mkdir()
            for source in profile["historical_training"]["sources"]:
                raw = (Path(stock_dir) / "tick" / source["file"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                    raise ValueError("历史模型训练源在冻结期间改变")
                (training_dir / source["file"]).write_bytes(raw)
        elif profile and (not trained or trained >= start):
            manifest["amount_profile_warning"] = "成交进度模型不具备区间前可用证明，冻结为空；不以未来训练结果补历史证据"
            profile = {}
        atomic_json(directory / "amount_profile.json", profile)
        with sqlite3.connect(directory / "evidence.sqlite") as conn:
            conn.execute("CREATE TABLE evidence(kind TEXT, code TEXT, date TEXT, payload TEXT, PRIMARY KEY(kind,code,date))")
            evidence_hash = hashlib.sha256()
            all_dates = manifest["calendar"]
            for index, date in enumerate(all_dates):
                if progress:
                    progress({"stage": "冻结行情证据", "completed": index, "total": len(all_dates), "trade_date": date})
                daily_map = None
                stock_dir = getattr(data_manager, "stock_dir", None)
                if stock_dir is not None:
                    partition = Path(stock_dir) / "all_daily" / f"{date}.csv"
                    daily_map = {}
                    if partition.exists():
                        whole = pd.read_csv(partition, dtype={"ts_code": str, "code": str})
                        code_col = next((c for c in ("ts_code", "code", "股票代码", "代码") if c in whole), None)
                        if code_col:
                            daily_map = {str(r[code_col]).split(".")[0].zfill(6): r for r in whole.to_dict("records")}
                for code, first in sorted(first_seen.items()):
                    history_only = date < calendar.prev(first)
                    if history_only and spec.suite == "legacy":
                        continue
                    canonical = StockCodeUtils.standardize_code(code)
                    if history_only:
                        minutes, auction = None, {}
                    elif stock_dir is None:
                        minutes = data_manager.get_stock_tick(canonical, date)
                        auction = data_manager.get_auction_data(canonical, date)
                    else:
                        tick = Path(stock_dir) / "tick" / f"{canonical}_{date}.csv"
                        minutes = pd.read_csv(tick) if tick.exists() else None
                        file = Path(stock_dir) / "auction" / f"{canonical}_{date}.json"
                        auction = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
                        if auction.get("数据源") not in {"eltdx", "eltdx_0925", "eltdx_open", "minute_tick"}:
                            auction = {}
                    from core.data.data_manager_suspension import suspension_status
                    daily = data_manager.get_stock_daily_data(canonical, date) if daily_map is None else daily_map.get(code, {})
                    values = {"daily": daily} if history_only else {"daily": daily,
                              "minutes": minutes.to_dict("records") if minutes is not None else [], "auction": auction,
                              "suspension": suspension_status(data_manager, code, date, allow_remote=False)}
                    for kind, value in values.items():
                        payload = json.dumps(clean(value), ensure_ascii=False, sort_keys=True, allow_nan=False)
                        conn.execute("INSERT INTO evidence VALUES(?,?,?,?)", (kind, code, date, payload))
                        evidence_hash.update(json.dumps([kind, code, date, payload], ensure_ascii=False).encode())
            manifest["evidence_hash"] = evidence_hash.hexdigest()
        for day, frame in frames.items():
            for arm in arms:
                target = directory / "plans" / arm / f"交易计划_{day}.csv"
                target.parent.mkdir(parents=True, exist_ok=True)
                selected = select_plans(frame, spec.selection_fraction) if arm == "selection" else frame
                selected.to_csv(target, index=False, encoding="utf-8-sig")
        for file in sorted(directory.rglob("*")):
            if file.is_file() and file.name not in {"report.json", "manifest.json"}:
                manifest["files"][file.relative_to(directory).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        manifest["data_hash"] = digest(manifest["files"])
        manifest["status"] = "frozen"
        manifest["contract_hash"] = digest(manifest)
        atomic_json(directory / "manifest.json", manifest)
        self._publish(manifest)
        return manifest

    def verify(self, ident: str) -> dict:
        directory = self._directory(ident)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        actual_contract = digest({k: v for k, v in manifest.items() if k != "contract_hash"})
        saved_contract = (self.ledger.experiment(ident).get("report") or {}).get("contract_hash")
        if actual_contract != manifest.get("contract_hash") or saved_contract != actual_contract:
            raise ValueError("冻结合同被修改")
        if manifest["implementation_hash"] != implementation_version():
            raise ValueError("执行代码已变化，旧实验不能继续；须重新冻结")
        if manifest["data_hash"] != digest(manifest["files"]):
            raise ValueError("冻结清单被修改")
        for name, expected in manifest["files"].items():
            file = directory / name
            if not file.resolve().is_relative_to(directory.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                raise ValueError(f"冻结数据或配置被修改：{name}")
        expected_config = digest({"base_config": manifest["base_config"], "spec": manifest["spec"], "profiles": manifest["profiles"]})
        if expected_config != manifest["configuration_hash"]:
            raise ValueError("冻结参数被修改")
        return manifest

    def run(self, ident: str, *, progress: Callable | None = None) -> dict:
        with experiment_lock(self._directory(ident) / ".run.lock"):
            return self._run_locked(ident, progress=progress)

    def _run_locked(self, ident: str, *, progress: Callable | None = None) -> dict:
        manifest = self.verify(ident)
        directory = self._directory(ident)
        spec = ResearchSpec(**manifest["spec"])
        report = {**manifest, "status": "running", "results": {}, "folds": [], "release_allowed": False}
        self._publish(report)
        arms = manifest["arms"]
        for arm in arms:
            config = BacktestConfig(**manifest["base_config"])
            engine = research_engine(FrozenEvidence(directory), config, manifest, arm=arm)
            state_file = directory / "states" / f"{arm}.json"
            if state_file.exists():
                checkpoint = json.loads(state_file.read_text(encoding="utf-8"))
                if checkpoint.get("contract_hash") != manifest["contract_hash"]:
                    raise ValueError("研究检查点不属于本冻结合同")
                if checkpoint.get("state_hash") != digest(checkpoint["state"]):
                    raise ValueError("研究账户检查点被修改或损坏")
                engine.import_state(checkpoint["state"])
                engine._candidate_evidence_samples = {tuple(row) for row in checkpoint.get("samples", [])}
                engine._candidate_evidence_skips = {(row["trade_date"], row["code"], row["evidence"]): row for row in checkpoint.get("skips", [])}
            completed = str(engine.daily_nav[-1]["date"]) if engine.daily_nav else ""
            blocked = []
            for index, date in enumerate(manifest["dates"]):
                if date <= completed:
                    continue
                snapshot = engine.export_state()
                step = {"stage": arms[arm]["name"], "arm": arm, "completed": index,
                        "total": len(manifest["dates"]), "trade_date": date}
                report["progress"] = step
                self._publish(report)
                if progress:
                    progress(step)
                try:
                    # Daily OHLC is necessary for opening and end-of-day valuation.
                    for code in engine.current_positions:
                        suspended = engine.dm.get_suspension_status(code, date).get("state") == "full_day"
                        daily = engine.dm.get_stock_daily_data(code, date)
                        if not suspended and not float(daily.get("close") or 0) > 0:
                            raise ReplayEvidenceBlocked([{"trade_date": date, "code": code,
                                "evidence": "daily_bar", "reason": "冻结包缺少当日日线，不能计算完整净值"}], {})
                    engine.run_one_day(date, str(directory / "plans" / arm))
                    committed = engine.export_state()
                    atomic_json(state_file, {"contract_hash": manifest["contract_hash"], "state": committed, "state_hash": digest(committed),
                        "samples": sorted(engine._candidate_evidence_samples), "skips": list(engine._candidate_evidence_skips.values())})
                except ReplayEvidenceBlocked as exc:
                    engine.import_state(snapshot)
                    blocked = exc.details
                    break
            state = engine.export_state()
            quality = engine.replay_data_quality()
            complete = len(state["daily_nav"]) == len(manifest["dates"]) and not blocked and not quality.get("skipped_candidate_count")
            report["results"][arm] = {"name": arms[arm]["name"], "changed_variable": arms[arm]["change"],
                "configuration": asdict(config), "sizing_policy": "fixed_34pct" if arm == "baseline" else (
                    "overnight_stress_budget" if spec.suite in {"return_refinement", "in_sample_tuning"} or (spec.suite == "confirmation_quality"
                    and arm in {"overnight_risk", "strategy_quality", "exit_failure"}) else "fixed_risk"),
                "risk_per_trade": None if arm == "baseline" else spec.fixed_risk_per_trade,
                "resolved_parameters": asdict(engine.parameters) if hasattr(engine, "parameters") else {},
                "complete": complete, "blocked": blocked,
                "data_quality": quality, "summary": account_metrics(state, config.initial_capital),
                "nav": state["daily_nav"], "state_path": state_file.relative_to(directory).as_posix(),
                "metrics": grouped_metrics(state, manifest["regimes"], start=manifest["start"], end=manifest["end"])}
            if spec.suite == "position_risk":
                from core.operations.sizing_research import tail_metrics
                report["results"][arm]["sizing_policy"] = "fixed_34pct" if arm == "baseline" else arm + "_stress_budget"
                report["results"][arm]["tail_metrics"] = tail_metrics(state, config.initial_capital)
            self._publish(report)
        report["folds"] = self._rolling(report, directory)
        if spec.suite == "position_risk":
            from core.operations.sizing_research import paired_comparisons
            report["paired_comparisons"] = paired_comparisons(report)
        elif spec.suite != "legacy":
            from core.operations.strategy_optimization import paired_comparisons
            report["paired_comparisons"] = paired_comparisons(report)
        report["status"] = "complete" if all(r["complete"] for r in report["results"].values()) else "incomplete"
        report["recommendation"] = {"status": "research_only", "reason": "历史回填和小样本不自动授权生产权重；须前瞻验证", "production_changes": False}
        enough_folds = len(report["folds"]) >= spec.min_validation_folds
        for arm, row in report["results"].items():
            oos_closed = sum(f["validation"][arm]["account"]["closed_trades"] for f in report["folds"])
            row["validation_closed"] = oos_closed
            row["evidence_status"] = ("historical_comparison_only" if row["complete"] and enough_folds
                                      and oos_closed >= spec.min_validation_closed else "insufficient_evidence")
        report["finished_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        self._publish(report)
        return report

    def _rolling(self, report: dict, directory: Path) -> list[dict]:
        spec = ResearchSpec(**report["spec"])
        dates = report["dates"]
        states = {arm: json.loads((directory / row["state_path"]).read_text(encoding="utf-8"))["state"]
                  if (directory / row["state_path"]).exists() else {} for arm, row in report["results"].items()}
        folds = []
        for offset in range(spec.train_days + spec.embargo_days, len(dates), spec.validation_days):
            valid = dates[offset:offset + spec.validation_days]
            if len(valid) != spec.validation_days:
                break
            train = dates[offset - spec.embargo_days - spec.train_days:offset - spec.embargo_days]
            training = {arm: account_metrics(state, report["base_config"]["initial_capital"], start=train[0], end=train[-1])
                        for arm, state in states.items()}
            eligible = [arm for arm, metric in training.items() if metric["closed_trades"] >= spec.min_train_closed
                        and metric["days"] == len(train)
                        and not any(train[0] <= row["trade_date"] <= train[-1]
                                    for row in report["results"][arm]["data_quality"].get("skipped_candidates", []))]
            if spec.suite != "legacy":
                eligible = [arm for arm in eligible if training[arm]["return_pct"] > 0
                            and (training[arm]["profit_factor"] or 0) > 1]
            # Objective fixed before execution; no validation performance enters selection.
            selected = max(eligible, key=lambda a: (training[a]["return_pct"] - training[a]["max_drawdown_pct"], a)) if eligible else None
            validation = {arm: {"account": account_metrics(state, report["base_config"]["initial_capital"],
                                                         start=valid[0], end=valid[-1]),
                                "groups": grouped_metrics(state, report["regimes"], start=valid[0], end=valid[-1])}
                          for arm, state in states.items()}
            folds.append({"train_start": train[0], "train_end": train[-1], "validation_start": valid[0],
                          "validation_end": valid[-1], "embargo_days": spec.embargo_days,
                          "classification": "retrospective_walk_forward", "selected_arm": selected,
                          "selection_status": "selected_from_prior_matured_results" if selected else (
                              "no_positive_training_edge_or_insufficient_samples" if spec.suite != "legacy"
                              else "insufficient_training_samples"),
                          "training": training, "validation": validation,
                          "account_scope": "continuous_independent_arm_accounts; not a stitched switchable account"})
        return folds

    def prepare_current(self, *, start: str, end: str, spec: ResearchSpec | None = None,
                        initial_capital: float = 1_000_000, progress: Callable | None = None,
                        amount_profile_path: Path | None = None) -> dict:
        """Generate new production plans privately, then seal local evidence once."""
        import tempfile

        from backtest.plan_source import build_backtest_plan_dir
        from config.settings import CACHE_DIR, SNAPSHOT_DIR
        from core.data.data_manager_main import DataManager
        from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS, StrategyProfileRepository
        from risk.risk_config import RiskConfig

        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("初始资金必须为正数")
        calendar = TradeCalendar()
        dates = calendar.get_trade_dates(start, end)
        missing = [calendar.prev(d) for d in dates if not (Path(SNAPSHOT_DIR) / f"{calendar.prev(d)}.json").exists()]
        if missing:
            raise ValueError(f"缺少候选日快照：{','.join(missing[:8])}；先补跑盘后数据")
        coverage = []
        for date in dates:
            day = calendar.prev(date)
            for strategy in PRODUCTION_STRATEGY_IDS:
                source = Path(WEB_DATA_DIR) / "screening/combinations" / strategy / f"screening_{day}.json"
                if not source.exists():
                    raise ValueError(f"{day} 缺少 {strategy} 策略产物，不能当作零候选")
                payload = json.loads(source.read_text(encoding="utf-8"))
                if payload.get("trade_date") != day or payload.get("ok") is not True or not isinstance(payload.get("final"), list):
                    raise ValueError(f"{day} / {strategy} 筛选产物失败或日期不一致，不能当作零候选")
            coverage.append(day)
        regimes = {}
        for date in dates:
            pool_file = Path(WEB_DATA_DIR) / "screening/decision_pool" / f"decision_pool_{calendar.prev(date)}.json"
            pool = json.loads(pool_file.read_text(encoding="utf-8")) if pool_file.exists() else {}
            market = str(pool.get("regime_label") or pool.get("regime") or "unknown")
            emotion = str(pool.get("emotion_phase_label") or pool.get("emotion_phase") or "unknown")
            regimes[date] = f"{market} / {emotion}"
        profiles = StrategyProfileRepository().frozen()
        profile_snapshot = {key: profiles.get_profile(key) for key in PRODUCTION_STRATEGY_IDS}
        config = BacktestConfig.from_risk_config(RiskConfig.load(), initial_capital=initial_capital, risk_control=True)
        config.rotation_enabled = True
        dm = DataManager("", CACHE_DIR, allow_remote_history=False)
        if progress:
            progress({"stage": "准备修复后生产计划", "completed": 0, "total": len(dates), "trade_date": start})
        with tempfile.TemporaryDirectory(prefix="replay-research-plans-") as temporary:
            plan_dir, _, _ = build_backtest_plan_dir(snapshot_dir=Path(SNAPSHOT_DIR), output_dir=Path(temporary),
                screening_dir=Path(WEB_DATA_DIR) / "screening", start_date=start, end_date=end,
                max_rank=0, strategy_ids=PRODUCTION_STRATEGY_IDS)
            after = StrategyProfileRepository().frozen()
            if digest(profile_snapshot) != digest({key: after.get_profile(key) for key in PRODUCTION_STRATEGY_IDS}):
                raise ValueError("生成候选期间配置发生变化，请重新冻结")
            return self.freeze(plans_dir=plan_dir, start=start, end=end, data_manager=dm, calendar=calendar,
                spec=spec, config=config, coverage_dates=coverage, regimes=regimes,
                profile_snapshot=profile_snapshot, progress=progress, amount_profile_path=amount_profile_path)
