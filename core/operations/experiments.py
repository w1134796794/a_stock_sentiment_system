"""Versioned, rolling evaluation of the five production strategies.

Historical artifacts are descriptive until a configuration is frozen before the
validation window and each daily artifact was actually generated on that date.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config.settings import BASE_DIR, WEB_DATA_DIR
from core.operations.ledger import TradingLedger, stable_id
from core.operations.equity import AccountEquityEvaluator
from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS, StrategyProfileRepository


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _sources(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value or "").replace("，", ",").split(",") if part.strip()}


def _code(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


class StrategyExperimentLedger:
    def __init__(self, *, root: Path | None = None, ledger: TradingLedger | None = None,
                 profiles: StrategyProfileRepository | None = None) -> None:
        self.root = Path(root or WEB_DATA_DIR)
        self.ledger = ledger or TradingLedger()
        self.profiles = profiles or StrategyProfileRepository()

    @staticmethod
    def _factor_hash() -> str:
        paths = [Path(BASE_DIR) / "core" / "factors" / "factor_library.py"]
        paths.extend(sorted((Path(BASE_DIR) / "core" / "factors" / "jobs").glob("*.py")))
        paths.extend(Path(BASE_DIR) / path for path in (
            "core/screening/screening_engine.py",
            "core/screening/strategy_profiles.py",
            "core/realtime/entry_signal_service.py",
            "core/portfolio/paper_trading_service.py",
            "core/portfolio/execution_quotes.py",
        ))
        digest = hashlib.sha256()
        for path in paths:
            digest.update(str(path.relative_to(BASE_DIR)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def freeze(self, *, label: str = "production-five") -> dict[str, Any]:
        profiles = {
            strategy_id: self.profiles.get_profile(strategy_id) or {}
            for strategy_id in PRODUCTION_STRATEGY_IDS
        }
        frozen_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        config_hash = _hash(profiles)
        factor_hash = self._factor_hash()
        experiment_id = stable_id("experiment", label, frozen_at, config_hash, factor_hash)
        report = {
            "experiment_id": experiment_id, "label": label, "frozen_at": frozen_at,
            "configuration_hash": config_hash, "factor_hash": factor_hash,
            "strategy_ids": list(PRODUCTION_STRATEGY_IDS), "profiles": profiles,
            "folds": [], "status": "frozen_waiting_for_validation",
        }
        self.ledger.save_experiment(experiment_id, frozen_at, config_hash, factor_hash, report)
        return report

    def evaluate(self, experiment_id: str, *, start: str, end: str,
                 train_days: int = 60, validation_days: int = 20,
                 initial_capital: float = 1_000_000.0,
                 account_key: str = "default",
                 equity_evaluator: AccountEquityEvaluator | None = None) -> dict[str, Any]:
        frozen = self.ledger.experiment(experiment_id)
        if not frozen:
            raise ValueError("实验不存在，请先冻结配置")
        if train_days < 1 or validation_days < 1 or start > end:
            raise ValueError("训练/验证窗口或日期范围无效")
        report = dict(frozen["report"])
        current_profiles = {
            key: self.profiles.get_profile(key) or {} for key in PRODUCTION_STRATEGY_IDS
        }
        versions_match = (
            _hash(current_profiles) == frozen["configuration_hash"]
            and self._factor_hash() == frozen["factor_hash"]
        )
        pool_dir = self.root / "screening" / "decision_pool"
        pool_files = sorted(pool_dir.glob("decision_pool_????????.json"))
        pools = {}
        for path in pool_files:
            date = path.stem.rsplit("_", 1)[-1]
            if start <= date <= end:
                data = _load(path)
                if data.get("trade_date") == date:
                    pools[date] = data
        dates = sorted(pools)
        if len(dates) <= train_days:
            raise ValueError("日期范围内的决策池不足以构造首个验证窗口")
        with self.ledger._connect() as conn:
            signal_rows = conn.execute(
                "SELECT signal_id, occurred_at, payload_json FROM trading_events WHERE kind='signal'"
            ).fetchall()
            fill_rows = conn.execute(
                "SELECT signal_id, payload_json FROM trading_events WHERE kind='paper_fill' AND account_key=?",
                (account_key,),
            ).fetchall()
            rejected_rows = conn.execute(
                "SELECT signal_id FROM trading_events WHERE kind='paper_order_rejected' AND account_key=?",
                (account_key,),
            ).fetchall()
            try:
                position_rows = conn.execute(
                    "SELECT id, strategy_id, entry_date, code, metadata_json "
                    "FROM portfolio_positions WHERE account_key=?", (account_key,)
                ).fetchall()
                trade_rows = conn.execute(
                    "SELECT position_id, action, price, shares, fees, trade_date "
                    "FROM portfolio_trades WHERE account_key=?", (account_key,)
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                position_rows, trade_rows = [], []
        signals = defaultdict(lambda: defaultdict(set))
        signal_asof = {}
        for item in signal_rows:
            evidence = json.loads(item["payload_json"])
            market_date = str(evidence.get("market_date") or "").replace("-", "")[:8]
            recorded_date = str(item["occurred_at"] or "").replace("-", "")[:8]
            signal_asof[item["signal_id"]] = bool(market_date and recorded_date <= market_date)
            strategy_ids = _sources(evidence.get("strategy_sources"))
            strategy_ids.add(str(evidence.get("strategy_id") or ""))
            for strategy_id in strategy_ids:
                key = (str(evidence.get("candidate_date") or ""), strategy_id,
                       _code(evidence.get("code")))
                signals[key][str(evidence.get("status") or "")].add(item["signal_id"])
        fills = {item["signal_id"] for item in fill_rows}
        rejected = {item["signal_id"] for item in rejected_rows} - fills
        positions = {int(row["id"]): dict(row) for row in position_rows}
        trades_by_position = defaultdict(list)
        for item in trade_rows:
            trades_by_position[int(item["position_id"])].append(dict(item))
        closed_returns = {}
        for position_id, trades in trades_by_position.items():
            if not any(trade["action"] == "sell" for trade in trades):
                continue
            bought = sum(float(t["price"]) * int(t["shares"]) + float(t["fees"] or 0)
                         for t in trades if t["action"] == "buy")
            sold = sum(float(t["price"]) * int(t["shares"]) - float(t["fees"] or 0)
                       for t in trades if t["action"] == "sell")
            if bought <= 0 or sum(int(t["shares"]) * (1 if t["action"] == "buy" else -1)
                                  for t in trades) != 0:
                continue
            closed_returns[position_id] = (sold / bought - 1) * 100
        outcomes = {}
        slippage = {}
        for position_id, position in positions.items():
            if position_id not in closed_returns:
                continue
            metadata = json.loads(position.get("metadata_json") or "{}")
            candidate_date = str(metadata.get("candidate_date") or "")
            if candidate_date:
                key = (candidate_date, str(position["strategy_id"]), _code(position["code"]))
                outcomes[key] = closed_returns[position_id]
                reference = float(metadata.get("reference_signal_price") or 0)
                buys = [trade for trade in trades_by_position[position_id] if trade["action"] == "buy"]
                if reference > 0 and buys:
                    slippage[key] = (float(buys[0]["price"]) / reference - 1) * 100
        equity = (equity_evaluator or AccountEquityEvaluator(self.ledger)).evaluate(
            account_key=account_key, start=start, end=end, experiment_id=experiment_id,
        )
        folds = []
        from core.factors.factor_library import DynamicWeightRepository

        weight_repository = DynamicWeightRepository()
        for offset in range(train_days, len(dates), validation_days):
            valid_dates = dates[offset:offset + validation_days]
            train_dates = dates[offset - train_days:offset]
            if not valid_dates:
                continue
            daily_versions = {}
            asof_ready = True
            for date in valid_dates:
                pool = pools[date]
                manifest = _load(self.root / "fetch_status" / f"fetch_{date}.json")
                snapshot = _load(self.root / "snapshots" / f"{date}.json")
                generated = str(pool.get("generated_at") or "")[:10].replace("-", "")
                fetched = str(manifest.get("fetched_at") or "")[:10].replace("-", "")
                snapshot_date = str((snapshot.get("meta") or {}).get("date") or "")
                if generated != date or fetched != date or snapshot_date != date:
                    asof_ready = False
                daily_versions[date] = _hash({"pool": pool, "manifest": manifest, "snapshot_meta": snapshot.get("meta")})
            prospective = versions_match and asof_ready and frozen["frozen_at"][:10].replace("-", "") < valid_dates[0]
            factor_ic = {}
            for strategy_id in PRODUCTION_STRATEGY_IDS:
                artifact = (weight_repository.resolve(train_dates[-1], strategy_id)
                            or weight_repository.resolve(train_dates[-1], "default"))
                if artifact is None:
                    continue
                payload = artifact.payload
                source_ready = (
                    str(payload.get("train_end") or "") <= train_dates[-1]
                    and str(payload.get("trained_at") or "")[:10].replace("-", "") < valid_dates[0]
                )
                if not source_ready:
                    prospective = False
                metrics = payload.get("factor_metrics") or {}
                factors = sorted(
                    ({"factor": name, **values} for name, values in metrics.items()
                     if isinstance(values, dict)),
                    key=lambda row: abs(float(row.get("ic_mean") or 0)), reverse=True,
                )[:10]
                factor_ic[strategy_id] = {
                    "profile": artifact.profile, "effective_date": artifact.effective_date,
                    "train_end": payload.get("train_end"), "available_before_validation": source_ready,
                    "factors": factors,
                }
            rows = []
            for date in valid_dates:
                regime = str(pools[date].get("regime") or "unknown")
                for candidate in pools[date].get("rows") or []:
                    sources = _sources(candidate.get("strategy_sources") or candidate.get("策略来源"))
                    sources.add(str(candidate.get("strategy_id") or candidate.get("策略ID") or ""))
                    code = _code(candidate.get("code") or candidate.get("代码"))
                    score = candidate.get("score", candidate.get("策略组合评分", candidate.get("综合评分")))
                    rank = candidate.get("rank", candidate.get("池排名", candidate.get("候选名次")))
                    actionable = bool(candidate.get("execution_eligible", True)) and str(
                        candidate.get("action_group") or candidate.get("行动分组") or "") not in {
                        "inactive", "暂不参与",
                    }
                    for strategy_id in PRODUCTION_STRATEGY_IDS:
                        if strategy_id in sources:
                            rows.append((date, regime, strategy_id, code, score, rank, actionable))
            fold_signal_ids = {signal_id for date, _, strategy, code, _, _, _ in rows
                               for statuses in signals[(date, strategy, code)].values()
                               for signal_id in statuses}
            if any(not signal_asof.get(signal_id, False) for signal_id in fold_signal_ids):
                prospective = False
            summary = []
            for strategy_id in PRODUCTION_STRATEGY_IDS:
                for regime in sorted({row[1] for row in rows} or {"unknown"}):
                    group = {(date, code) for date, phase, strategy, code, _, _, actionable in rows
                             if strategy == strategy_id and phase == regime and actionable}
                    excluded = {(date, code) for date, phase, strategy, code, _, _, actionable in rows
                                if strategy == strategy_id and phase == regime and not actionable}
                    confirmed = set()
                    unfilled = set()
                    executable = set()
                    rejected_candidates = set()
                    for date, code in group:
                        evidence_ids = signals[(date, strategy_id, code)].get("confirmed", set())
                        if evidence_ids:
                            confirmed.add((date, code))
                        if (signals[(date, strategy_id, code)].get("unfilled")
                                or signals[(date, strategy_id, code)].get("signal_unfilled")) and not evidence_ids & fills:
                            unfilled.add((date, code))
                        if evidence_ids & fills:
                            executable.add((date, code))
                        if evidence_ids & rejected:
                            rejected_candidates.add((date, code))
                    unfilled -= rejected_candidates
                    closed = [outcomes[(date, strategy_id, code)] for date, code in group
                              if (date, strategy_id, code) in outcomes]
                    top_three = [(date, code) for date, phase, strategy, code, _, rank, actionable in rows
                                 if phase == regime and strategy == strategy_id
                                 and actionable and rank is not None and str(rank).isdigit() and int(rank) <= 3]
                    top_closed = [outcomes[(date, strategy_id, code)] for date, code in top_three
                                  if (date, strategy_id, code) in outcomes]
                    daily_ic = []
                    for date in valid_dates:
                        pairs = [(float(score), outcomes[(date, strategy_id, code)])
                                 for item_date, phase, strategy, code, score, _, actionable in rows
                                 if item_date == date and phase == regime and strategy == strategy_id
                                 and actionable
                                 and (date, strategy_id, code) in outcomes
                                 and score is not None and str(score).replace(".", "", 1).isdigit()]
                        if len(pairs) >= 3 and len({pair[0] for pair in pairs}) > 1:
                            points = pd.DataFrame(pairs, columns=["score", "return"])
                            value = points["score"].rank().corr(points["return"].rank())
                            if pd.notna(value):
                                daily_ic.append(float(value))
                    observed_slippage = [slippage[(date, strategy_id, code)] for date, code in group
                                         if (date, strategy_id, code) in slippage]
                    summary.append({
                        "strategy_id": strategy_id, "regime": regime,
                        "candidates": len(group), "excluded_candidates": len(excluded),
                        "confirmed": len(confirmed),
                        "confirmation_rate_pct": _rate(len(confirmed), len(group)),
                        "unfilled_signals": len(unfilled),
                        "rejected_orders": len(rejected_candidates),
                        "tradability_rate_pct": _rate(len(executable), len(confirmed | unfilled)),
                        "paper_fills": len(executable),
                        "fill_rate_pct": _rate(len(executable), len(confirmed)),
                        "closed_trades": len(closed),
                        "mean_closed_trade_return_pct": round(sum(closed) / len(closed), 4) if closed else None,
                        "top3_closed_trades": len(top_closed),
                        "top3_win_rate_pct": _rate(sum(value > 0 for value in top_closed), len(top_closed)),
                        "closed_trade_rank_ic": round(sum(daily_ic) / len(daily_ic), 4) if daily_ic else None,
                        "closed_trade_ic_days": len(daily_ic),
                        "mean_signal_to_fill_slippage_pct": (
                            round(sum(observed_slippage) / len(observed_slippage), 4)
                            if observed_slippage else None
                        ),
                    })
            fold_curve = [row for row in equity["rows"] if valid_dates[0] <= row["trade_date"] <= valid_dates[-1]]
            fold_complete = len(fold_curve) == len(valid_dates) and all(row["valuation_complete"] for row in fold_curve)
            before = next((row for row in reversed(equity["rows"])
                           if row["trade_date"] < valid_dates[0] and row["valuation_complete"]), None)
            baseline = before["equity"] if before else equity["summary"].get("initial_capital")
            fold_return = ((fold_curve[-1]["equity"] / baseline - 1) * 100
                           if fold_complete and baseline else None)
            folds.append({
                "train_start": train_dates[0], "train_end": train_dates[-1],
                "validation_start": valid_dates[0], "validation_end": valid_dates[-1],
                "data_version_hashes": daily_versions,
                "factor_ic": factor_ic,
                "classification": "prospective_oos" if prospective else "historical_descriptive",
                "coverage_warning": "缺少当日收盘价时净值与回撤不作完整结论；历史回放若未关联信号ID，不计入成交漏斗",
                "account_equity": {"complete": fold_complete, "return_pct": round(fold_return, 4) if fold_return is not None else None,
                                   "days": len(fold_curve), "valued_days": sum(row["valuation_complete"] for row in fold_curve)},
                "metrics": summary,
            })
        report.update({
            "start": start, "end": end, "train_days": train_days,
            "validation_days": validation_days, "factor_and_configuration_unchanged": versions_match,
            "folds": folds, "status": "evaluated",
            "account_key": account_key, "equity": equity,
            "warning": "历史回填不自动获得样本外资格；仅冻结之后且时点数据齐全的窗口标记 prospective_oos。",
        })
        self.ledger.save_experiment(experiment_id, frozen["frozen_at"],
                                    frozen["configuration_hash"], frozen["factor_hash"], report)
        return report
