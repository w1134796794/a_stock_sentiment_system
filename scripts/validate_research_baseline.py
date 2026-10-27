"""Rebuild an isolated historical baseline and audit real evidence retrieval."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.data.minute_cache_files import minute_cache_exists, read_minute_csv

import duckdb
from core.infrastructure.duckdb_access import connect as duckdb_connect
import pandas as pd
from loguru import logger

from backtest.backtest_engine import BacktestConfig, BacktestEngine
from backtest.evidence_policy import ReplayEvidenceBlocked
from backtest.minute_entry import normalize_minute_bars
from backtest.plan_source import build_backtest_plan_dir
from backtest.trade_calendar import TradeCalendar
from config.settings import CACHE_DIR, FACTOR_DB_PATH, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
from core.data import DataManager
from core.data.minute_quality import inspect_minute_bars
from core.etl.daily_pipeline import ETLDailyPipeline
from core.operations.replay_research import (
    ARMS,
    FrozenEvidence,
    ReplayResearch,
    ResearchEngine,
    ResearchSpec,
    atomic_json,
    digest,
    experiment_lock,
    research_engine,
)
from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS, StrategyProfileRepository
from core.signals.minute_amount_profile import (
    MinuteAmountProfileRepository,
    MinuteAmountProfileTrainer,
    validate_historical_profile,
)
from core.utils.sector_names import normalize_sector_names
from core.utils.stock_code_utils import StockCodeUtils
from core.utils.stock_eligibility import new_entry_exclusion
from risk.risk_config import RiskConfig


def progress(root, **row):
    try:
        atomic_json(root / "progress.json", row)
    except PermissionError as exc:
        logger.warning(f"进度文件暂时无法替换，继续执行；账户检查点仍严格保存: {exc}")
    print(json.dumps(row, ensure_ascii=False), flush=True)


def requirements(plan_dir, dates, calendar, db):
    required = set()
    for date in dates:
        file = plan_dir / f"交易计划_{calendar.prev(date)}.csv"
        if not file.exists():
            continue
        frame = pd.read_csv(file, dtype={"代码": str})
        sectors = {str(row["代码"]).zfill(6): set(normalize_sector_names(row.get("共振板块")))
                   for row in frame.to_dict("records")}
        peers = BacktestEngine._load_sector_peer_codes(date, sectors, duckdb_path=db)
        required.update((date, code) for code in sectors)
        required.update((date, code) for values in peers.values() for code in values)
    return sorted(required)


def recover_holding_minutes(report, dm):
    requests = {(str(row.get("trade_date")), str(row.get("code")))
                for result in report.get("results", {}).values() for row in result.get("blocked", [])
                if row.get("scope") == "holding" and row.get("evidence") == "exit_minute_bars"}
    recovered = []
    for date, code in sorted(requests):
        canonical = StockCodeUtils.standardize_code(code, add_suffix=True)
        frame = dm.get_stock_tick(canonical, date, require_full_day=True)
        if inspect_minute_bars(frame, date, expected_code=canonical)["valid"]:
            recovered.append({"date": date, "code": code})
    return recovered


def prefetch_holding_evidence(manifest, directory, dm, *, notify=lambda row: None,
                             checkpoint_dir=None):
    """Preparation only: discover actual exits without changing sealed evidence or accounts."""
    extra, attempted, blocked, resumed = {}, set(), [], {}

    class PreparationEvidence(FrozenEvidence):
        def get_stock_tick(self, code, date):
            key = (str(date), str(code).split(".")[0].zfill(6))
            return extra[key].copy() if key in extra else super().get_stock_tick(code, date)

    for arm in manifest.get("arms", ARMS):
        factory = research_engine if manifest.get("spec", {}).get("suite", "legacy") != "legacy" else ResearchEngine
        engine = factory(PreparationEvidence(directory), BacktestConfig(**manifest["base_config"]), manifest, arm=arm)
        completed_date = ""
        checkpoint_file = checkpoint_dir / f"{arm}.json" if checkpoint_dir else None
        if checkpoint_file and checkpoint_file.exists():
            checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            if checkpoint.get("contract_hash") != manifest["contract_hash"]:
                raise ValueError("持仓证据准备检查点不属于当前冻结合同")
            if checkpoint.get("state_hash") != digest(checkpoint["state"]):
                raise ValueError("持仓证据准备检查点已损坏")
            engine.import_state(checkpoint["state"])
            nav = checkpoint["state"].get("daily_nav", [])
            completed_date = str(nav[-1]["date"]) if nav else ""
            resumed[arm] = completed_date
        for index, date in enumerate(manifest["dates"]):
            if date <= completed_date:
                continue
            notify({"stage": "预演补齐实际持仓退出分钟", "arm": arm, "trade_date": date,
                    "completed": index, "total": len(manifest["dates"]), "downloaded": len(extra)})
            snapshot = engine.export_state()
            completed = False
            for _ in range(8):
                try:
                    engine.run_one_day(date, str(directory / "plans" / arm))
                    completed = True
                    break
                except ReplayEvidenceBlocked as exc:
                    engine.import_state(snapshot)
                    rows = exc.details
                    if not rows or any(row.get("scope") != "holding"
                                       or row.get("evidence") != "exit_minute_bars" for row in rows):
                        blocked.extend(rows)
                        break
                    recovered = False
                    for row in rows:
                        key = (str(row["trade_date"]), str(row["code"]).split(".")[0].zfill(6))
                        if key not in attempted:
                            attempted.add(key)
                            canonical = StockCodeUtils.standardize_code(key[1], add_suffix=True)
                            frame = dm.get_stock_tick(canonical, key[0], require_full_day=True)
                            if inspect_minute_bars(frame, key[0], expected_code=canonical)["valid"]:
                                extra[key] = frame
                        if key in extra:
                            engine._exit_minute_frames.pop(key, None)
                            recovered = True
                        else:
                            blocked.append(row)
                    if any((str(row["trade_date"]), str(row["code"]).split(".")[0].zfill(6))
                           not in extra for row in rows) or not recovered:
                        break
            if not completed:
                if not blocked:
                    blocked.append({"trade_date": date, "scope": "holding", "reason": "持仓证据准备重试超过上限"})
                break
    return {"classification": "preparation_only_not_performance", "recovered": [
        {"date": date, "code": code} for date, code in sorted(extra)], "blocked": blocked,
        "resumed_checkpoints": resumed}


def factor_status_ready(root, date):
    file = root / "factor_status" / f"factors_{date}.json"
    try:
        status = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(status, dict):
        return False
    jobs = status.get("jobs", [])
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        return False
    required_jobs = {"market_factor_job", "lhb_factor_job", "short_signal_factors",
                     "sector_factor_job", "stock_factor_job"}
    return (status.get("trade_date") == date and len(jobs) == len(required_jobs)
            and {job.get("name") for job in jobs} == required_jobs
            and all(job.get("ok") is True and job.get("trade_date") == date for job in jobs))


def validate_repair(root, dates, *, strategy_versions=None):
    checkpoint_file = root / "repair_checkpoint.json"
    checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8")) if checkpoint_file.exists() else {}
    missing = sorted(set(dates) - set(checkpoint.get("completed", [])))
    if missing:
        raise RuntimeError(f"独立补建未完成: {', '.join(missing)}")
    for date in dates:
        if not factor_status_ready(root, date):
            raise RuntimeError(f"{date} 因子任务状态不完整或日期不一致")
        for strategy in PRODUCTION_STRATEGY_IDS:
            file = root / "screening/combinations" / strategy / f"screening_{date}.json"
            row = json.loads(file.read_text(encoding="utf-8"))
            if (row.get("trade_date") != date or row.get("ok") is not True
                    or not isinstance(row.get("final"), list) or row.get("final_count") != len(row["final"])):
                raise RuntimeError(f"{date} / {strategy} 筛选产物不完整或日期不一致")
            if strategy_versions is not None and row.get("strategy_version") != strategy_versions.get(strategy):
                raise RuntimeError(f"{date} / {strategy} 策略版本不一致，请在同配置下重新补建")
        pool = json.loads((root / "screening/decision_pool" / f"decision_pool_{date}.json").read_text(encoding="utf-8"))
        snapshot = json.loads((root / "snapshots" / f"{date}.json").read_text(encoding="utf-8"))
        if pool.get("trade_date") != date or snapshot.get("meta", {}).get("date") != date:
            raise RuntimeError(f"{date} 决策池与快照日期不一致")


def reconcile_prices(evidence_file):
    checked, unavailable, mismatches = 0, 0, []
    with sqlite3.connect(evidence_file.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT d.code,d.date,d.payload,m.payload FROM evidence d "
            "LEFT JOIN evidence m ON m.kind='minutes' AND m.code=d.code AND m.date=d.date WHERE d.kind='daily'")
        for code, date, daily, minutes in rows:
            close = float(json.loads(daily).get("close") or 0)
            if close <= 0 or not minutes or minutes in {"[]", "null"}:
                unavailable += 1
                continue
            frame = normalize_minute_bars(pd.DataFrame(json.loads(minutes or "[]")))
            if close <= 0 or frame.empty or frame.iloc[-1]["time"] != "15:00:00":
                unavailable += 1
                continue
            minute_close = float(frame.iloc[-1]["close"])
            checked += 1
            if abs(close - minute_close) > 0.01001:
                mismatches.append({"date": date, "code": code, "daily_close": close, "minute_close": minute_close})
    return {"checked": checked, "unavailable": unavailable, "mismatches": mismatches,
            "scope": "paired_close_prices_only; completeness_is_checked_separately"}


def prepare_amount_profile(root, replay_start, calendar, dm, db):
    file = root / "minute_amount_profiles.json"
    repository = MinuteAmountProfileRepository(file)
    try:
        validate_historical_profile(repository.load(), dm.stock_dir / "tick", replay_start)
        return file
    except (ValueError, OSError):
        pass
    cutoff = calendar.prev(replay_start)
    dates = calendar.get_trade_dates("20260101", cutoff)[-5:]
    with duckdb_connect(str(db), read_only=True) as connection:
        stocks = connection.execute(
            "SELECT code,name,amount_yuan FROM stock_daily_silver WHERE CAST(trade_date AS VARCHAR)=? "
            "AND amount_yuan>0 ORDER BY amount_yuan,code", [cutoff]).fetchdf()
    stocks = stocks.loc[[not new_entry_exclusion(row) for row in stocks.to_dict("records")]].drop_duplicates("code")
    if stocks.empty or not dates:
        raise RuntimeError("区间前缺少股票全集或交易日，不能补建成交进度模型")
    indices = sorted({index * (len(stocks) - 1) // 59 for index in range(60)})
    codes = stocks.iloc[indices]["code"].astype(str).str.zfill(6).tolist()
    missing = []
    for index, (date, code) in enumerate((date, code) for date in dates for code in codes):
        progress(root, stage="补建区间前成交进度曲线", date=date, code=code,
                 completed=index, total=len(dates) * len(codes))
        canonical = StockCodeUtils.standardize_code(code, add_suffix=True)
        frame = dm.get_stock_tick(canonical, date, require_full_day=True)
        if not inspect_minute_bars(frame, date, expected_code=canonical)["valid"]:
            missing.append({"date": date, "code": code, "reason": dm.get_minute_quality_report(canonical, date)})
    result = MinuteAmountProfileTrainer(cache_dir=dm.stock_dir / "tick", repository=repository).train_historical(
        start_date=dates[0], end_date=cutoff, codes=codes)
    atomic_json(root / "amount_profile_training.json", {"universe_as_of": cutoff, "universe": codes,
        "dates": dates, "missing": missing, "ok": result.get("ok"), "sample_days": result.get("sample_days"),
        "model_data_end": (result.get("historical_training") or {}).get("end")})
    if not result.get("ok"):
        raise RuntimeError(f"历史成交进度模型样本不足: {result}")
    validate_historical_profile(repository.load(), dm.stock_dir / "tick", replay_start)
    return file


def repair(root, dates, calendar, *, recompute_all):
    db = root / "factors.duckdb"
    if not db.exists():
        progress(root, stage="复制独立研究数据库")
        shutil.copy2(FACTOR_DB_PATH, db)
    history_start = calendar.get_trade_dates("20260101", dates[0])[-21]
    for name in ("etl_quality", "fetch_status", "screening", "factor_status"):
        source = Path(WEB_DATA_DIR) / name
        for file in source.rglob("*.json"):
            stamp = file.stem.rsplit("_", 1)[-1]
            if len(stamp) != 8 or not history_start <= stamp <= dates[-1]:
                continue
            target = root / name / file.relative_to(source)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
    snapshot_dir = root / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    for date in dates:
        file = Path(SNAPSHOT_DIR) / f"{date}.json"
        target = snapshot_dir / file.name
        if file.exists() and not target.exists():
            shutil.copy2(file, target)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
    pipeline = ETLDailyPipeline(dm, duckdb_path=db, web_data_dir=root,
                               snapshot_dir=snapshot_dir, app_db_path=root / "app.sqlite")
    checkpoint_file = root / "repair_checkpoint.json"
    checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8")) if checkpoint_file.exists() else {"completed": []}
    profiles = StrategyProfileRepository().frozen()
    versions = {key: profiles.get_profile(key)["version"] for key in PRODUCTION_STRATEGY_IDS}
    for index, date in enumerate(dates):
        if date in checkpoint["completed"]:
            if not recompute_all:
                try:
                    validate_repair(root, [date], strategy_versions=versions)
                except (OSError, ValueError, RuntimeError):
                    pass
                else:
                    continue
            checkpoint["completed"].remove(date)
            atomic_json(checkpoint_file, checkpoint)
        progress(root, stage="重算因子与五策略", date=date, completed=index, total=len(dates))
        if recompute_all or not factor_status_ready(root, date):
            result = pipeline.compute_factors(date, calendar.prev(date))
            if not result.ok:
                raise RuntimeError(f"{date} 因子计算失败: {result.factor_results}")
        result = pipeline.run_screening(date, calendar.prev(date), strategy_ids=list(PRODUCTION_STRATEGY_IDS),
                                        primary_strategy="mainline_leader", archive_cases=False)
        if not result.ok or result.warnings:
            raise RuntimeError(f"{date} 筛选不完整: {result.warnings}")
        checkpoint["completed"].append(date)
        atomic_json(checkpoint_file, checkpoint)
    progress(root, stage="重算完成", completed=len(dates), total=len(dates))


def execute(root, start, end, calendar, *, fetch, skip_candidates):
    db = root / "factors.duckdb"
    dates = calendar.get_trade_dates(start, end)
    progress(root, stage="检查补建与策略版本", start=start, end=end)
    profiles = StrategyProfileRepository().frozen()
    frozen_profiles = {key: profiles.get_profile(key) for key in PRODUCTION_STRATEGY_IDS}
    strategy_versions = {key: profile["version"] for key, profile in frozen_profiles.items()}
    validate_repair(root, [calendar.prev(dates[0]), *dates], strategy_versions=strategy_versions)
    progress(root, stage="生成T-1生产决策计划", start=start, end=end)
    plan_dir, files, rows = build_backtest_plan_dir(snapshot_dir=root / "snapshots", output_dir=root / "plans",
        screening_dir=root / "screening", start_date=start, end_date=end, max_rank=0,
        strategy_ids=PRODUCTION_STRATEGY_IDS)
    requests = requirements(plan_dir, dates, calendar, db)
    progress(root, stage="审计分钟完整性", completed=0, total=len(requests), plan_rows=rows)
    dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=fetch)
    failures, downloaded = [], 0
    for index, (date, code) in enumerate(requests):
        if index % 25 == 0:
            progress(root, stage="审计分钟完整性", completed=index, total=len(requests),
                     downloaded=downloaded, missing=len(failures))
            atomic_json(root / "minute_audit.json", {"status": "running", "processed": index,
                "requirements": len(requests), "downloaded": downloaded, "missing": failures})
        canonical = StockCodeUtils.standardize_code(code, add_suffix=True)
        file = Path(CACHE_DIR) / "stock/tick" / f"{canonical}_{date}.csv"
        try:
            valid = minute_cache_exists(file) and inspect_minute_bars(read_minute_csv(file), date, expected_code=canonical)["valid"]
        except (ValueError, OSError):
            valid = False
        if not valid:
            progress(root, stage="补取分钟", date=date, code=code, completed=index, total=len(requests))
            frame = dm.get_stock_tick(canonical, date, require_full_day=True)
            valid = inspect_minute_bars(frame, date, expected_code=canonical)["valid"]
            downloaded += int(valid)
            if not valid:
                failures.append({"date": date, "code": code, "evidence": "minutes",
                                 "reason": dm.get_minute_quality_report(canonical, date)})
    atomic_json(root / "minute_audit.json", {"status": "complete", "processed": len(requests),
        "requirements": len(requests), "downloaded": downloaded, "missing": failures})
    amount_profile = prepare_amount_profile(root, start, calendar, dm, db)
    current = StrategyProfileRepository().frozen()
    if any(current.get_profile(key)["version"] != version for key, version in strategy_versions.items()):
        raise RuntimeError("补取数据期间策略配置发生改变，请重新冻结")
    config = BacktestConfig.from_risk_config(RiskConfig.load(), initial_capital=1_000_000, risk_control=True)
    config.rotation_enabled = True
    regimes = {}
    for date in dates:
        pool = json.loads((root / "screening/decision_pool" / f"decision_pool_{calendar.prev(date)}.json").read_text(encoding="utf-8"))
        regimes[date] = f"{pool.get('regime_label') or pool.get('regime')} / {pool.get('emotion_phase_label') or pool.get('emotion_phase')}"
    service = ReplayResearch()
    attempts = []
    for round_number in range(5):
        manifest = service.freeze(plans_dir=plan_dir, start=start, end=end, data_manager=dm, calendar=calendar,
            spec=ResearchSpec(missing_evidence_policy="skip_candidates" if skip_candidates else "strict"),
            config=config, coverage_dates=[calendar.prev(d) for d in dates], regimes=regimes,
            profile_snapshot=frozen_profiles, factor_db_path=db, amount_profile_path=amount_profile,
            progress=lambda row: progress(root, **row))
        attempts.append(manifest["experiment_id"])
        atomic_json(root / "experiment.json", {"experiment_id": manifest["experiment_id"], "attempts": attempts,
                                               "plan_files": files, "plan_rows": rows})
        price_audit = reconcile_prices(service._directory(manifest["experiment_id"]) / "evidence.sqlite")
        atomic_json(root / "price_audit.json", price_audit)
        if price_audit["mismatches"]:
            progress(root, stage="日线与分钟价格口径不一致", mismatches=price_audit["mismatches"])
            raise RuntimeError("价格口径校验失败，不能将本实验发布为可信基准")
        report = service.run(manifest["experiment_id"], progress=lambda row: progress(root, **row))
        if report["status"] == "complete" or not fetch or round_number == 4:
            break
        preparation = prefetch_holding_evidence(manifest, service._directory(manifest["experiment_id"]), dm,
                                               notify=lambda row: progress(root, **row))
        atomic_json(root / "holding_evidence_preparation.json", preparation)
        recovered = preparation["recovered"]
        if not recovered:
            break
        progress(root, stage="补齐持仓分钟后重新冻结", recovered=recovered)
    summary = {"experiment_id": manifest["experiment_id"], "status": report["status"],
               "results": {arm: {key: value.get(key) for key in ("summary", "blocked", "data_quality", "evidence_status")}
                           for arm, value in report["results"].items()}, "folds": report.get("folds"),
               "parameter_changes": False, "production_account_changed": False}
    atomic_json(root / "validation_summary.json", summary)
    progress(root, stage="验证完成", status=report["status"], experiment_id=manifest["experiment_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["repair", "run"])
    parser.add_argument("--start", default="20260701")
    parser.add_argument("--end", default="20261009")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--recompute-all", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--skip-candidates", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.root.resolve() == Path(WEB_DATA_DIR).resolve():
        raise ValueError("必须使用独立研究目录")
    calendar = TradeCalendar()
    dates = calendar.get_trade_dates(args.start, args.end)
    if not dates or dates[0] != args.start or dates[-1] != args.end:
        raise ValueError("区间必须为有效交易日")
    logger.remove()
    logger.add(args.root / f"{args.stage}.log", encoding="utf-8")
    with experiment_lock(args.root / "validation.lock"):
        if args.stage == "repair":
            repair(args.root, [calendar.prev(dates[0]), *dates], calendar, recompute_all=args.recompute_all)
        else:
            execute(args.root, args.start, args.end, calendar, fetch=args.fetch, skip_candidates=args.skip_candidates)


if __name__ == "__main__":
    main()
