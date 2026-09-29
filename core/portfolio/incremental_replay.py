"""Day-by-day paper replay with an account-owned, transactional checkpoint."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import normalize_minute_bars
from backtest.plan_source import build_backtest_plan_dir
from backtest.trade_calendar import TradeCalendar
from config.settings import (
    CACHE_DIR, PAPER_INITIAL_CAPITAL, PAPER_MAX_POSITIONS, PAPER_POSITION_PCT,
    PAPER_ROTATION_MIN_EDGE, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR,
)
from core.data.data_manager_main import DataManager
from core.portfolio.holding_repository import HoldingRepository
from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS
from core.realtime.models import normalize_stock_code
from risk.risk_config import RiskConfig


class ReplayBlocked(ValueError):
    def __init__(self, trade_date: str, reason: str):
        super().__init__(reason)
        self.trade_date = trade_date
        self.reason = reason


class IncrementalPaperReplay:
    """Keep the normal paper account; use a separate account for historical rebuilds."""

    def __init__(self, repository: HoldingRepository | None = None, *,
                 account_key: str = "default", data_manager: Any = None,
                 calendar: TradeCalendar | None = None) -> None:
        self.repository = repository or HoldingRepository()
        self.account_key = account_key
        self.calendar = calendar or TradeCalendar()
        self.dm = data_manager
        self._prefetch_dm = data_manager
        self.repository.ensure_account(account_key, name="模拟交易账户",
                                       initial_capital=PAPER_INITIAL_CAPITAL)

    @staticmethod
    def config_version() -> str:
        root = Path(__file__).resolve().parents[2]
        digest = hashlib.sha256()
        for relative in (
            "config/strategy_combinations.yaml", "config/risk_control.yaml",
            "backtest/backtest_engine.py", "backtest/minute_entry.py",
            "backtest/exit_policy.py", "backtest/plan_source.py",
        ):
            path = root / relative
            digest.update(relative.encode())
            digest.update(path.read_bytes() if path.exists() else b"missing")
        digest.update(json.dumps({
            "strategies": list(PRODUCTION_STRATEGY_IDS),
            "position_pct": PAPER_POSITION_PCT,
            "max_positions": PAPER_MAX_POSITIONS,
            "rotation_edge": PAPER_ROTATION_MIN_EDGE,
            "entry_mode": "hybrid", "sizing": "fixed_risk", "exit": "strategy",
        }, sort_keys=True).encode())
        return digest.hexdigest()[:16]

    def checkpoint(self) -> dict[str, Any]:
        with self.repository._connect() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_replay_checkpoints WHERE account_key=?",
                (self.account_key,),
            ).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json") or "{}")
        return result

    def _account_snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]], int, str]:
        with self.repository._connect() as conn:
            account = conn.execute(
                "SELECT * FROM portfolio_accounts WHERE account_key=?", (self.account_key,)
            ).fetchone()
            positions = conn.execute(
                "SELECT * FROM portfolio_positions WHERE account_key=? AND status='open'",
                (self.account_key,),
            ).fetchall()
            trade = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS id, COALESCE(MAX(trade_date),'') AS trade_date "
                "FROM portfolio_trades WHERE account_key=?", (self.account_key,),
            ).fetchone()
        return (dict(account), [self.repository._position_row(row) for row in positions],
                int(trade["id"]), str(trade["trade_date"]))

    @staticmethod
    def _engine_position(position: dict[str, Any]) -> dict[str, Any]:
        entry = float(position["entry_price"])
        meta = dict(position.get("metadata") or {})
        return {
            "stock_name": position["name"], "entry_date": position["entry_date"],
            "entry_time": position.get("entry_time") or "",
            "entry_price": entry, "shares": int(position["shares"]),
            "cost_basis": float(position["cost_amount"]),
            "market_value": float(position["last_price"] or entry) * int(position["shares"]),
            "last_close": float(position["last_price"] or entry),
            "highest_price": float(position["high_watermark"] or entry),
            "max_favorable_price": float(position["high_watermark"] or entry),
            "min_adverse_price": entry,
            "stop_loss_price": float(position["structural_stop"] or
                                     entry * (1 - float(position["emergency_loss_pct"] or 6) / 100)),
            "pattern_type": position.get("strategy_name") or "手工持仓",
            "hot_resonance": False, "resonance_sectors": position.get("sector_names") or "",
            "strategy_id": position.get("strategy_id") or "default",
            "strategy_name": position.get("strategy_name") or "",
            "strategy_execution": meta.get("strategy_execution") or {},
            "strategy_version": meta.get("strategy_version") or "",
            "strategy_sources": position.get("strategy_id") or "manual",
            "plan_score": float(meta.get("entry_strength_score") or 0),
        }

    def _config(self, capital: float) -> BacktestConfig:
        config = BacktestConfig.from_risk_config(
            RiskConfig.load(), initial_capital=capital, risk_control=True)
        config.max_positions = min(int(PAPER_MAX_POSITIONS), 3)
        config.max_position_per_stock = float(PAPER_POSITION_PCT) / 100
        config.max_total_position = 1.0
        config.max_sector_concentration = 1.0
        config.account_position_pct = float(PAPER_POSITION_PCT) / 100
        config.rotation_enabled = True
        config.rotation_min_edge = float(PAPER_ROTATION_MIN_EDGE)
        config.entry_mode = "hybrid"
        config.position_sizing_mode = "fixed_risk"
        config.exit_policy_mode = "strategy"
        # Required minute evidence is checked before processing the day.
        config.exit_minute_data_policy = "cache_only"
        return config

    def _block(self, date: str, reason: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.repository._write() as conn:
            conn.execute(
                "INSERT INTO portfolio_replay_checkpoints "
                "(account_key, blocked_date, blocked_reason, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account_key) DO UPDATE SET blocked_date=excluded.blocked_date, "
                "blocked_reason=excluded.blocked_reason, updated_at=excluded.updated_at",
                (self.account_key, date, reason, now),
            )
        raise ReplayBlocked(date, reason)

    def _plan(self, prev_date: str) -> tuple[Path, list[str]]:
        snapshot = Path(SNAPSHOT_DIR) / f"{prev_date}.json"
        if not snapshot.exists():
            self._block(self.calendar.next(prev_date), f"缺少候选日 {prev_date} 页面快照")
        output = Path(WEB_DATA_DIR) / "portfolio_replay_plans" / self.account_key
        plan_dir, _, _ = build_backtest_plan_dir(
            snapshot_dir=Path(SNAPSHOT_DIR), output_dir=output,
            screening_dir=Path(WEB_DATA_DIR) / "screening",
            start_date=prev_date, end_date=prev_date,
            strategy_ids=PRODUCTION_STRATEGY_IDS,
        )
        path = plan_dir / f"交易计划_{prev_date}.csv"
        if not path.exists():
            # A verified empty production pool is a valid no-buy day.
            decision = Path(WEB_DATA_DIR) / "screening" / "decision_pool" / f"decision_pool_{prev_date}.json"
            if not decision.exists():
                self._block(self.calendar.next(prev_date), f"缺少候选日 {prev_date} 决策池，不能按空候选继续")
            return plan_dir, []
        frame = pd.read_csv(path, dtype={"代码": str})
        return plan_dir, [str(code).zfill(6) for code in frame.get("代码", [])]

    def _required_minutes(self, date: str, codes: list[str]) -> list[str]:
        missing = []
        if self._prefetch_dm is None:
            self._prefetch_dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
        dm = self._prefetch_dm
        for code in sorted(set(codes)):
            ts_code = normalize_stock_code(code, add_suffix=True)
            try:
                raw = dm.get_stock_tick(ts_code, date)
                if raw is not None and not raw.empty:
                    date_column = next((name for name in ("trade_date", "date", "datetime")
                                        if name in raw.columns), "")
                    if date_column:
                        observed = (raw[date_column].astype(str).str[:10]
                                    .str.replace("-", "", regex=False).str[:8])
                        if not observed.eq(date).all():
                            missing.append(code)
                            continue
                bars = normalize_minute_bars(raw)
                if len(bars) < 2 or str(bars.iloc[-1]["time"]) < "14:55:00":
                    missing.append(code)
            except Exception:
                missing.append(code)
        return missing

    @staticmethod
    def _sector_peer_codes(date: str, prev_date: str, plan_dir: Path) -> list[str]:
        path = plan_dir / f"交易计划_{prev_date}.csv"
        if not path.exists():
            return []
        plans = pd.read_csv(path, dtype={"代码": str})
        sector_map: dict[str, set[str]] = {}
        for _, plan in plans.iterrows():
            code = str(plan.get("代码") or "").zfill(6)
            raw = plan.get("共振板块")
            if pd.isna(raw) or not str(raw).strip():
                raw = plan.get("所属板块")
            if not code or pd.isna(raw):
                continue
            sector_map[code] = {
                part.strip() for part in str(raw).replace("；", ",").replace("，", ",").split(",")
                if part.strip()
            }
        peers = BacktestEngine._load_sector_peer_codes(date, sector_map)
        return sorted({str(peer).zfill(6) for rows in peers.values() for peer in rows})

    def _seed_engine(self, checkpoint: dict[str, Any], account: dict[str, Any],
                     positions: list[dict[str, Any]]) -> BacktestEngine:
        dm = self.dm or DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
        engine = BacktestEngine(dm, self._config(float(account["initial_capital"])))
        if checkpoint.get("state"):
            engine.import_state(checkpoint["state"])
        engine.cash = float(account["cash"])
        saved = dict(engine.current_positions)
        engine.current_positions = {}
        for position in positions:
            code = position["code"]
            previous = saved.get(code) or {}
            same_lot = (str(previous.get("entry_date")) == str(position["entry_date"])
                        and int(previous.get("shares") or 0) == int(position["shares"]))
            engine.current_positions[code] = (
                {**previous, "market_value": float(position["last_price"] or position["entry_price"])
                 * int(position["shares"]), "last_close": float(position["last_price"] or position["entry_price"])}
                if same_lot else self._engine_position(position)
            )
        engine.total_capital = engine.cash + sum(
            p["market_value"] for p in engine.current_positions.values())
        return engine

    def _commit_day(self, date: str, version: str, engine: BacktestEngine,
                    new_trades: list[Any], expected_trade_id: int) -> list[str]:
        from core.operations.ledger import stable_id

        fill_keys = []
        now = datetime.now().isoformat(timespec="seconds")
        with self.repository._write() as conn:
            actual = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM portfolio_trades WHERE account_key=?",
                (self.account_key,),
            ).fetchone()[0])
            if actual != expected_trade_id:
                raise ReplayBlocked(date, "回放期间账户有新成交；请重新启动接力，避免覆盖手工交易")
            if conn.execute(
                "SELECT 1 FROM portfolio_replay_days WHERE account_key=? AND trade_date=?",
                (self.account_key, date),
            ).fetchone():
                raise ReplayBlocked(date, "该交易日已提交，不允许重复成交")
            for index, trade in enumerate(new_trades):
                row = asdict(trade)
                code = str(row["stock_code"]).zfill(6)
                action = str(row["action"]).lower()
                key = stable_id("incremental_replay", self.account_key, date, index,
                                code, action, version)
                if conn.execute("SELECT 1 FROM portfolio_executions WHERE event_key=?", (key,)).fetchone():
                    raise ReplayBlocked(date, f"成交幂等键重复：{key}")
                if action == "buy":
                    price = float(row["entry_price"])
                    shares = int(row["shares"])
                    engine_position = engine.current_positions.get(code) or {}
                    self.repository.open_position({
                        "code": code, "name": row["stock_name"], "entry_date": date,
                        "entry_time": row.get("entry_time") or "",
                        "entry_price": price, "shares": shares,
                        "fees": max(float(row["position_size"]) - price * shares, 0),
                        "strategy_id": row.get("strategy_id") or "default",
                        "strategy_name": row.get("strategy_name") or "",
                        "sector_names": row.get("resonance_sectors") or "",
                        "source": "auto_replay", "reason": row.get("entry_signal") or "分钟确认",
                        "metadata": {"replay_fill_key": key, "config_version": version,
                                     "execution_price_basis": "historical_minute_estimate",
                                     "strategy_execution": engine_position.get("strategy_execution") or {},
                                     "strategy_version": engine_position.get("strategy_version") or "",
                                     "entry_strength_score": engine_position.get("plan_score") or 0},
                    }, self.account_key, _connection=conn)
                elif action == "sell":
                    position = conn.execute(
                        "SELECT id FROM portfolio_positions WHERE account_key=? AND code=? AND status='open'",
                        (self.account_key, code),
                    ).fetchone()
                    if not position:
                        raise ReplayBlocked(date, f"卖出 {code} 时持仓不存在")
                    price = float(row["exit_price"])
                    shares = int(row["shares"])
                    fees = max(price * shares - float(row["position_size"]) - float(row["pnl"]), 0)
                    self.repository.sell_position(int(position["id"]), {
                        "trade_date": date, "trade_time": row.get("exit_time") or "",
                        "price": price, "shares": shares, "fees": fees,
                        "reason": row.get("exit_reason") or "回放退出", "source": "auto_replay",
                        "execution_key": key,
                        "metadata": {"replay_fill_key": key, "config_version": version},
                    }, _connection=conn)
                else:
                    raise ReplayBlocked(date, f"未知成交方向：{action}")
                conn.execute(
                    "INSERT OR IGNORE INTO portfolio_executions VALUES (?, ?, ?, ?)",
                    (key, self.account_key, json.dumps(row, ensure_ascii=False, default=str), now),
                )
                fill_keys.append(key)
            cash = float(conn.execute(
                "SELECT cash FROM portfolio_accounts WHERE account_key=?", (self.account_key,)
            ).fetchone()[0])
            if abs(cash - engine.cash) > .02:
                raise ReplayBlocked(date, f"账户现金与回放引擎不一致：{cash:.2f} != {engine.cash:.2f}")
            for code, state in engine.current_positions.items():
                conn.execute(
                    "UPDATE portfolio_positions SET last_price=?, high_watermark=?, updated_at=? "
                    "WHERE account_key=? AND code=? AND status='open'",
                    (float(state.get("last_close") or state["entry_price"]),
                     float(state.get("highest_price") or state["entry_price"]), now,
                     self.account_key, code),
                )
            trade_id = int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM portfolio_trades WHERE account_key=?",
                (self.account_key,),
            ).fetchone()[0])
            state = engine.export_state()
            conn.execute(
                "INSERT INTO portfolio_replay_days VALUES (?, ?, ?, ?, ?)",
                (self.account_key, date, version, json.dumps(fill_keys), now),
            )
            conn.execute(
                "INSERT INTO portfolio_replay_checkpoints "
                "(account_key,last_completed_date,config_version,state_json,account_trade_id,"
                "blocked_date,blocked_reason,updated_at) VALUES (?,?,?,?,?,'','',?) "
                "ON CONFLICT(account_key) DO UPDATE SET last_completed_date=excluded.last_completed_date, "
                "config_version=excluded.config_version, state_json=excluded.state_json, "
                "account_trade_id=excluded.account_trade_id, blocked_date='',blocked_reason='',"
                "updated_at=excluded.updated_at",
                (self.account_key, date, version, json.dumps(state, ensure_ascii=False, default=str),
                 trade_id, now),
            )
        return fill_keys

    def run(self, start_date: str, end_date: str, *,
            progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if not self.calendar.is_real:
            raise ValueError("真实交易日历不可用，拒绝用工作日近似进行接力回放")
        checkpoint = self.checkpoint()
        version = self.config_version()
        if checkpoint.get("config_version") and checkpoint["config_version"] != version:
            raise ValueError("策略配置版本已变化；当前账户不能按不同口径继续接力，请使用独立历史重建账户")
        account, positions, trade_id, last_trade_date = self._account_snapshot()
        completed = str(checkpoint.get("last_completed_date") or "")
        anchor = completed or last_trade_date
        first = self.calendar.next(anchor) if anchor else start_date
        if not anchor and not start_date:
            raise ValueError("空账户首次接力必须提供开始日期")
        if start_date and first < start_date:
            raise ValueError(f"必须先从 {first} 补跑；不能跳过未完成的交易日")
        if completed and trade_id != int(checkpoint.get("account_trade_id") or 0):
            with self.repository._connect() as conn:
                fresh = conn.execute(
                    "SELECT trade_date, source FROM portfolio_trades WHERE account_key=? AND id>? ORDER BY id",
                    (self.account_key, int(checkpoint.get("account_trade_id") or 0)),
                ).fetchall()
            if any(str(row["trade_date"]) > completed for row in fresh):
                self._block(first, "检查点之后存在手工/实时成交；其日期晚于待补跑日，不能穿越成交时间")
        dates = self.calendar.get_trade_dates(first, end_date)
        if not dates:
            return {"state": "done", "last_completed_date": completed,
                    "completed_days": 0, "next_trade_date": first, "fill_count": 0}
        engine = self._seed_engine(checkpoint, account, positions)
        count = 0
        fills = 0
        for date in dates:
            prev = self.calendar.prev(date)
            try:
                if self.config_version() != version:
                    self._block(date, "回放期间策略配置已变化，不能混用配置版本")
                plan_dir, candidates = self._plan(prev)
                _, current_positions, expected_trade_id, _ = self._account_snapshot()
                sector_peers = self._sector_peer_codes(date, prev, plan_dir)
                required = candidates + [p["code"] for p in current_positions] + sector_peers
                if progress:
                    progress({"stage": "检查分钟证据", "trade_date": date,
                              "completed_days": count, "required_stocks": len(set(required)),
                              "sector_peers": len(sector_peers)})
                missing = self._required_minutes(date, required)
                if missing:
                    self._block(date, f"缺少 {date} 分钟证据：{', '.join(missing[:20])}")
                missing_daily = [code for code in sorted(set(required))
                                 if not engine._get_stock_daily_bar(code, date)]
                if missing_daily:
                    self._block(date, f"缺少 {date} 日线估值证据：{', '.join(missing_daily[:20])}")
                before = len(engine.trade_history)
                attempts_before = len(engine.entry_attempts)
                engine.run_one_day(date, str(plan_dir))
                insufficient = [row for row in engine.entry_attempts[attempts_before:]
                                if row.get("status") == "data_insufficient"
                                or row.get("reason_code") in {
                                    "missing_minutes", "incomplete_opening_window",
                                    "missing_sector_confirmation",
                                }]
                if insufficient:
                    examples = "; ".join(
                        f"{row.get('stock_code')}: {row.get('reason') or row.get('reason_code')}"
                        for row in insufficient[:5]
                    )
                    self._block(date, f"{date} 入场证据不足：{examples}")
                daily_fills = self._commit_day(date, version, engine,
                                               engine.trade_history[before:], expected_trade_id)
                count += 1
                fills += len(daily_fills)
                if progress:
                    progress({"stage": "逐日提交完成", "trade_date": date,
                              "completed_days": count, "fill_count": fills})
            except ReplayBlocked as exc:
                self._block(date, exc.reason)
            except Exception as exc:
                self._block(date, f"{date} 回放失败：{exc}")
        return {"state": "done", "last_completed_date": dates[-1],
                "completed_days": count, "next_trade_date": self.calendar.next(dates[-1]),
                "fill_count": fills, "config_version": version}


__all__ = ["IncrementalPaperReplay", "ReplayBlocked"]
