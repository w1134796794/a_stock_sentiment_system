"""Independent signal trades and incrementally matured, availability-gated labels."""
from __future__ import annotations

from core.data.minute_cache_files import minute_cache_exists, read_minute_csv, write_minute_csv

import json
import hashlib
import copy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from collections import OrderedDict

import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.minute_entry import ENTRY_FIXED, normalize_minute_bars
from backtest.plan_source import _to_backtest_row
from core.operations.ledger import stable_id
from core.signals.case_features import build_signal_case, clean, event_time
from core.signals.case_repository import SignalCaseRepository
from core.utils.price_limit import limit_down_price, limit_up_price

OUTCOME_VERSION = "standalone_next_minute_v1"


class CaseDataSource:
    """Bounded per-run cache. Offline mode must not call remote minute/auction APIs."""
    def __init__(self, dm, *, fetch_missing: bool = False):
        self.dm = dm
        self.fetch_missing = fetch_missing
        self.frames = OrderedDict()

    def __getattr__(self, name):
        return getattr(self.dm, name)

    def get_stock_tick(self, code: str, date: str):
        key = (code, date)
        if key in self.frames:
            self.frames.move_to_end(key)
            return self.frames[key]
        folder = getattr(self.dm, "stock_dir", None)
        path = Path(folder) / "tick" / f"{code}_{date}.csv" if folder else None
        raw = read_minute_csv(path) if path and minute_cache_exists(path) else pd.DataFrame()
        _, status = validate_minutes(raw, date, full_session=True)
        if status != "complete" and self.fetch_missing:
            if path and minute_cache_exists(path):
                provider = self.dm._get_eltdx_provider()
                replacement = provider.get_minute_bars(code, date) if provider else pd.DataFrame()
                _, refreshed = validate_minutes(replacement, date, full_session=True)
                if refreshed == "complete":
                    write_minute_csv(replacement, path)
                    raw = replacement
            else:
                raw = self.dm.get_stock_tick(code, date)
        self.frames[key] = raw
        while len(self.frames) > 128:
            self.frames.popitem(last=False)
        return raw

    def get_auction_data(self, code, date):
        folder = getattr(self.dm, "stock_dir", None)
        path = Path(folder) / "auction" / f"{code}_{date}.json" if folder else None
        if path and path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("数据源") in {"eltdx", "eltdx_0925", "eltdx_open", "minute_tick"}:
                return value
        return self.dm.get_auction_data(code, date) if self.fetch_missing else {}


def closed_trade_result(fills: list[dict]) -> dict:
    """Aggregate a whole episode. Prices already include slippage; charge fees once."""
    shares = 0.0
    invested = received = fees = 0.0
    for fill in fills:
        quantity, price = float(fill["shares"]), float(fill["price"])
        fee = float(fill.get("commission", 0)) + float(fill.get("tax", 0))
        fees += fee
        if fill["side"] == "buy":
            shares += quantity
            invested += quantity * price + fee
        elif fill["side"] == "sell":
            shares -= quantity
            received += quantity * price - fee
        else:
            raise ValueError("Unknown fill side")
        if shares < -1e-6:
            raise ValueError("Sell exceeds purchased shares")
    if shares > 1e-6 or invested <= 0:
        return {"state": "pending", "remaining_shares": shares}
    net = (received - invested) / invested
    return {"state": "mature", "net_return": net, "success": net > 0,
            "invested": invested, "received": received, "fees": fees}


def validate_minutes(raw: pd.DataFrame, date: str, *, full_session: bool = False) -> tuple[pd.DataFrame, str]:
    if raw is None or raw.empty:
        return pd.DataFrame(), "missing_minutes"
    for column in ("trade_date", "date", "datetime"):
        if column in raw:
            dates = raw[column].astype(str).str[:10].str.replace("-", "", regex=False).str[:8]
            if not dates.eq(date).all():
                return pd.DataFrame(), "wrong_minute_date"
    bars = normalize_minute_bars(raw)
    if bars.empty or len(bars) < 2:
        return pd.DataFrame(), "invalid_minutes"
    if full_session and (len(bars) < 235 or bars.iloc[0]["time"] > "09:31:00" or bars.iloc[-1]["time"] < "14:59:00"):
        return pd.DataFrame(), "incomplete_minute_session"
    return bars, "complete"


