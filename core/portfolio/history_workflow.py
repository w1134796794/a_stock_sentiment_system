"""Explicit, persistent prepare/prefetch/seal/run/reconcile historical jobs."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.infrastructure.atomic_json import atomic_write_json as _write_json
from core.infrastructure.file_mutex import file_mutex
from core.operations.replay_research import clean
from core.portfolio.history_evidence import (
    FrozenHistory,
    audit_day,
    code_version,
    pack_inputs,
    read_packet,
    write_packet,
)
from core.realtime.market_readiness import calendar_day
from core.realtime.trading_session import shanghai_now

PHASES = ("prepare", "prefetch", "freeze", "run", "reconcile")
_PROCESSES = {}


def atomic_write_json(path, value, **kwargs):
    return _write_json(path, clean(value), **kwargs)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def calendar_path():
    from config.settings import BASE_DIR, TRADE_CALENDAR_FILE

    path = Path(TRADE_CALENDAR_FILE)
    return path if path.is_absolute() else Path(BASE_DIR) / path


def trading_dates(start, end):
    from core.realtime.market_readiness import at

    path = calendar_path()
    for value in (start, end):
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError("日期必须为YYYYMMDD")
        at(value, "00:00:00")
        if not calendar_day(path, value)[0]:
            raise ValueError("起止日期必须是真实交易日")
    if start > end or end >= shanghai_now().strftime("%Y%m%d"):
        raise ValueError("仅允许已结束的历史区间，不包含今天或未来日期")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        dates = sorted(
            {
                row["cal_date"].replace("-", "")
                for row in csv.DictReader(stream)
                if row.get("exchange", "SSE") in {"", "SSE"} and row["is_open"] == "1"
            }
        )
    selected = [date for date in dates if start <= date <= end]
    if dates.index(start) == 0:
        raise ValueError("真实日历缺少首日的前一交易日")
    if len(selected) > 60:
        raise ValueError("单个任务最多60个交易日，请分段准备")
    return dates, selected


class HistoryWorkflows:
    def __init__(self, root=None):
        from config.settings import WEB_DATA_DIR

        self.root = Path(root or Path(WEB_DATA_DIR) / "history_workflows")

    def directory(self, ident):
        if not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise ValueError("历史任务ID无效")
        return self.root / ident

    def get(self, ident):
        process = _PROCESSES.get(ident)
        if process is not None and process.poll() is not None:
            _PROCESSES.pop(ident, None)
        value = read_json(self.directory(ident) / "status.json")
        if value["state"] in {"running", "queued"}:
            age = (shanghai_now() - datetime.fromisoformat(value["updated_at"])).total_seconds()
            if age > 30:
                try:
                    with file_mutex(self.directory(ident) / "run.lock", timeout=0):
                        value.update(state="interrupted", error="子进程已结束或服务已重启，可重新执行当前阶段")
                except (OSError, TimeoutError):
                    pass
        return value

    def recent(self):
        if not self.root.exists():
            return []
        files = sorted(self.root.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        return [self.get(path.parent.name) for path in files]

    def create(self, start, end, capital):
        calendar, dates = trading_dates(start, end)
        capital = float(capital)
        if not math.isfinite(capital) or capital <= 0:
            raise ValueError("初始资金必须为有限正数")
        ident = uuid4().hex
        directory = self.directory(ident)
        value = dict(
            job_id=ident,
            start=start,
            end=end,
            dates=dates,
            calendar=calendar,
            capital=capital,
            state="idle",
            phase="",
            stages={},
            frozen_version=None,
            account_modified=False,
            notification_sent=False,
            classification="historical_minute_research",
            updated_at=shanghai_now().isoformat(),
            error="",
            failures=[],
        )
        atomic_write_json(directory / "status.json", value, indent=2)
        return value

    def _require(self, value, phase):
        if phase not in PHASES:
            raise ValueError("未知历史任务阶段")
        index = PHASES.index(phase)
        if index and value["stages"].get(PHASES[index - 1]) != "complete":
            raise ValueError("前一阶段尚未完成，不能跳过校验或冻结")
        if phase == "prefetch" and value.get("pool_scope_contract") != "read_only_saved_pools_v1":
            raise ValueError("请先重新读取历史池名单；旧版历史重建准备不再用于补数")

    def start(self, ident, phase):
        directory = self.directory(ident)
        with file_mutex(directory / "run.lock", timeout=0):
            value = self.get(ident)
            if value["state"] in {"running", "queued"}:
                raise ValueError("该任务已有阶段在运行")
            self._require(value, phase)
            value.update(state="queued", phase=phase, updated_at=shanghai_now().isoformat(), error="")
            (directory / "cancel.json").unlink(missing_ok=True)
            atomic_write_json(directory / "status.json", value, indent=2)
            from config.settings import BASE_DIR

            command = [
                sys.executable,
                "-X",
                "utf8",
                str(Path(BASE_DIR) / "scripts/history_backtest_workflow.py"),
                "--root",
                str(self.root.resolve()),
                "step",
                ident,
                phase,
            ]
            environment = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            environment["MARKET_DATA_NODE_ROLE"] = "collector" if phase == "prefetch" else "server"
            try:
                with (directory / "worker.log").open("ab") as log:
                    options = (
                        {"creationflags": subprocess.CREATE_NO_WINDOW}
                        if os.name == "nt"
                        else {"start_new_session": True}
                    )
                    process = subprocess.Popen(
                        command,
                        cwd=BASE_DIR,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        **options,
                    )
                value["pid"] = process.pid
                _PROCESSES[ident] = process
            except Exception:
                value.update(state="failed", error="无法启动独立历史子进程")
                atomic_write_json(directory / "status.json", value, indent=2)
                raise
        return value

    def cancel(self, ident):
        directory = self.directory(ident)
        value = self.get(ident)
        if value["state"] not in {"queued", "running"}:
            raise ValueError("当前没有运行中的阶段")
        atomic_write_json(directory / "cancel.json", {"requested_at": shanghai_now().isoformat()})
        return {**value, "cancel_requested": True}

    def execute(self, ident, phase):
        directory = self.directory(ident)
        with file_mutex(directory / "run.lock", timeout=0):
            value = read_json(directory / "status.json")
            self._require(value, phase)
            if value["state"] != "queued":
                (directory / "cancel.json").unlink(missing_ok=True)
            for later in PHASES[PHASES.index(phase) :]:
                value["stages"].pop(later, None)
            if phase in {"prepare", "prefetch", "freeze"}:
                value.update(frozen_version=None)
                for key in ("summary", "reconciliation", "archive_status"):
                    value.pop(key, None)
            value.update(
                state="running",
                phase=phase,
                pid=os.getpid(),
                error="",
                failures=[],
                updated_at=shanghai_now().isoformat(),
            )

            def progress(**step):
                if (directory / "cancel.json").exists():
                    raise InterruptedError("已取消；已完成的缓存和每日账户检查点保留")
                value.update(progress=step, updated_at=shanghai_now().isoformat())
                atomic_write_json(directory / "status.json", value, indent=2)

            try:
                progress(stage=phase)
                if phase == "prefetch":
                    today = shanghai_now()
                    is_open = calendar_day(calendar_path(), today.strftime("%Y%m%d"))[0]
                    if is_open and "09:15:00" <= today.strftime("%H:%M:%S") <= "15:15:00":
                        raise ValueError("历史准备和补数仅允许盘外执行，避免占用实时采集资源")
                    with file_mutex(self.root / "preparation.lock", timeout=0):
                        getattr(self, "_" + phase)(directory, value, progress)
                else:
                    getattr(self, "_" + phase)(directory, value, progress)
                value["stages"][phase] = "complete"
                value["state"] = "complete"
            except Exception as exc:
                state = "interrupted" if isinstance(exc, InterruptedError) else "blocked"
                value.update(state=state, error=f"{type(exc).__name__}: {exc}")
                value["stages"][phase] = state
            value["updated_at"] = shanghai_now().isoformat()
            atomic_write_json(directory / "status.json", value, indent=2)
            return value

    def _prepare(self, directory, value, progress):
        from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR
        from core.operations.replay_research import FrozenCalendar
        from core.portfolio.historical_pools import read_historical_pools

        calendar = FrozenCalendar(value["calendar"])
        manifest, scope = {}, []
        for index, date in enumerate(value["dates"]):
            candidate = calendar.prev(date)
            progress(
                stage="只读历史龙头池和决策池名单",
                trade_date=date,
                candidate_date=candidate,
                completed=index,
                total=len(value["dates"]),
            )
            packet = read_historical_pools(WEB_DATA_DIR, FACTOR_DB_PATH, candidate, date)
            name = f"pools/{date}.json.gz"
            manifest[name] = write_packet(directory / name, packet)
            scope.append(
                {
                    "date": date,
                    "candidate_date": candidate,
                    **packet["counts"],
                    "stock_count": len(packet["rows"]),
                    "basis": packet["basis"],
                }
            )
        atomic_write_json(directory / "pool_manifest.json", manifest)
        value["pool_scope"] = scope
        value["pool_scope_contract"] = "read_only_saved_pools_v1"

    def _pool_inputs(self, directory, date):
        from hashlib import sha256

        manifest = read_json(directory / "pool_manifest.json")
        name = f"pools/{date}.json.gz"
        path = directory / name
        if name not in manifest or sha256(path.read_bytes()).hexdigest() != manifest[name]:
            raise ValueError("历史池名单快照被修改，请重新读取名单")
        packet = read_packet(path)
        if packet.get("market_date") != date:
            raise ValueError("历史池名单日期不匹配")
        return packet

    def _prefetch(self, directory, value, progress):
        from core.infrastructure.shared_state import TaskLease

        lease = TaskLease.acquire("post_close_pipeline", 180)
        if lease is None:
            raise ValueError("已有盘后准备任务运行，请稍后重试独立补数")
        try:

            def guarded_progress(**step):
                if not lease.is_owner():
                    raise ValueError("独立补数租约已丢失，停止执行")
                progress(**step)

            self._collect(directory, value, guarded_progress)
            guarded_progress(stage="独立补数完成")
        finally:
            lease.release()

    def _collect(self, directory, value, progress):
        from config.settings import (
            CACHE_DIR,
            HISTORY_AXDATA_FIRST,
            PAPER_MAX_POSITIONS,
            PAPER_POSITION_PCT,
            PAPER_ROTATION_MIN_EDGE,
            TUSHARE_TOKEN,
        )
        from core.data import DataManager
        from core.operations.replay_research import FrozenCalendar
        from core.realtime.day_replay import DayReplayLoader
        from core.screening.strategy_profiles import StrategyProfileRepository
        from core.signals.minute_amount_profile import MinuteAmountProfileRepository
        from core.utils.stock_code_utils import StockCodeUtils
        from risk.risk_config import RiskConfig

        calendar = FrozenCalendar(value["calendar"])
        profiles = StrategyProfileRepository().list_profiles()
        context = {
            "profiles": {p["id"]: deepcopy(p) for p in profiles},
            "risk_config": RiskConfig.load().to_dict(),
            "account": {
                "max_positions": min(int(PAPER_MAX_POSITIONS), 3),
                "position_pct": float(PAPER_POSITION_PCT) / 100,
                "rotation_min_edge": float(PAPER_ROTATION_MIN_EDGE),
            },
        }
        value["sector_mode"] = context["risk_config"]["entry_sector_mode"]
        dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=True)
        dm.prefer_axdata_history = HISTORY_AXDATA_FIRST
        from core.data.providers.axdata_provider import AxDataProvider

        value["history_source_policy"] = {
            "axdata_first": HISTORY_AXDATA_FIRST,
            "axdata_version": AxDataProvider.available(),
            "transport": "local_sdk",
            "minute_fallbacks": ["eltdx", "Ashare"],
            "market_daily_factors": "read_existing_only",
        }
        loader = DayReplayLoader(data_manager=dm, calendar=calendar)
        request = dict(until="15:01:00", profile="production", lookback=10, codes=[], fetch_missing=False)
        potential, first_seen = {}, {}
        value["acquisition"] = {}
        for date in value["dates"]:
            progress(stage="整合候选与潜在持仓范围", trade_date=date)
            packet = self._pool_inputs(directory, date)
            for row in packet["rows"]:
                from core.realtime.entry_signal_service import RealtimeEntrySignalService

                old = potential.get(row["code"]) or {}
                names = RealtimeEntrySignalService._sectors(row) | RealtimeEntrySignalService._sectors(old)
                potential[row["code"]] = {"code": row["code"], "sector_names": sorted(names)}
                first_seen.setdefault(row["code"], date)
        for index, date in enumerate(value["dates"]):
            progress(
                stage="补取两池分钟与已有板块归档"
                if value["sector_mode"] == "trend_score"
                else "独立补取分钟及完整成分证据",
                trade_date=date,
                completed=index,
                total=len(value["dates"]),
            )
            required = {c: r for c, r in potential.items() if first_seen[c] <= date}
            packet = loader.run(
                {**request, "market_date": date, "fetch_missing": True},
                inputs_only=True,
                extra_codes=required,
                extra_rows=list(required.values()),
                pool_inputs=self._pool_inputs(directory, date),
                rule_risk=context["risk_config"],
                progress=lambda row, date=date: progress(**{**row, "trade_date": date}),
            )
            summary = packet.get("minute_acquisition") or {}
            value["acquisition"][date] = {
                **{k: v for k, v in summary.items() if k not in {"failures", "skips"}},
                "failure_preview": summary.get("failures", [])[:20],
            }
            packet["risk_config"] = packet.get("risk_config") or deepcopy(context["risk_config"])
            if not packet.get("minute_amount_profile") and potential:
                packet["minute_amount_profile"] = MinuteAmountProfileRepository().load()
            packet["minute_amount_profile"] = packet.get("minute_amount_profile") or {}
            packet["daily"], packet["suspensions"] = {}, {}
            for stock_index, code in enumerate(required):
                progress(
                    stage="核对日线及停复牌", trade_date=date, code=code, completed=stock_index, total=len(required)
                )
                canonical = StockCodeUtils.standardize_code(code)
                packet["daily"][code] = dm.get_stock_daily_data(canonical, date)
                packet["suspensions"][code] = dm.get_suspension_status(canonical, date, allow_remote=True)
            packet.update(execution_frozen=True, required_rows=required)
            write_packet(directory / "staging" / f"{date}.json.gz", pack_inputs(packet))
        from core.operations.replay_research import digest

        current = {p["id"]: p for p in StrategyProfileRepository().list_profiles()}
        if digest(current) != digest(context["profiles"]) or digest(RiskConfig.load().to_dict()) != digest(
            context["risk_config"]
        ):
            raise ValueError("补数期间策略配置发生变化，请重新执行独立补数")
        context["history_source_policy"] = deepcopy(value["history_source_policy"])
        atomic_write_json(directory / "context.json", context)
        value["potential_stock_count"] = len(potential)

    def _freeze(self, directory, value, progress):
        from config.settings import APP_DB_PATH, HOLDING_DB_PATH, WEB_DATA_DIR
        from core.operations.replay_research import digest
        from core.realtime.day_replay import replay_request
        from core.realtime.snapshot_replay import load_snapshot_inputs
        from scripts.diagnose_intraday_signal import runtime_evidence

        failures = []
        for index, date in enumerate(value["dates"]):
            progress(stage="校验历史证据连续性", trade_date=date, completed=index, total=len(value["dates"]))
            packet = read_packet(directory / "staging" / f"{date}.json.gz")
            failures.extend(audit_day(packet, date, packet["required_rows"]))
        atomic_write_json(directory / "validation.json", {"failures": failures, "complete": not failures}, indent=2)
        if failures:
            value["failures"] = failures[:100]
            raise ValueError(f"缺少或不连续的历史证据 {len(failures)} 项；补齐后重新校验，不能发布完整收益")
        version = uuid4().hex
        frozen = directory / "frozen" / version
        files, archive_status = {}, []
        for date in value["dates"]:
            progress(stage="冻结分钟输入及原始归档", trade_date=date)
            packet = read_packet(directory / "staging" / f"{date}.json.gz")
            name = f"days/{date}.json.gz"
            files[name] = write_packet(frozen / name, packet)
            for profile in ("leader_pool", "decision_pool"):
                request = replay_request(dict(market_date=date, profile=profile, replay_mode="archived_snapshot"))
                captured = load_snapshot_inputs(WEB_DATA_DIR, request)
                available = bool(
                    captured.get("plan") and any(e["kind"] == "snapshot" for e in captured.get("events", []))
                )
                archive_status.append(
                    {
                        "date": date,
                        "profile": profile,
                        "status": "captured" if available else "missing",
                        "failures": captured.get("load_failures", []),
                    }
                )
                if available:
                    selected = [r["code"] for r in captured["plan"]["rows"]]
                    captured["recorded_runtime"] = {
                        "confirmation_history": runtime_evidence(APP_DB_PATH, "realtime_signal_events", date, selected),
                        "signal_ledger": runtime_evidence(HOLDING_DB_PATH, "signal_evidence", date, selected),
                    }
                    name = f"snapshots/{date}-{profile}.json.gz"
                    files[name] = write_packet(frozen / name, captured)
        manifest = dict(
            version=version,
            dates=value["dates"],
            calendar=value["calendar"],
            capital=value["capital"],
            context=read_json(directory / "context.json"),
            code_hash=code_version(),
            files=files,
            archive_status=archive_status,
            frozen_at=shanghai_now().isoformat(),
            classification="historical_research_not_original_live_execution",
        )
        manifest["contract_hash"] = digest(manifest)
        atomic_write_json(frozen / "manifest.json", manifest, indent=2)
        value.update(frozen_version=version, archive_status=archive_status)

    def frozen(self, directory, value):
        version = value.get("frozen_version") or ""
        if not re.fullmatch(r"[0-9a-f]{32}", version):
            raise ValueError("缺少冻结版本")
        return FrozenHistory(directory / "frozen" / version)

    def _run(self, directory, value, progress):
        from backtest.backtest_engine import BacktestConfig, BacktestEngine
        from core.operations.replay_research import digest
        from risk.risk_config import RiskConfig

        frozen = self.frozen(directory, value)
        manifest = frozen.manifest
        config = BacktestConfig.from_risk_config(
            RiskConfig.from_dict(manifest["context"]["risk_config"]),
            initial_capital=manifest["capital"],
            risk_control=True,
        )
        # The effective account settings are sealed too, not read on resumption.
        account = manifest["context"]["account"]
        config.max_positions = account["max_positions"]
        config.max_position_per_stock = config.account_position_pct = account["position_pct"]
        config.rotation_min_edge = account["rotation_min_edge"]
        config.max_total_position = config.max_sector_concentration = 1.0
        config.production_rules = config.rotation_enabled = True
        config.entry_mode, config.position_sizing_mode, config.exit_policy_mode = "hybrid", "fixed_risk", "strategy"
        config.exit_minute_data_policy, config.replay_missing_evidence_policy = "cache_only", "strict"
        engine = BacktestEngine(frozen, config, frozen_context={**manifest["context"], "calendar": frozen.calendar})
        checkpoint_path = directory / f"checkpoint-{manifest['version']}.json"
        completed = []
        if checkpoint_path.exists():
            checkpoint = read_json(checkpoint_path)
            if checkpoint.get("checkpoint_hash") != digest(
                {k: v for k, v in checkpoint.items() if k != "checkpoint_hash"}
            ):
                raise ValueError("账户检查点被修改")
            if checkpoint["contract_hash"] != manifest["contract_hash"] or checkpoint["state_hash"] != digest(
                checkpoint["state"]
            ):
                raise ValueError("账户检查点与冻结合同不一致")
            engine.import_state(checkpoint["state"])
            completed = checkpoint["completed"]
            if completed != manifest["dates"][: len(completed)] or completed != [r["date"] for r in engine.daily_nav]:
                raise ValueError("检查点完成日期与冻结交易日不一致")
            engine._candidate_evidence_samples = {tuple(row) for row in checkpoint.get("samples", [])}
        for index, date in enumerate(manifest["dates"]):
            if date in completed:
                continue
            progress(stage="只读冻结输入回测", trade_date=date, completed=index, total=len(manifest["dates"]))
            engine.run_one_day(date, "")
            completed.append(date)
            state = engine.export_state()
            checkpoint = dict(
                contract_hash=manifest["contract_hash"],
                completed=completed,
                state=state,
                state_hash=digest(state),
                samples=sorted(engine._candidate_evidence_samples),
            )
            checkpoint["checkpoint_hash"] = digest(checkpoint)
            atomic_write_json(checkpoint_path, checkpoint)
        result = engine._generate_backtest_report()
        report = dict(
            classification="historical_minute_research",
            frozen_version=manifest["version"],
            completed_dates=completed,
            data_quality=engine.replay_data_quality(),
            summary={k: v for k, v in result.items() if isinstance(v, (str, int, float, bool)) or v is None},
            nav=engine.daily_nav,
            trades=[asdict(t) for t in engine.trade_history],
            entry_attempts=engine.entry_attempts,
            account_modified=False,
            notification_sent=False,
        )
        if not report["data_quality"].get("full_sample_metrics_valid"):
            raise ValueError("证据样本不完整，不发布完整区间收益")
        atomic_write_json(directory / "result.json", report, default=str, indent=2)
        value["summary"] = report["summary"]

    def _reconcile(self, directory, value, progress):
        from core.realtime.runtime_diagnostics import attach_runtime_diagnostics
        from core.realtime.snapshot_replay import replay_snapshot_inputs

        frozen = self.frozen(directory, value)
        result = read_json(directory / "result.json")
        if result.get("frozen_version") != frozen.manifest["version"]:
            raise ValueError("回测结果与冻结版本不一致")
        rows = []
        for source in frozen.manifest["archive_status"]:
            date, profile = source["date"], source["profile"]
            progress(stage="原始归档与实时记录对账", trade_date=date, profile=profile)
            if source["status"] == "missing":
                rows.append({**source, "reason": "缺少当时归档，不能用补取分钟或收盘快照替代"})
                continue
            captured = read_packet(frozen.directory / "snapshots" / f"{date}-{profile}.json.gz")
            report = replay_snapshot_inputs(captured)
            report["recorded_runtime"] = captured.get("recorded_runtime", {})
            attach_runtime_diagnostics(report)
            for stock in report["stocks"]:
                attempts = [
                    a
                    for a in result["entry_attempts"]
                    if str(a.get("date") or a.get("trade_date")) == date
                    and str(a.get("stock_code") or a.get("code")).zfill(6) == stock["code"]
                ]
                rows.append(
                    dict(
                        date=date,
                        profile=profile,
                        code=stock["code"],
                        status="compared" if report["complete"] else "incomplete_archive",
                        minute_confirm_time=next(
                            (a.get("confirm_time") for a in attempts if a.get("confirm_time")), ""
                        ),
                        snapshot_confirm_time=stock.get("first_confirm_time") or "",
                        runtime_diagnostic=stock.get("runtime_diagnostic") or {},
                        failures=report["failures"],
                    )
                )
        atomic_write_json(
            directory / "reconciliation.json",
            dict(
                frozen_version=frozen.manifest["version"],
                rows=rows,
                notification_sent=False,
                note="分钟与秒级确认不要求同刻；缺少运行记录不能认定当时未确认",
            ),
            indent=2,
        )
        value["reconciliation"] = rows[:100]
