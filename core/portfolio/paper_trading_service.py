"""Automatic paper fills and historical replay imports."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Dict

import pandas as pd

from config.settings import (
    OUTPUT_DIR,
    PAPER_INITIAL_CAPITAL,
    PAPER_MAX_POSITIONS,
    PAPER_POSITION_PCT,
    PAPER_ROTATION_MIN_EDGE,
)
from core.portfolio.holding_repository import HoldingRepository
from core.realtime.models import normalize_stock_code


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "", "--") else default
    except (TypeError, ValueError):
        return default


class PaperTradingService:
    """Turn confirmed minute signals into isolated paper-account executions."""

    ACCOUNT_KEY = "default"

    def __init__(
        self,
        repository: HoldingRepository | None = None,
        *,
        initial_capital: float = PAPER_INITIAL_CAPITAL,
        max_positions: int = PAPER_MAX_POSITIONS,
        position_pct: float = PAPER_POSITION_PCT,
        rotation_min_edge: float = PAPER_ROTATION_MIN_EDGE,
        quote_service: Any = None,
    ) -> None:
        self.repository = repository or HoldingRepository()
        self.quote_service = quote_service
        self.initial_capital = max(float(initial_capital), 10_000.0)
        self.max_positions = min(max(int(max_positions), 1), 3)
        self.position_pct = min(max(float(position_pct), 1.0), 100.0)
        self.rotation_min_edge = min(max(float(rotation_min_edge), 0.0), 30.0)
        self.repository.ensure_account(
            self.ACCOUNT_KEY,
            name="模拟交易账户",
            initial_capital=self.initial_capital,
        )

    def process_realtime_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        from core.portfolio.execution_quotes import execution_price, quote_error
        from core.operations.ledger import TradingLedger, stable_id
        from core.realtime.quote_service import RealtimeQuoteService
        from risk.risk_config import RiskConfig

        market_date = str(payload.get("market_date") or "").replace("-", "")[:8]
        profile = str(payload.get("profile") or "realtime")
        strategy = dict(payload.get("strategy") or {})
        opened = []
        rotations = []
        skipped: Dict[str, int] = {}
        rows = sorted(
            (dict(item or {}) for item in payload.get("rows") or []),
            key=self._candidate_strength,
            reverse=True,
        )
        confirmed_codes = [normalize_stock_code(row.get("code"), add_suffix=False)
                           for row in rows if not self._skip_reason(row, market_date)]
        existing = self.repository.list_positions(self.ACCOUNT_KEY)
        codes = list(dict.fromkeys([*confirmed_codes, *[p["code"] for p in existing]])) if confirmed_codes else []
        quotes = {}
        if codes:
            try:
                service = self.quote_service or RealtimeQuoteService()
                quote_rows = service.get_quotes(codes).get("quotes") or []
                quotes = {normalize_stock_code(q.get("code"), add_suffix=False): q for q in quote_rows}
            except Exception:
                quotes = {}
        costs = RiskConfig.load()
        ledger = TradingLedger(self.repository.db_path)
        for row in rows:
            reason = self._skip_reason(row, market_date)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            code = normalize_stock_code(row.get("code"), add_suffix=False)
            signal_id = str(row.get("signal_id") or stable_id(
                "signal", payload.get("candidate_date") or payload.get("trade_date"),
                market_date, code, profile, row.get("entry_mode"), row.get("confirm_time"),
            ))

            def reject(message: str) -> None:
                skipped[message] = skipped.get(message, 0) + 1
                ledger.event(stable_id(signal_id, "not_executable", message), "paper_order_rejected",
                             {"code": code, "reason": message, "market_date": market_date},
                             signal_id=signal_id, account_key=self.ACCOUNT_KEY)

            buy_quote = quotes.get(code) or {}
            entry_time = str(row.get("entry_time") or "")
            error = quote_error(buy_quote, market_date, side="buy", reference_time=entry_time)
            if error or not entry_time:
                reject(error or "缺少成交时间")
                continue
            try:
                price = execution_price(buy_quote, side="buy", reference_time=entry_time,
                                        slippage=costs.slippage)
            except (TypeError, ValueError, KeyError) as exc:
                reject(str(exc) or "成交报价无效")
                continue
            if self.repository.get_open_position(code, self.ACCOUNT_KEY):
                skipped["已有持仓"] = skipped.get("已有持仓", 0) + 1
                continue
            if self.repository.has_buy_trade(self.ACCOUNT_KEY, code, market_date):
                skipped["当日已买入"] = skipped.get("当日已买入", 0) + 1
                continue
            positions = self.repository.list_positions(self.ACCOUNT_KEY)
            account = self.repository.account(self.ACCOUNT_KEY)
            cash = _number(account.get("cash"))
            if any(quote_error(quotes.get(p["code"]), market_date,
                               reference_time=buy_quote["time"]) for p in positions):
                reject("持仓行情不足，暂停估值与买入")
                continue
            equity = cash + sum(
                _number((quotes.get(item["code"]) or {}).get("last_price"))
                * int(item.get("shares") or 0)
                for item in positions
            )
            position_pct = self._position_pct(row)
            per_position = min(equity * position_pct / 100.0, cash) / (1 + costs.commission_rate)
            shares = int(per_position / price / 100) * 100
            candidate_strength = self._candidate_strength(row)
            rotation = None
            rotation_order = None
            if len(positions) >= self.max_positions or shares < 100:
                if any(quote_error(quotes.get(p["code"]), market_date,
                                   reference_time=buy_quote["time"]) for p in positions):
                    reject("持仓行情不足，暂停换股")
                    continue
                positions = [{**p, "last_price": _number((quotes.get(p["code"]) or {}).get("last_price")),
                              "execution_quote": quotes.get(p["code"], {})} for p in positions]
                equity = cash + sum(p["last_price"] * int(p["shares"]) for p in positions)
                target = self._rotation_target(
                    positions,
                    market_date=market_date,
                    candidate_strength=candidate_strength,
                    reference_time=buy_quote["time"],
                )
                if not target:
                    key = "没有可换出的更弱持仓" if positions else "可用资金不足一手"
                    reject(key)
                    continue
                sell_price = execution_price(target["execution_quote"], side="sell",
                                             reference_time=target["execution_quote"]["time"],
                                             slippage=costs.slippage)
                sell_fees = sell_price * int(target.get("shares") or 0) * (
                    costs.commission_rate + costs.stamp_duty_rate)
                expected_cash = cash + sell_price * int(target.get("shares") or 0) - sell_fees
                expected_shares = int(
                    min(equity * position_pct / 100.0, expected_cash)
                    / (1 + costs.commission_rate) / price / 100
                ) * 100
                if sell_price <= 0 or expected_shares < 100:
                    reject("换股后资金仍不足一手")
                    continue
                target_strength = self._holding_strength(target)
                rotation_order = {
                        "trade_date": market_date,
                        "trade_time": target["execution_quote"]["time"],
                        "price": sell_price,
                        "fees": sell_fees,
                        "shares": int(target.get("shares") or 0),
                        "reason": (
                            f"强弱换股：新信号{candidate_strength:.1f}分，"
                            f"高于当前持仓{target_strength:.1f}分"
                        ),
                        "source": "auto_rotation",
                        "metadata": {
                            "replacement_code": code,
                            "candidate_strength": candidate_strength,
                            "holding_strength": target_strength,
                        },
                    }
                rotation = {
                    "sold_code": target.get("code"),
                    "sold_name": target.get("name"),
                    "sold_strength": round(target_strength, 2),
                    "bought_code": code,
                    "candidate_strength": round(candidate_strength, 2),
                }
                shares = expected_shares
            if shares < 100:
                reject("可用资金不足一手")
                continue
            strategy_id = str(row.get("strategy_id") or strategy.get("id") or profile)
            strategy_name = str(row.get("strategy_name") or strategy.get("name") or profile)
            sectors = row.get("resonance_sectors") or row.get("sector_names") or ""
            if isinstance(sectors, (list, tuple, set)):
                sectors = ",".join(str(item) for item in sectors if item)
            buy_payload = {
                        "code": code,
                        "name": row.get("name"),
                        "entry_date": market_date,
                        "entry_time": buy_quote["time"],
                        "entry_price": price,
                        "fees": price * shares * costs.commission_rate,
                        "structural_stop": _number(row.get("structural_stop")),
                        "shares": shares,
                        "strategy_id": strategy_id,
                        "strategy_name": strategy_name,
                        "sector_names": sectors,
                        "source": "auto_rotation" if rotation else "auto_realtime",
                        "reason": (
                            f"{row.get('entry_mode_text') or '分钟信号'}确认后有效报价模拟成交，"
                            f"模拟仓位{position_pct:.0f}%，信号强度{candidate_strength:.1f}分"
                        ),
                        "metadata": {
                            "signal_id": signal_id,
                            "signal_evidence": row.get("signal_evidence") or {},
                            "strategy_execution": row.get("strategy_execution") or strategy.get("execution") or {},
                            "strategy_version": row.get("strategy_version") or strategy.get("version") or "legacy",
                            "structure": row.get("structure") or {},
                            "protection_price_source": "盘后结构保护价" if row.get("structural_stop") else "账户风险底线",
                            "candidate_date": payload.get("candidate_date") or payload.get("trade_date"),
                            "entry_mode": row.get("entry_mode"),
                            "confirm_time": row.get("confirm_time"),
                            "profile": profile,
                            "is_leader_observation": bool(row.get("is_leader_observation")),
                            "entry_strength_score": round(candidate_strength, 2),
                            "screening_score": _number(row.get("screening_score")),
                            "quote_source_time": buy_quote.get("time"),
                            "quote_received_at": buy_quote.get("received_at"),
                            "execution_price_basis": "observed_quote_plus_slippage",
                            "reference_signal_price": row.get("entry_price"),
                            "success_probability": _number(row.get("success_probability")),
                            "rotation": rotation or {},
                        },
                    }
            order_id = stable_id(signal_id, "paper_order", "buy")
            if rotation_order:
                rotation_order.setdefault("metadata", {})["signal_id"] = signal_id
            ledger.event(order_id, "paper_order", {
                "code": code, "market_date": market_date, "entry_time": buy_payload["entry_time"],
                "price": price, "shares": shares, "fees": buy_payload["fees"],
                "price_basis": "observed_quote_plus_slippage", "strategy_id": strategy_id,
                "rotation": rotation or {},
            }, signal_id=signal_id, account_key=self.ACCOUNT_KEY)
            try:
                opened_position = (self.repository.rotate_position(int(target["id"]), rotation_order, buy_payload, self.ACCOUNT_KEY)
                                   if rotation_order else self.repository.open_position(buy_payload, self.ACCOUNT_KEY))
            except ValueError as exc:
                reject(str(exc))
                continue
            evidence = dict(row.get("signal_evidence") or {})
            if evidence:
                evidence["paper_execution_time"] = buy_payload["entry_time"]
                ledger.evidence(signal_id, evidence)
            opened.append(opened_position)
            if rotation:
                rotations.append(rotation)
        return {
            "ok": True,
            "account_key": self.ACCOUNT_KEY,
            "opened": len(opened),
            "rotated": len(rotations),
            "rotations": rotations,
            "positions": opened,
            "skipped": skipped,
        }

    def _position_pct(self, row: Dict[str, Any]) -> float:
        # 模拟交易账户采用固定三槽位资金规则，候选页的保守仓位提示只作展示。
        return self.position_pct

    @staticmethod
    def _candidate_strength(row: Dict[str, Any]) -> float:
        base = 0.0
        for field in (
            "turn_score",
            "screening_score",
            "leader_score",
            "score",
            "success_probability",
            "confidence_score",
        ):
            value = _number(row.get(field))
            if 0 < value <= 1:
                value *= 100.0
            if value > 0:
                base = value
                break
        if base <= 0:
            base = 50.0
        mode_bonus = {
            "weak_to_strong": 4.0,
            "continuation": 3.0,
            "acceleration": 2.0,
            "high_open_acceleration": 2.0,
        }.get(str(row.get("entry_mode") or ""), 0.0)
        group_bonus = 4.0 if str(row.get("action_group") or "") == "重点确认" else 0.0
        sector = dict(row.get("sector_detail") or {})
        breadth = _number(sector.get("breadth"))
        if 0 < breadth <= 1:
            breadth *= 100.0
        sector_bonus = max(min((breadth - 50.0) / 20.0, 3.0), -3.0) if breadth else 0.0
        return round(max(0.0, min(100.0, base + mode_bonus + group_bonus + sector_bonus)), 2)

    @staticmethod
    def _holding_strength(position: Dict[str, Any]) -> float:
        metadata = dict(position.get("metadata") or {})
        base = _number(metadata.get("entry_strength_score"), 50.0)
        entry_price = _number(position.get("entry_price"))
        last_price = _number(position.get("last_price"), entry_price)
        pnl_pct = (last_price / entry_price - 1.0) * 100.0 if entry_price > 0 else 0.0
        momentum = max(min(pnl_pct * 0.8, 12.0), -12.0)
        action_adjustment = {
            "hold": 0.0,
            "watch": -5.0,
            "reduce": -12.0,
            "sell": -25.0,
            "blocked": -20.0,
            "data_insufficient": -3.0,
        }.get(str(position.get("latest_action") or "hold"), 0.0)
        return round(max(0.0, min(100.0, base + momentum + action_adjustment)), 2)

    def _rotation_target(
        self,
        positions: list[Dict[str, Any]],
        *,
        market_date: str,
        candidate_strength: float,
        reference_time: str = "",
    ) -> Dict[str, Any]:
        from core.portfolio.execution_quotes import quote_error

        sellable = [
            row
            for row in positions
            if str(row.get("entry_date") or "").replace("-", "") < market_date
            and int(row.get("shares") or 0) > 0
            and _number(row.get("last_price"), _number(row.get("entry_price"))) > 0
            and not quote_error(row.get("execution_quote") or {}, market_date, reference_time=reference_time)
            and self.repository.sellable_shares(int(row["id"]), market_date) == int(row["shares"])
        ]
        if not sellable:
            return {}
        weakest = min(sellable, key=self._holding_strength)
        weakest_strength = self._holding_strength(weakest)
        if candidate_strength < weakest_strength + self.rotation_min_edge:
            return {}
        return weakest

    def prefetch_replay_minutes(
        self,
        start_date: str,
        end_date: str,
        *,
        progress: Callable[[Dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        """Cache T+1 minute/auction evidence before a historical replay."""
        from config.settings import CACHE_DIR, TUSHARE_TOKEN
        from core.data import DataManager
        from core.factors.strategy_training import STRATEGY_TRAINING_SPECS
        from scripts.prefetch_strategy_minutes import _minute_cached, build_requirements

        profiles = list(STRATEGY_TRAINING_SPECS)
        minute_requirements, auction_requirements, audit = build_requirements(
            profiles,
            str(start_date),
            str(end_date),
            include_sector_peers=True,
            peer_count=8,
        )
        cache_dir = Path(CACHE_DIR)
        cached = {
            requirement
            for requirement in minute_requirements
            if _minute_cached(cache_dir, *requirement)
        }
        pending = sorted(minute_requirements - cached)
        total = len(pending) + len(auction_requirements)
        completed = 0
        fetched = 0
        failed = []
        auction_fetched = 0
        auction_failed = []

        def publish(stage: str) -> None:
            if progress:
                progress({
                    "stage": stage,
                    "completed": completed,
                    "total": total,
                    "minute_cached": len(cached),
                    "minute_fetched": fetched,
                    "minute_failed": len(failed),
                    "auction_fetched": auction_fetched,
                    "auction_failed": len(auction_failed),
                })

        publish("准备分钟需求")
        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
        for trade_date, ts_code in pending:
            frame = dm.get_stock_tick(ts_code, trade_date)
            if frame is not None and len(frame) > 1:
                fetched += 1
            else:
                failed.append({"trade_date": trade_date, "ts_code": ts_code})
            completed += 1
            if completed % 20 == 0 or completed == total:
                publish("补齐分钟行情")

        for trade_date, ts_code in sorted(auction_requirements):
            payload = dm.get_auction_data(ts_code, trade_date)
            if payload:
                auction_fetched += 1
            else:
                auction_failed.append({"trade_date": trade_date, "ts_code": ts_code})
            completed += 1
            if completed % 20 == 0 or completed == total:
                publish("补齐集合竞价")

        result = {
            **audit,
            "minute_cached": len(cached),
            "minute_fetched": fetched,
            "minute_failed": len(failed),
            "minute_failures_preview": failed[:20],
            "auction_fetched": auction_fetched,
            "auction_failed": len(auction_failed),
            "auction_failures_preview": auction_failed[:20],
            "completed": completed,
            "total": total,
        }
        if progress:
            progress({"stage": "分钟证据准备完成", **result})
        return result

    @staticmethod
    def _skip_reason(row: Dict[str, Any], market_date: str) -> str:
        if str(row.get("confirm_status") or row.get("status") or "") != "confirmed":
            return "尚未确认"
        if not market_date:
            return "行情日期缺失"
        if _number(row.get("entry_price")) <= 0:
            return "下一分钟成交价缺失"
        if bool(row.get("is_stale")):
            return "行情已过期"
        return ""

    def import_backtest_run(self, run_id: str, *, reset: bool = True,
                            account_key: str | None = None) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        account_key = str(account_key or self.ACCOUNT_KEY)
        if not run_id:
            raise ValueError("回测批次不能为空")
        result_dir = Path(OUTPUT_DIR) / "backtest_results"
        trades_path = result_dir / f"backtest_trades_{run_id}.csv"
        positions_path = result_dir / f"backtest_positions_{run_id}.csv"
        if not trades_path.exists() and not positions_path.exists():
            raise ValueError(f"未找到回测批次 {run_id}")
        if reset and account_key == self.ACCOUNT_KEY:
            with self.repository._connect() as conn:
                occupied = conn.execute(
                    "SELECT 1 FROM portfolio_trades WHERE account_key=? LIMIT 1",
                    (account_key,),
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM portfolio_positions WHERE account_key=? LIMIT 1",
                    (account_key,),
                ).fetchone()
            if occupied:
                raise ValueError("当前模拟账户已有成交或持仓；历史重建必须指定独立账户")
        self.repository.ensure_account(
            account_key,
            name="历史重建账户" if account_key != self.ACCOUNT_KEY else "模拟交易账户",
            initial_capital=self.initial_capital,
        )
        if reset:
            self.repository.reset_account(
                account_key,
                initial_capital=self.initial_capital,
            )

        imported = {"buy": 0, "sell": 0, "open": 0, "skipped": 0}
        if trades_path.exists():
            trades = pd.read_csv(trades_path, dtype={"stock_code": str})
            for row in trades.to_dict("records"):
                try:
                    self._import_trade(row, run_id, imported, account_key=account_key)
                except (TypeError, ValueError):
                    imported["skipped"] += 1
        if positions_path.exists():
            positions = pd.read_csv(positions_path, dtype={"stock_code": str})
            for row in positions.to_dict("records"):
                code = normalize_stock_code(row.get("stock_code"), add_suffix=False)
                if not code or self.repository.get_open_position(code, account_key):
                    continue
                if len(self.repository.list_positions(account_key)) >= self.max_positions:
                    imported["skipped"] += 1
                    continue
                try:
                    opened = self.repository.open_position(
                        {
                            "code": code,
                            "name": row.get("stock_name"),
                            "entry_date": row.get("entry_date"),
                            "entry_time": row.get("entry_time"),
                            "entry_price": row.get("entry_price"),
                            "shares": row.get("shares"),
                            "strategy_id": row.get("strategy_id"),
                            "strategy_name": row.get("strategy_name"),
                            "source": "historical_replay",
                            "metadata": {"run_id": run_id},
                        },
                        account_key,
                    )
                    current = _number(row.get("current_price"), _number(row.get("entry_price")))
                    self.repository.update_position_market(
                        int(opened["id"]),
                        last_price=current,
                        high_watermark=max(current, _number(opened.get("high_watermark"))),
                        action="hold",
                        reason="历史分钟回放结束时仍持仓",
                    )
                    imported["open"] += 1
                except (TypeError, ValueError):
                    imported["skipped"] += 1
        return {**imported, "run_id": run_id, "account_key": account_key}

    def _import_trade(
        self,
        row: Dict[str, Any],
        run_id: str,
        imported: Dict[str, int],
        *, account_key: str | None = None,
    ) -> None:
        account_key = str(account_key or self.ACCOUNT_KEY)
        action = str(row.get("action") or "").strip().lower()
        code = normalize_stock_code(row.get("stock_code"), add_suffix=False)
        shares = int(_number(row.get("shares")))
        entry_price = _number(row.get("entry_price"))
        exit_price = _number(row.get("exit_price"))
        position_size = _number(row.get("position_size"))
        pnl = _number(row.get("pnl"))
        if action == "buy":
            if (
                not self.repository.get_open_position(code, account_key)
                and len(self.repository.list_positions(account_key)) >= self.max_positions
            ):
                imported["skipped"] += 1
                return
            self.repository.open_position(
                {
                    "code": code,
                    "name": row.get("stock_name"),
                    "entry_date": row.get("entry_date") or row.get("date"),
                    "entry_time": row.get("entry_time"),
                    "entry_price": row.get("entry_price"),
                    "shares": row.get("shares"),
                    "fees": max(position_size - entry_price * shares, 0.0)
                    if math.isfinite(position_size) and position_size > 0 else 0.0,
                    "strategy_id": row.get("strategy_id"),
                    "strategy_name": row.get("strategy_name"),
                    "sector_names": row.get("resonance_sectors"),
                    "source": "historical_replay",
                    "reason": row.get("entry_signal") or "分钟买点确认",
                    "metadata": {"run_id": run_id, "execution_price_basis": "historical_minute_estimate",
                                 "cost_basis_source": "backtest_position_size" if position_size > 0 else "unknown"},
                },
                account_key,
            )
            imported["buy"] += 1
            return
        if action != "sell":
            imported["skipped"] += 1
            return
        position = self.repository.get_open_position(code, account_key)
        if not position:
            imported["skipped"] += 1
            return
        self.repository.sell_position(
            int(position["id"]),
            {
                "trade_date": row.get("date"),
                "trade_time": row.get("exit_time") or "",
                "price": row.get("exit_price"),
                "shares": min(int(_number(row.get("shares"))), int(position.get("shares") or 0)),
                "fees": max(exit_price * shares - position_size - pnl, 0.0)
                if math.isfinite(position_size) and math.isfinite(pnl) and position_size > 0 else 0.0,
                "reason": row.get("exit_reason") or "历史分钟回放退出",
                "source": "historical_replay",
                "metadata": {"run_id": run_id, "execution_price_basis": "historical_minute_estimate",
                             "cost_basis_source": "backtest_position_size_and_pnl" if position_size > 0 else "unknown"},
            },
        )
        imported["sell"] += 1

    @staticmethod
    def latest_run_id(*, newer_than: float = 0.0) -> str:
        result_dir = Path(OUTPUT_DIR) / "backtest_results"
        files = sorted(
            result_dir.glob("backtest_summary_*.csv"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files:
            if path.stat().st_mtime + 1e-6 >= newer_than:
                return path.stem.removeprefix("backtest_summary_")
        return ""


__all__ = ["PaperTradingService"]