class _IndependentEngine(BacktestEngine):
    """Reuse the execution/exit rules without cash, holdings or theme allocation gates."""
    def __init__(self, dm, config, *, price: float, shares: int):
        super().__init__(dm, replace(config, risk_control=False, rotation_enabled=False, account_position_pct=1,
                                     initial_capital=max(1_000_000, price * shares * 2)))
        self.prepared_price = price
        self.unit_shares = shares

    def _check_buy_conditions(self, *_args):
        return True, self.prepared_price

    def _calculate_position_size(self, _plan):
        self._last_sizing_meta = {"method": "independent_legal_unit"}
        return self.prepared_price * self.unit_shares + 0.000001

    def _entry_gap_position_multiplier(self, _gap):
        return 1.0


class SignalCaseBuilder:
    def __init__(self, dm: Any, *, repository: SignalCaseRepository | None = None,
                 config: BacktestConfig | None = None, calendar=None):
        self.dm = dm
        self.repository = repository or SignalCaseRepository()
        if config is None:
            from risk.risk_config import RiskConfig
            config = BacktestConfig.from_risk_config(RiskConfig.load())
        self.config = config
        self.scanner = BacktestEngine(dm, config)
        self.scanner._load_sector_peer_codes = lambda date, sectors: BacktestEngine._load_sector_peer_codes(
            date, sectors, duckdb_path=self.repository.path)
        if calendar is not None:
            self.scanner.calendar = calendar
        self.calendar = self.scanner.calendar

    def discover(self, rows: list[dict], *, candidate_date: str, market_date: str,
                 source_available_at: str = "", data_version: str = "") -> int:
        if not self.calendar.is_real:
            raise ValueError("Signal cases require a real trading calendar")
        if self.config.entry_mode == ENTRY_FIXED:
            raise ValueError("Daily fixed-gap baseline is not a minute-confirmed case")
        plans = [plan for row in rows if (plan := _to_backtest_row(row))]
        frame = pd.DataFrame(plans)
        if frame.empty:
            return 0
        sources = [row for row in rows if _to_backtest_row(row)]
        with self.repository.connect() as con:
            done = {item[0] for item in con.execute(
                "SELECT attempt_id FROM signal_case_attempts WHERE market_date=? AND state<>'blocked'",
                [market_date]).fetchall()}
        selected = []
        for (_, plan), source in zip(frame.iterrows(), sources):
            version = stable_id(OUTCOME_VERSION, asdict(self.config), plan.get("策略版本"), plan.get("策略执行"))
            attempt_id = stable_id("case_attempt", market_date, str(plan["代码"]).zfill(6), version)
            if attempt_id not in done:
                selected.append((plan, source))
        if not selected:
            return 0
        frame = pd.DataFrame([plan.to_dict() for plan, _ in selected])
        scanner = self.scanner
        scanner._day_plans = frame
        scanner._prefetch_entry_data(frame, market_date)
        created = 0
        with self.repository.connect() as con:
            for (_, plan), (_, source) in zip(frame.iterrows(), selected):
                code = str(plan["代码"]).zfill(6)
                config_version = stable_id(OUTCOME_VERSION, asdict(self.config), plan.get("策略版本"),
                                           plan.get("策略执行"))
                attempt_id = stable_id("case_attempt", market_date, code, config_version)
                previous = con.execute("SELECT state FROM signal_case_attempts WHERE attempt_id=?", [attempt_id]).fetchone()
                if previous and previous[0] != "blocked":
                    continue
                try:
                    with self.repository.transaction(con):
                        count = self._discover_one(con, plan, source, candidate_date, market_date,
                                                   config_version, attempt_id, source_available_at, data_version)
                    created += count
                except Exception as exc:
                    con.execute("INSERT OR REPLACE INTO signal_case_attempts VALUES (?, ?, 'blocked', ?)",
                                [attempt_id, market_date, json.dumps({"code": code, "reason": str(exc)})])
                finally:
                    scanner._clear_entry_state(code)
        return created

    def _discover_one(self, con, plan, source, candidate_date, market_date,
                      config_version, attempt_id, source_available_at, data_version):
        scanner = self.scanner
        code = str(plan["代码"]).zfill(6)
        before = len(scanner.entry_attempts)
        can_buy, price = scanner._check_buy_conditions(plan, market_date, code, str(plan["名称"]))
        attempt = scanner.entry_attempts[-1] if len(scanner.entry_attempts) > before else {}
        raw = scanner._minute_frames.get((market_date, code), pd.DataFrame())
        _, minute_status = validate_minutes(raw, market_date)
        data_status = str(attempt.get("data_status") or "")
        blocked = (minute_status != "complete" or data_status not in {"complete", "auction_missing_fallback"}
                   or str(attempt.get("reason_code", "")).startswith("missing_"))
        state = "blocked" if blocked else "filled" if can_buy else "unfilled" if attempt.get("confirm_time") else "no_opportunity"
        con.execute("INSERT OR REPLACE INTO signal_case_attempts VALUES (?, ?, ?, ?)",
                    [attempt_id, market_date, state, json.dumps(clean(attempt), ensure_ascii=False)])
        if blocked or not attempt.get("confirm_time"):
            return 0
        case = build_signal_case({**source, **attempt, "code": code, "strategy_version": config_version,
                                  "factor_values": json.loads(scanner._factor_context_json(plan)),
                                  "strategy_execution": scanner._plan_execution(plan)},
                                 {"candidate_date": candidate_date, "market_date": market_date,
                                  "source_available_at": source_available_at, "data_version": data_version},
                                 minutes=raw, origin="reconstructed")
        created = int(self.repository.record(case, con))
        if not can_buy:
            self.repository.save_progress(case["case_id"], "unfilled", market_date, {"reason": attempt.get("reason")}, con)
            return created
        if con.execute("SELECT 1 FROM signal_case_progress WHERE case_id=?", [case["case_id"]]).fetchone():
            return created
        engine = self._seed(plan, case, price, scanner)
        position = engine.current_positions[code]
        payload = {"engine": engine.export_state(), "config": asdict(self.config),
                   "entry_price": price, "entry_date": market_date, "entry_time": attempt["entry_time"],
                   "cost_version": engine._cost_version(), "exit_rule_version": engine._exit_rule_version(position),
                   "outcome_version": OUTCOME_VERSION, "last_processed": "",
                   "fills": [{"side": "buy", "shares": position["shares"], "price": price,
                              "commission": price * position["shares"] * self.config.commission_rate}]}
        self.repository.save_progress(case["case_id"], "pending", "", payload, con)
        for label in ("net_return", "mfe", "mae", "return_d1", "return_d3", "return_d5"):
            self.repository.save_outcome(case["case_id"], label, {"state": "pending"}, con)
        return created

    def initialize_live(self, through_date: str) -> int:
        initialized = 0
        with self.repository.connect() as con:
            records = con.execute("""SELECT c.payload_json FROM signal_cases c
                LEFT JOIN signal_case_progress p USING(case_id) WHERE (p.case_id IS NULL OR p.state='initialization_blocked')
                AND json_extract_string(c.payload_json, '$.origin')='live' AND c.market_date<=?""",
                [through_date]).fetchall()
            for (text,) in records:
                case = json.loads(text)
                try:
                    with self.repository.transaction(con):
                        count = self._initialize_live_one(case, con)
                    initialized += count
                except Exception as exc:
                    self.repository.save_progress(case["case_id"], "initialization_blocked", "",
                                                  {"reason": str(exc)}, con)
        return initialized

    def _initialize_live_one(self, case, con) -> int:
        if case["status"] == "unfilled":
            self.repository.save_progress(case["case_id"], "unfilled", case["market_date"], {}, con)
            return 0
        cfg = case.get("backtest_config")
        if not cfg or not case.get("strategy_execution"):
            # Do not quietly substitute today's exit configuration.
            self.repository.save_progress(case["case_id"], "initialization_blocked", "",
                                          {"reason": "missing_frozen_execution_config"}, con)
            return 0
        cfg = dict(cfg)
        if cfg.get("exit_policy_mode") == "oos_selected":
            if not case.get("resolved_exit_policy"):
                self.repository.save_progress(case["case_id"], "initialization_blocked", "",
                                              {"reason": "missing_frozen_exit_policy"}, con)
                return 0
            cfg["exit_policy_mode"] = case["resolved_exit_policy"].get("policy") or "strategy"
        config = BacktestConfig(**cfg)
        date, code = case["market_date"], case["code"]
        raw = self.dm.get_stock_tick(self.scanner._standardize_stock_code(code), date)
        bars, reason = validate_minutes(raw, date, full_session=True)
        if reason != "complete":
            self.repository.save_progress(case["case_id"], "initialization_blocked", "", {"reason": reason}, con)
            return 0  # Retried next postclose run.
        daily = self.scanner._get_stock_daily_bar(code, date) or {}
        ceiling = limit_up_price(float(daily.get("pre_close") or 0), code, case["name"])
        if ceiling is None:
            self.repository.save_progress(case["case_id"], "initialization_blocked", "", {"reason": "missing_price_limit"}, con)
            return 0
        confirm_time = case["confirm_at"].split("T")[-1]
        future = bars[bars["time"] > confirm_time]
        # Match the shared entry engine: the next observed minute, not a later cherry-picked dip.
        if future.empty:
            self.repository.save_progress(case["case_id"], "unfilled", date,
                                          {"reason": "no_post_confirmation_minute"}, con)
            return 0
        fill = future.iloc[0]
        price = float(fill["open"]) * (1 + config.slippage)
        if float(fill["volume"]) <= 0 or price >= ceiling:
            self.repository.save_progress(case["case_id"], "unfilled", date,
                                          {"reason": "next_minute_not_tradable"}, con)
            return 0
        features = case["features"]
        plan = pd.Series(_to_backtest_row({"code": code, "name": case["name"],
            "strategy_id": case["strategy_id"], "strategy_version": case["config_version"],
            "strategy_execution": case["strategy_execution"], "context": features.get("daily_context") or features.get("factors") or {},
            "共振板块": features.get("resonance_sectors") or ""}))
        engine = _IndependentEngine(self.dm, config, price=price, shares=200 if code.startswith("688") else 100)
        engine.calendar = self.calendar
        engine._last_entry_meta[code] = {"confirm_time": confirm_time, "entry_time": fill["time"],
            "structural_stop": (case["strategy_execution"].get("structures") or {}).get(case["entry_mode"], {}).get("protection", 0)}
        engine._execute_buy(plan, date)
        position = engine.current_positions[code]
        progress = {"engine": engine.export_state(), "config": cfg, "entry_price": price,
            "entry_date": date, "entry_time": str(fill["time"]), "last_processed": "",
            "cost_version": engine._cost_version(), "exit_rule_version": engine._exit_rule_version(position),
            "outcome_version": OUTCOME_VERSION, "fills": [{"side": "buy", "shares": position["shares"],
            "price": price, "commission": price * position["shares"] * config.commission_rate}]}
        self.repository.save_progress(case["case_id"], "pending", "", progress, con)
        return 1

    def _seed(self, plan, case, price, scanner):
        code = case["code"]
        # STAR requires >=200 shares; other supported A-share instruments use 100.
        shares = 200 if code.startswith("688") else 100
        engine = _IndependentEngine(self.dm, self.config, price=price, shares=shares)
        engine.calendar = self.calendar
        engine._last_entry_gap[code] = scanner._last_entry_gap.get(code, 0)
        engine._last_entry_signal[code] = scanner._last_entry_signal.get(code, "")
        engine._last_entry_meta[code] = dict(scanner._last_entry_meta.get(code) or {})
        engine._execute_buy(plan, case["market_date"])
        return engine

    def refresh(self, as_of_date: str) -> int:
        """Resume each case at its next incomplete day; never advance across missing evidence."""
        if not self.calendar.is_real:
            raise ValueError("Signal outcomes require a real trading calendar")
        updated = 0
        with self.repository.connect() as con:
            rows = con.execute("""SELECT c.payload_json, p.payload_json FROM signal_cases c
                JOIN signal_case_progress p USING(case_id) WHERE p.state IN ('pending', 'blocked')
                AND c.market_date<=? ORDER BY c.market_date, c.case_id""", [as_of_date]).fetchall()
            for case_text, progress_text in rows:
                case, progress = json.loads(case_text), json.loads(progress_text)
                cfg = BacktestConfig(**progress["config"])
                engine = _IndependentEngine(self.dm, cfg, price=progress["entry_price"], shares=100)
                engine.calendar = self.calendar
                engine.import_state(progress["engine"])
                start = self.calendar.next(progress["last_processed"]) if progress["last_processed"] else case["market_date"]
                for date in self.calendar.get_trade_dates(start, as_of_date):
                    if date == datetime.now().strftime("%Y%m%d") and datetime.now().hour < 16:
                        break
                    before_day = copy.deepcopy(progress)
                    before_engine = engine.export_state()
                    con.execute("BEGIN TRANSACTION")
                    try:
                        reason = self._advance(engine, case, progress, date, con)
                    except Exception as exc:
                        reason = f"data_error: {exc}"
                    if reason:
                        con.execute("ROLLBACK")
                        progress = before_day
                        engine.import_state(before_engine)
                        progress["blocked_reason"] = reason
                        progress["engine"] = engine.export_state()
                        self.repository.save_progress(case["case_id"], "blocked", progress["last_processed"], progress, con)
                        break
                    progress.pop("blocked_reason", None)
                    progress["last_processed"] = date
                    progress["engine"] = engine.export_state()
                    finished = not engine.current_positions and date >= self.calendar.offset(case["market_date"], 5)
                    self.repository.save_progress(case["case_id"], "complete" if finished else "pending", date, progress, con)
                    con.execute("COMMIT")
                    updated += 1
                    if finished:
                        break
        return updated

    def _advance(self, engine, case, progress, date, con) -> str:
        code = case["code"]
        bar = engine._get_stock_daily_bar(code, date)
        if not bar or not bar.get("close"):
            return "missing_daily"
        entry_price = progress["entry_price"]
        position = engine.current_positions.get(code)
        reference = float(progress.get("last_daily_close") or 0)
        pre = float(bar.get("pre_close") or 0)
        if reference and pre and abs(pre / reference - 1) > .001:
            return "corporate_action_requires_verified_cashflows"
        if position:
            raw = self.dm.get_stock_tick(engine._standardize_stock_code(code), date)
            bars, reason = validate_minutes(raw, date, full_session=True)
            if reason != "complete":
                return reason
            # Synthetic split adjustment is not an executable corporate-action ledger.
            eligible = bars[bars["time"] >= progress["entry_time"]] if date == case["market_date"] else bars
            engine._exit_minute_frames[(date, code)] = eligible
            before = dict(position)
            result = None
            if date > case["market_date"]:
                intent = progress.get("exit_intent")
                result = intent or engine._minute_exit_decision(code, position, date, bar)
            if result and result.get("sell"):
                progress["exit_intent"] = {**result, "date": result.get("date", date)}
                trigger_time = result.get("time", "15:00:00") if not intent else "00:00:00"
                future = eligible[eligible["time"] > trigger_time]
                floor = limit_down_price(pre, code, case.get("name") or "") if pre > 0 else None
                if floor is None:
                    return "missing_price_limit"
                future = future[(future["volume"] > 0) & (future["open"] * (1 - engine.config.slippage) > floor)]
                if not future.empty:
                    fill = future.iloc[0]
                    exit_time = str(fill["time"])
                    # Reset excursions computed inside the exit detector; no post-fill extremes.
                    position.update(before)
                    through = eligible[eligible["time"] < exit_time]
                    if not through.empty:
                        engine._update_excursion(position, float(through["high"].max()), float(through["low"].min()))
                    engine._update_excursion(position, float(fill["open"]), float(fill["open"]))
                    engine._execute_sell(code, float(fill["open"]), date, str(result["reason"]))
                    trade = engine.trade_history[-1]
                    received = trade.exit_price * trade.shares
                    progress["fills"].append({"side": "sell", "shares": trade.shares, "price": trade.exit_price,
                                              "commission": received * engine.config.commission_rate,
                                              "tax": received * engine.config.stamp_duty_rate})
                    outcome = closed_trade_result(progress["fills"])
                    ended = event_time(date, exit_time)
                    available = max(ended, datetime.now().isoformat(timespec="milliseconds"))
                    versions = {key: progress[key] for key in ("cost_version", "exit_rule_version", "outcome_version")}
                    precision = ("minute_point_next_observation" if "source" in eligible and eligible["source"].eq("eltdx").any()
                                 else "minute_next_open_estimate")
                    outcome.update(label_end_at=ended, available_at=available, exit_time=exit_time,
                                   exit_reason=trade.exit_reason, **versions, execution_precision=precision,
                                   intraminute_order="conservative_ambiguous")
                    self.repository.save_outcome(case["case_id"], "net_return", outcome, con)
                    for label, value in (("mfe", trade.mfe_pct), ("mae", trade.mae_pct)):
                        self.repository.save_outcome(case["case_id"], label,
                            {"state": "mature", "value": value, "label_end_at": ended,
                             "available_at": available, "execution_precision": precision, **versions}, con)
                    progress.pop("exit_intent", None)
            if code in engine.current_positions:
                engine._update_excursion(position, float(eligible["high"].max()), float(eligible["low"].min()))
                position["last_close"] = float(bar["close"])
                position["market_value"] = position["shares"] * float(bar["close"])
        for horizon in (1, 3, 5):
            if date == self.calendar.offset(case["market_date"], horizon):
                ended = event_time(date)
                self.repository.save_outcome(case["case_id"], f"return_d{horizon}", {
                    "state": "mature", "value": float(bar["close"]) / entry_price - 1,
                    "label_end_at": ended, "available_at": max(ended, datetime.now().isoformat(timespec="milliseconds")),
                    "basis": "unrealized_close_return", "outcome_version": OUTCOME_VERSION}, con)
        progress["last_daily_close"] = float(bar["close"])
        return ""

    def run_day(self, candidate_date: str, market_date: str, *, web_data_dir: Path) -> dict:
        path = web_data_dir / "screening" / "decision_pool" / f"decision_pool_{candidate_date}.json"
        if not path.exists():
            raise FileNotFoundError(f"Missing frozen decision pool: {candidate_date}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if str(payload.get("trade_date") or payload.get("date") or candidate_date) != candidate_date:
            raise ValueError("Decision pool date mismatch")
        rows = [row for row in payload.get("rows") or [] if row.get("execution_eligible")]
        with self.repository.connect() as con:
            for row in payload.get("rows") or []:
                if not row.get("execution_eligible"):
                    con.execute("INSERT OR REPLACE INTO signal_case_attempts VALUES (?, ?, ?, ?)",
                        [stable_id("vetoed_case", candidate_date, row.get("code")), market_date, "strategy_veto",
                         json.dumps(clean(row), ensure_ascii=False)])
        version = hashlib.sha256(path.read_bytes()).hexdigest()
        recorded = self.discover(rows, candidate_date=candidate_date, market_date=market_date,
                                 source_available_at=payload.get("generated_at") or "", data_version=version)
        initialized = self.initialize_live(market_date)
        refreshed = self.refresh(market_date)
        return {"recorded": recorded, "initialized_live": initialized,
                "refreshed_days": refreshed, **self.repository.coverage()}


def run_postclose_cases(trade_date: str, *, dm, db_path: Path, web_data_dir: Path, ledger_path: Path) -> dict:
    repository = SignalCaseRepository(db_path)
    archived = repository.import_staged(ledger_path, through_date=trade_date)
    builder = SignalCaseBuilder(CaseDataSource(dm), repository=repository)
    previous = builder.calendar.prev(trade_date)
    path = web_data_dir / "screening" / "decision_pool" / f"decision_pool_{previous}.json"
    if path.exists():
        report = builder.run_day(previous, trade_date, web_data_dir=web_data_dir)
    else:
        builder.initialize_live(trade_date)
        refreshed = builder.refresh(trade_date)
        report = {"missing_candidate_date": previous, "refreshed_days": refreshed, **repository.coverage()}
    report["archived_live"] = archived
    report["trade_date"] = trade_date
    directory = web_data_dir / "signal_cases"
    directory.mkdir(parents=True, exist_ok=True)
    from core.signals.similar_case_service import publish_case_index
    report["matching_index"] = publish_case_index(repository, directory,
                                                  as_of=datetime.now().isoformat(timespec="milliseconds"))
    target = directory / f"coverage_{trade_date}.json"
    pending = target.with_suffix(".json.tmp")
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(target)
    return report
