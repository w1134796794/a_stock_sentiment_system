"""Bounded, persistent diagnostic jobs separate from paper/live state."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock, Thread

from core.infrastructure.atomic_json import atomic_write_json
from core.infrastructure.file_mutex import file_mutex
from core.realtime.day_replay import DayReplayLoader, clean, compare_reports, replay_request, replay_saved_inputs


class ReplayCancelled(Exception):
    pass


class DayReplayJobs:
    def __init__(self, root, *, loader_factory=DayReplayLoader):
        self.root = Path(root) / "intraday_replays"
        self.loader_factory = loader_factory
        self.lock = Lock()
        self.thread = None

    def path(self, run_id, name="status.json"):
        if not re.fullmatch(r"[a-f0-9]{32}", str(run_id)):
            raise ValueError("回放 ID 无效")
        return self.root / run_id / name

    def start(self, payload):
        request = replay_request(payload)
        baseline = str(payload.get("baseline_id") or "")
        if baseline:
            if not self.path(baseline, "inputs.json").exists():
                raise ValueError("基准报告不存在或尚未完成")
            captured = json.loads(self.path(baseline, "inputs.json").read_text(encoding="utf-8"))
            if captured["request"] != {k:v for k,v in request.items() if k != "fetch_missing"}:
                raise ValueError("对照必须保持基准日期、范围、股票与结束时间一致")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("已有诊断回放运行中")
            run_id = uuid.uuid4().hex
            status = {"run_id": run_id, "request": request, "baseline_id": baseline,
                      "state": "queued", "started_epoch": time.time(), "updated_epoch": time.time(),
                      "progress": {"stage": "等待诊断回放", "completed": 0, "total": 0}}
            atomic_write_json(self.path(run_id), status)
            self.thread = Thread(target=self._run, args=(status,), name="intraday-diagnostic", daemon=True)
            self.thread.start()
            return {"ok": True, "run_id": run_id}

    def _run(self, status):
        run_id = status["run_id"]
        def save():
            status["updated_epoch"] = time.time()
            atomic_write_json(self.path(run_id), clean(status))
        def progress(value):
            if self.path(run_id, "cancel.json").exists():
                raise ReplayCancelled()
            status.update(state="running", progress=value)
            save()
        try:
            with file_mutex(self.root / "execution.lock", timeout=0):
                progress({"stage": "加载候选日输入", "completed": 0, "total": 0})
                if self.path(run_id, "cancel.json").exists():
                    raise ReplayCancelled()
                baseline = status.get("baseline_id")
                if baseline:
                    original = json.loads(self.path(baseline, "report.json").read_text(encoding="utf-8"))
                    report = replay_saved_inputs(json.loads(self.path(baseline, "inputs.json").read_text(encoding="utf-8")), progress)
                    report["comparison"] = compare_reports(original, report)
                    report["recorded_runtime"] = original.get("recorded_runtime", {})
                    report["limitations"].extend(original.get("limitations", [])[4:])
                else:
                    report = self.loader_factory().run(status["request"], progress)
                if self.path(run_id, "cancel.json").exists():
                    raise ReplayCancelled()
                captured = report.pop("replay_inputs", None)
                if captured is not None:
                    atomic_write_json(self.path(run_id, "inputs.json"), captured)
                report["run_id"] = run_id
                atomic_write_json(self.path(run_id, "report.json"), clean(report), indent=2)
                status.update(state="done", summary={key: report.get(key) for key in (
                    "market_date", "candidate_date", "confirmed_stocks", "complete", "input_hash", "code_hash", "counts")})
        except ReplayCancelled:
            status.update(state="cancelled", error="用户取消诊断回放")
        except Exception as exc:
            status.update(state="failed", error=str(exc))
        finally:
            status["finished_epoch"] = time.time()
            save()

    def get(self, run_id, *, report=False):
        path = self.path(run_id, "report.json" if report else "status.json")
        if not path.exists():
            raise FileNotFoundError("回放记录不存在或报告尚未完成")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not report and value.get("state") in {"running", "queued"} and time.time() - value.get("updated_epoch", 0) > 1800:
            value.update(state="interrupted", error="任务超过30分钟未更新，可能服务已重启；请重新运行")
        return value

    def cancel(self, run_id):
        status = self.get(run_id)
        if status["state"] not in {"running", "queued"}:
            raise ValueError("该任务没有运行中")
        atomic_write_json(self.path(run_id, "cancel.json"), {"requested_epoch": time.time()})
        return {"ok": True, "run_id": run_id}

    def recent(self):
        result = []
        for path in sorted(self.root.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                result.append(self.get(path.parent.name))
            except (ValueError, OSError):
                continue
        return result
