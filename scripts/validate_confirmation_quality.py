"""Run a registered research suite against an existing sealed baseline."""
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
from backtest.trade_calendar import TradeCalendar
from config.settings import CACHE_DIR, TUSHARE_TOKEN
from core.data import DataManager
from core.operations.replay_research import (
    FrozenCalendar,
    ReplayResearch,
    ResearchSpec,
    atomic_json,
    digest,
    experiment_lock,
    research_arms,
)
from scripts.validate_research_baseline import prefetch_holding_evidence


def sealed_source(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if digest({k: v for k, v in manifest.items() if k != "contract_hash"}) != manifest["contract_hash"]:
        raise ValueError("源基准合同已被修改")
    for name, expected in manifest["files"].items():
        file = directory / name
        if not file.resolve().is_relative_to(directory.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"源基准证据已变化：{name}")
    return manifest


def source_plans(directory, manifest):
    arm = "baseline" if "baseline" in manifest["arms"] else "strategy_quality"
    folder = directory / "plans" / arm
    if arm not in manifest["arms"] or not folder.is_dir():
        raise ValueError("源实验缺少完整候选控制组目录，不能视为零候选")
    calendar = FrozenCalendar(manifest["calendar"])
    for date in manifest["dates"]:
        file = folder / f"交易计划_{calendar.prev(date)}.csv"
        if file.relative_to(directory).as_posix() not in manifest["files"] or not file.is_file():
            raise ValueError(f"源候选缺少冻结覆盖证据：{date}")
    return folder


def execute(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(root / "run.log", encoding="utf-8")
    source = sealed_source(args.baseline.resolve())
    plans = source_plans(args.baseline.resolve(), source)
    service = ReplayResearch()
    calendar = TradeCalendar()
    started = time.time()

    def progress(row):
        value = {**row, "elapsed_seconds": round(time.time() - started, 1)}
        atomic_json(root / "progress.json", value)
        if row.get("completed", 0) % 5 == 0 or "completed" not in row:
            print(json.dumps(value, ensure_ascii=False), flush=True)

    marker = root / "experiment.json"
    attempts = []
    manifest = None
    if marker.exists():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if saved["source_contract_hash"] != source["contract_hash"]:
            raise ValueError("不能将另一源基准接入当前验证")
        manifest = service.verify(saved["experiment_id"])
        attempts = saved["attempts"]

    local = DataManager("", CACHE_DIR, allow_remote_history=False)
    suite = getattr(args, "suite", "confirmation_quality")
    spec = ResearchSpec(**{**source["spec"], "suite": suite, "optimization": source["spec"].get("optimization", {}),
                           "missing_evidence_policy": "strict"})
    if manifest is not None and manifest["spec"]["suite"] != suite:
        raise ValueError("当前输出目录属于另一对照方案，请使用新目录")
    for round_number in range(5):
        if manifest is None:
            manifest = service.freeze(plans_dir=plans, start=source["start"], end=source["end"],
                data_manager=local, calendar=calendar, spec=spec, config=BacktestConfig(**source["base_config"]),
                coverage_dates=[calendar.prev(d) for d in source["dates"]], regimes=source["regimes"],
                profile_snapshot=source["profiles"], factor_db_path=args.private / "factors.duckdb",
                amount_profile_path=args.private / "minute_amount_profiles.json", progress=progress)
            attempts.append(manifest["experiment_id"])
            atomic_json(marker, {"source_experiment_id": source["experiment_id"],
                "source_contract_hash": source["contract_hash"], "experiment_id": manifest["experiment_id"],
                "attempts": attempts})
        progress({"stage": f"开始{len(research_arms(spec))}组完整区间", "experiment_id": manifest["experiment_id"], "round": round_number + 1})
        report = service.run(manifest["experiment_id"], progress=progress)
        failures = [row for value in report["results"].values() for row in value["blocked"]]
        atomic_json(root / "validation_summary.json", {"experiment_id": manifest["experiment_id"],
            "source_experiment_id": source["experiment_id"], "status": report["status"],
            "start": report["start"], "end": report["end"], "dates": len(report["dates"]),
            "spec": report["spec"], "results": report["results"], "folds": report["folds"],
            "paired_comparisons": report["paired_comparisons"], "release_allowed": False,
            "production_account_changed": False, "parameter_changes": False,
            "classification": report["classification"], "unseen_validation_required": suite in {"return_refinement", "in_sample_tuning"}})
        if report["status"] == "complete":
            directory = service._directory(manifest["experiment_id"])
            checkpoints = {p: p.read_bytes() for p in (directory / "states").glob("*.json")}
            if len(checkpoints) != len(research_arms(spec)):
                raise RuntimeError("缺少独立账户检查点")
            service.run(manifest["experiment_id"])
            if any(p.read_bytes() != value for p, value in checkpoints.items()):
                raise RuntimeError("重复运行改变了已完成检查点")
            atomic_json(root / "resume_audit.json", {"experiment_id": manifest["experiment_id"],
                "checkpoints": len(checkpoints), "byte_identical_after_repeat": True})
            progress({"stage": "完整区间及滚动验证完成", "experiment_id": manifest["experiment_id"],
                      "status": "complete", "folds": len(report["folds"])})
            return
        if not args.fetch_missing or round_number == 4:
            raise RuntimeError(f"验证缺证据：{failures}")
        if not failures or any(row.get("evidence") != "exit_minute_bars" or row.get("scope") != "holding" for row in failures):
            raise RuntimeError(f"缺失不是可自动补取的持仓分钟，需检查：{failures}")
        remote = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
        directory = service._directory(manifest["experiment_id"])
        preparation = prefetch_holding_evidence(manifest, directory, remote, notify=progress,
                                               checkpoint_dir=directory / "states")
        atomic_json(root / f"repairs_round_{round_number + 1}.json", {**preparation,
                   "sealed_inputs_modified": False, "next_action": "freeze_new_experiment"})
        if preparation["blocked"]:
            raise RuntimeError(f"持仓证据准备仍有缺口：{preparation['blocked']}")
        manifest = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fetch-missing", action="store_true")
    parser.add_argument("--suite", choices=["confirmation_quality", "return_refinement", "in_sample_tuning"], default="confirmation_quality")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with experiment_lock(args.output / "validation.lock"):
        execute(args)


if __name__ == "__main__":
    main()
