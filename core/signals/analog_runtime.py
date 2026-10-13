"""Bounded background scoring; request/notification paths only read frozen results."""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from core.operations.ledger import TradingLedger
from core.signals.case_features import event_time
from core.signals.similar_case_service import SimilarCaseService, load_case_index


def query_record(case: dict) -> dict:
    from backtest.backtest_engine import BacktestConfig, BacktestEngine
    from core.signals.case_outcomes import OUTCOME_VERSION

    config = dict(case.get("backtest_config") or {})
    if config.get("exit_policy_mode") == "oos_selected":
        config["exit_policy_mode"] = (case.get("resolved_exit_policy") or {}).get("policy") or "strategy"
    if not config:
        return {"case": case, "versions": {}}
    # Version hashing needs no data manager, calendar, or market request.
    engine = object.__new__(BacktestEngine)
    engine.config = BacktestConfig(**config)
    return {"case": case, "versions": {
        "cost_version": engine._cost_version(),
        "exit_rule_version": engine._exit_rule_version({"strategy_execution": case.get("strategy_execution") or {}}),
        "outcome_version": OUTCOME_VERSION}}


def notification_summary(result: dict) -> str:
    if result.get("status") != "estimated":
        return "相似历史评估：尚未完成或样本不足，不影响原信号确认。"
    reliability = result.get("reliability") or {}
    interval = result.get("probability_interval") or {}
    p, expected = result.get("probability"), result.get("expected_net_return")
    if p is None or expected is None:
        return "相似历史评估：样本不足。"
    low, high = interval.get("low"), interval.get("high")
    bounds = f"，95%区间{low:.0%}–{high:.0%}" if low is not None and high is not None else ""
    payoff = result.get("payoff_ratio")
    tail = (result.get("return_distributions") or {}).get("net_return", {}).get("p05")
    extra = (f"，盈亏比{payoff:.2f}" if payoff is not None else "，盈亏比暂无")
    extra += f"，尾部5%收益{tail:+.1%}" if tail is not None else ""
    return (f"相似历史参考：净盈利频率{p:.0%}{bounds}，平均净收益{expected:+.1%}{extra}；"
            f"{reliability.get('raw_sample_count', 0)}例/{reliability.get('trading_days', 0)}日；"
            f"{'已校准' if result.get('calibration_status') == 'fitted' else '未校准'}，不保证盈利。"
            f"\n评估版本：{result.get('forecast_id', '')}")


class AnalogRuntime:
    def __init__(self, ledger: TradingLedger | None = None, index_path: Path | None = None,
                 model_path: Path | None = None):
        from config.settings import WEB_DATA_DIR
        self.ledger = ledger or TradingLedger()
        self.index_path = index_path or Path(WEB_DATA_DIR) / "signal_cases" / "matching_index.json"
        configured = os.environ.get("ANALOG_MODEL_FILE")
        self.model_path = model_path or (Path(configured) if configured else None)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._models = {}

    def attach(self, payload: dict, *, enqueue: bool = False) -> dict:
        for row in payload.get("rows") or []:
            case = (row.get("signal_evidence") or {}).get("signal_case")
            if not case or not row.get("signal_id"):
                continue
            if enqueue:
                case = {**case, "signal_id": row["signal_id"]}
                result = self.ledger.enqueue_analog(case)
                self.start()
            else:
                result = self.ledger.analog_analysis(signal_id=row["signal_id"])
            row["analog_analysis"] = result
            row["forecast_id"] = result.get("forecast_id") or ""
        return payload

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="analog-scoring", daemon=True)
                self._thread.start()
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(30)
            self._wake.clear()
            if self._stop.is_set():
                break
            for job in self.ledger.pending_analog_jobs():
                if self._stop.is_set():
                    break
                if not self.ledger.claim_analog_job(job["forecast_id"]):
                    continue
                try:
                    self.score_job(job)
                except Exception as exc:
                    self.ledger.finish_analog_job(job["forecast_id"], error=f"analysis_failed:{type(exc).__name__}")

    def score_job(self, job: dict) -> dict:
        existing = self.ledger.case_forecast(job["forecast_id"])
        if existing:
            self.ledger.finish_analog_job(job["forecast_id"])
            return existing
        case = job["case"]
        as_of = event_time("", case.get("confirm_at"))
        result = {"status": "not_ready", "reason": "matching_index_unavailable", "probability": None}
        if self.index_path.exists() and as_of:
            index = load_case_index(self.index_path)
            query = query_record(case)
            # Never fit an artifact containing observations published after this signal.
            key = (index.get("data_version"), as_of[:10])
            if key not in self._models:
                self._models.clear()
                self._models[key] = SimilarCaseService(index.get("records") or []).fit(as_of=as_of[:10] + "T00:00:00")
            result = self._models[key].evaluate(query, as_of=as_of)
            from core.signals.similarity_validation import evaluate_frozen_model
            artifacts = self.ledger.analog_models(released_only=True)
            if self.model_path and self.model_path.exists():
                artifacts.insert(0, json.loads(self.model_path.read_text(encoding="utf-8")))
            for artifact in artifacts:
                evaluated = evaluate_frozen_model(artifact, query, as_of=as_of)
                if evaluated.get("status") == "estimated":
                    result = evaluated
                    break
            records = {r["case"]["case_id"]: r for r in index.get("records") or []}
            for match in result.get("matches") or []:
                record = records.get(match["case_id"], {})
                from core.signals.similar_case_service import known_label
                outcome = known_label(record, "net_return", as_of) or {}
                match.update(exit_reason=outcome.get("exit_reason"), fees=outcome.get("fees"),
                             execution_precision=outcome.get("execution_precision"))
                match["normalized_path"] = [{"day": 0, "value": 100.}]
                for day in (1, 3, 5):
                    label = known_label(record, f"return_d{day}", as_of)
                    if label and label.get("value") is not None:
                        match["normalized_path"].append({"day": day, "value": 100 * (1 + float(label["value"]))})
            sample = [records[i]["case"]["market_date"] for i in result.get("sample_case_ids") or [] if i in records]
            result["sample_window"] = {"start": min(sample), "end": max(sample)} if sample else None
            from core.portfolio.signal_comparison import SignalComparisonService
            result["remaining_horizon"] = SignalComparisonService().entry_horizon(result, index.get("records") or [], as_of=as_of)
        result = copy.deepcopy(result)
        result.update(source_forecast_id=result.get("forecast_id"), forecast_id=job["forecast_id"],
                      signal_id=job["signal_id"], case_id=job["case_id"], as_of=as_of,
                      model_version=result.get("model_version") or "unavailable",
                      trading_gate_enabled=False, prediction_generated_at=datetime.now().isoformat(timespec="milliseconds"))
        saved = self.ledger.save_case_forecast(result)
        self.ledger.finish_analog_job(job["forecast_id"])
        return saved
