"""Seal three-strategy inputs, run three sizing arms, and audit rolling results."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger

from backtest.backtest_engine import BacktestConfig
from backtest.plan_source import build_backtest_plan_dir
from backtest.trade_calendar import TradeCalendar
from config.settings import CACHE_DIR, FACTOR_DB_PATH, OUTPUT_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
from core.data import DataManager
from core.operations.replay_research import ReplayResearch, ResearchSpec, atomic_json, digest, experiment_lock
from core.portfolio.holding_repository import HoldingRepository
from core.screening.strategy_profiles import StrategyProfileRepository
from scripts.validate_research_baseline import prefetch_holding_evidence

STRATEGIES = ["mainline_leader", "weak_to_strong", "first_board_launch"]


def protected_state(repository):
    with repository._connect() as conn:
        accounts = {table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("portfolio_accounts", "portfolio_positions", "portfolio_trades")}
    return digest({"accounts": accounts, "profiles": StrategyProfileRepository().frozen().list_profiles()})


def execute(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(root / "run.log", encoding="utf-8")
    source_dir = Path(OUTPUT_DIR) / "backtest_results"
    source_file = source_dir / f"backtest_manifest_{args.source_run_id}.json"
    source = json.loads(source_file.read_text(encoding="utf-8"))
    start, end = str(source["start_date"]), str(source["end_date"])
    service, repository = ReplayResearch(), HoldingRepository()
    profiles = StrategyProfileRepository().frozen()
    if set(profiles.default_selection()) != set(STRATEGIES):
        raise ValueError("The three enabled strategies changed; do not mix another selection")
    before = protected_state(repository)
    calendar = TradeCalendar()
    dates = calendar.get_trade_dates(start, end)
    started = time.time()

    def progress(row):
        value = {**row, "elapsed_seconds": round(time.time() - started, 1)}
        atomic_json(root / "progress.json", value)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    marker = root / "experiment.json"
    saved = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    if saved and saved["source_sha256"] != hashlib.sha256(source_file.read_bytes()).hexdigest():
        raise ValueError("The source replay manifest changed")
    manifest = service.verify(saved["experiment_id"]) if saved else None
    attempts = saved["attempts"] if saved else []
    coverage, regimes = [], {}
    for date in dates:
        day = calendar.prev(date)
        if not (Path(SNAPSHOT_DIR) / f"{day}.json").is_file():
            raise ValueError(f"Missing candidate-day snapshot: {day}")
        for strategy in STRATEGIES:
            file = Path(WEB_DATA_DIR) / "screening/combinations" / strategy / f"screening_{day}.json"
            payload = json.loads(file.read_text(encoding="utf-8"))
            if payload.get("trade_date") != day or payload.get("ok") is not True or not isinstance(payload.get("final"), list):
                raise ValueError(f"Invalid screening coverage: {day}/{strategy}")
        coverage.append(day)
        pool_file = Path(WEB_DATA_DIR) / "screening/decision_pool" / f"decision_pool_{day}.json"
        pool = json.loads(pool_file.read_text(encoding="utf-8")) if pool_file.exists() else {}
        regimes[date] = f"{pool.get('regime_label') or pool.get('regime') or 'unknown'} / {pool.get('emotion_phase_label') or pool.get('emotion_phase') or 'unknown'}"
    spec = ResearchSpec(suite="position_risk", missing_evidence_policy="strict")
    plan_dir = root / "plans" / ("backtest_trade_plans_baseline_strategies_" + "-".join(STRATEGIES))
    if not plan_dir.exists() or not (root / "plan_audit.json").is_file():
        progress({"stage": "准备共同三策略候选", "completed": 0, "total": len(dates)})
        plan_dir, files, rows = build_backtest_plan_dir(snapshot_dir=Path(SNAPSHOT_DIR), output_dir=root / "plans",
            screening_dir=Path(WEB_DATA_DIR) / "screening", start_date=start, end_date=end,
            max_rank=0, strategy_ids=STRATEGIES)
        atomic_json(root / "plan_audit.json", {"files": files, "rows": rows, "strategies": STRATEGIES})
    local = DataManager("", CACHE_DIR, allow_remote_history=False)
    for round_number in range(5):
        if manifest is None:
            manifest = service.freeze(plans_dir=plan_dir, start=start, end=end, data_manager=local,
                calendar=calendar, spec=spec, config=BacktestConfig(**source["backtest_config"]),
                coverage_dates=coverage, regimes=regimes,
                profile_snapshot={sid: profiles.get_profile(sid) for sid in STRATEGIES},
                factor_db_path=FACTOR_DB_PATH, amount_profile_path=args.amount_profile, progress=progress)
            attempts.append(manifest["experiment_id"])
            atomic_json(marker, {"source_run_id": args.source_run_id,
                "source_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "experiment_id": manifest["experiment_id"], "attempts": attempts,
                "input_correction": "共用区间前成交进度模型；旧生产模型无区间前可用证明，不沿用旧收益"})
        report = service.run(manifest["experiment_id"], progress=progress)
        if report["status"] == "complete":
            break
        failures = [r for result in report["results"].values() for r in result["blocked"]]
        if (not args.fetch_missing or round_number == 4 or not failures
                or any(r.get("scope") != "holding" or r.get("evidence") != "exit_minute_bars" for r in failures)):
            raise RuntimeError(f"Strict evidence blocked: {failures}")
        remote = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
        directory = service._directory(manifest["experiment_id"])
        preparation = prefetch_holding_evidence(manifest, directory, remote, notify=progress,
                                                checkpoint_dir=directory / "states")
        atomic_json(root / f"repair_round_{round_number + 1}.json", preparation)
        if preparation["blocked"]:
            raise RuntimeError(f"Evidence repair blocked: {preparation['blocked']}")
        manifest = None
    directory = service._directory(manifest["experiment_id"])
    checkpoints = {p: p.read_bytes() for p in (directory / "states").glob("*.json")}
    if len(checkpoints) != 3:
        raise RuntimeError("Missing independent account checkpoints")
    service.run(manifest["experiment_id"])
    if any(p.read_bytes() != raw for p, raw in checkpoints.items()):
        raise RuntimeError("Repeated execution changed completed checkpoints")
    plan_hashes = {arm: digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (directory / "plans" / arm).glob("*.csv")}) for arm in manifest["arms"]}
    if len(set(plan_hashes.values())) != 1 or before != protected_state(repository):
        raise RuntimeError("Candidate inputs or production account/configuration changed")
    for arm, result in report["results"].items():
        state = json.loads((directory / result["state_path"]).read_text(encoding="utf-8"))["state"]
        if any(row.get("strategy_id") not in STRATEGIES for row in state["trade_history"]):
            raise RuntimeError("Disabled strategy entered comparison")
    summary = {"experiment_id": manifest["experiment_id"], "source_run_id": args.source_run_id,
        "status": report["status"], "start": start, "end": end, "days": len(dates),
        "source_amount_model_replaced_for_causality": True,
        "classification": report["classification"], "unseen_validation_required": True,
        "production_account_and_profiles_unchanged": True, "identical_candidate_plan_hashes": plan_hashes,
        "resume_checkpoints_byte_identical": True, "max_positions_all_arms": 3,
        "results": report["results"], "folds": report["folds"], "paired_comparisons": report["paired_comparisons"],
        "release_allowed": False, "elapsed_seconds": round(time.time() - started, 1)}
    atomic_json(root / "validation_summary.json", summary)
    progress({"stage": "三组仓位及滚动验证完成", "status": "complete", "experiment_id": manifest["experiment_id"],
              "folds": len(report["folds"])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--amount-profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fetch-missing", action="store_true")
    args = parser.parse_args()
    with experiment_lock(args.output.resolve() / "validation.lock"):
        execute(args)


if __name__ == "__main__":
    main()
