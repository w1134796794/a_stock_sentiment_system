import json
from types import SimpleNamespace


def test_rebuild_blocks_before_controller_when_prefetch_incomplete(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    failures = [{"trade_date": "20260901", "code": "000001", "evidence": "minute_bars",
                 "reason": "缺少下午分钟"}]
    called = []
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {"mode": "rebuild"})
    monkeypatch.setattr(module, "_get_paper_trading_service", lambda: SimpleNamespace(
        prefetch_replay_minutes=lambda *a, **kw: {"minute_failed": 1, "evidence_failures": failures}))
    monkeypatch.setattr(BACKTEST_CONTROLLER, "start", lambda *a, **kw: called.append(a))
    monkeypatch.setattr(BACKTEST_CONTROLLER, "status", lambda *a: (_ for _ in ()).throw(AssertionError("must not poll controller")))
    module._run_paper_rebuild("20260901", "20260903", 1000000)
    assert called == []
    assert module._PAPER_REPLAY_STATE["state"] == "blocked"
    payload = json.loads(module.api_portfolio_replay_status().body)
    assert payload["state"] == "blocked"
    assert payload["paper_replay"]["evidence_failures"] == failures


def test_configuration_endpoints_use_transfer_and_check_preview_version(monkeypatch):
    import web.app as module
    import core.screening.config_transfer as transfer_module
    class Transfer:
        def export(self): return {"effective_version": "v1"}
        def compare(self, bundle): return {"current_version": "v1", "changed_count": 1}
        def apply(self, bundle, version):
            if version != "v1": raise ValueError("本地配置已变化")
            return {"effective_version": "v2"}
    monkeypatch.setattr(transfer_module, "StrategyConfigTransfer", Transfer)
    assert json.loads(module.api_strategy_config_export().body)["effective_version"] == "v1"
    assert json.loads(module.api_strategy_config_compare({"bundle": {}}).body)["data"]["changed_count"] == 1
    assert module.api_strategy_config_import({"bundle": {}, "expected_version": "old"}).status_code == 400
    assert json.loads(module.api_strategy_config_import({"bundle": {}, "expected_version": "v1"}).body)["data"]["effective_version"] == "v2"


def test_rebuild_does_not_import_results_with_runtime_evidence_gaps(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    failures = [{"trade_date": "20260902", "code": "000001", "evidence": "exit_minute_bars",
                 "reason": "持仓退出分钟缺失"}]
    imported = []
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {"mode": "rebuild", "state": "running", "imported": False})
    monkeypatch.setattr(module, "_get_paper_trading_service", lambda: SimpleNamespace(
        latest_run_id=lambda **kw: "test",
        rebuild_evidence_failures=lambda run: failures,
        import_backtest_run=lambda *a, **kw: imported.append(a)))
    monkeypatch.setattr(BACKTEST_CONTROLLER, "status", lambda *a: {"state": "done"})
    result = json.loads(module.api_portfolio_replay_status().body)
    assert result["state"] == "blocked"
    assert imported == []
    assert result["paper_replay"]["evidence_failures"] == failures


def test_idle_replay_does_not_attach_an_unrelated_backtest(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {})
    monkeypatch.setattr(BACKTEST_CONTROLLER, "status", lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert json.loads(module.api_portfolio_replay_status().body)["state"] == "idle"


def test_incremental_progress_keeps_day_totals_and_timestamps(monkeypatch):
    import web.app as module
    import core.portfolio.incremental_replay as source
    class Replay:
        def run(self, start, end, progress):
            progress({"stage": "准备逐日接力", "total_days": 5, "completed_days": 0})
            progress({"stage": "检查分钟证据", "trade_date": start, "code": "000001"})
            return {"last_completed_date": end, "completed_days": 5}
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {"mode": "incremental", "state": "running"})
    monkeypatch.setattr(source, "IncrementalPaperReplay", Replay)
    module._run_paper_replay("20260901", "20260907")
    result = json.loads(module.api_portfolio_replay_status().body)["paper_replay"]
    assert result["progress"]["total_days"] == 5
    assert result["progress"]["code"] == "000001"
    assert result["updated_epoch"] <= result["finished_epoch"]
    assert result["state"] == "done"


def test_rebuild_exposes_real_controller_progress(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    progress = {"stage": "运行分钟回放", "trade_date": "20260902", "completed": 1,
                "total": 5, "unit": "交易日", "updated_epoch": 100}
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {"mode": "rebuild", "state": "running"})
    monkeypatch.setattr(BACKTEST_CONTROLLER, "status", lambda *a: {"state": "running", "progress": progress})
    result = json.loads(module.api_portfolio_replay_status().body)
    assert result["paper_replay"]["progress"] == progress
    assert result["paper_replay"]["updated_epoch"] == 100


def test_failed_rebuild_import_is_not_reported_as_completed(monkeypatch):
    import web.app as module
    from desktop.runner import BACKTEST_CONTROLLER
    monkeypatch.setattr(module, "_PAPER_REPLAY_STATE", {"mode": "rebuild", "state": "running"})
    monkeypatch.setattr(BACKTEST_CONTROLLER, "status", lambda *a: {"state": "done"})
    monkeypatch.setattr(module, "_get_paper_trading_service", lambda: SimpleNamespace(
        latest_run_id=lambda **kw: "test", rebuild_evidence_failures=lambda run: [],
        import_backtest_run=lambda *a, **kw: (_ for _ in ()).throw(ValueError("write failed"))))
    result = json.loads(module.api_portfolio_replay_status().body)
    assert result["state"] == "error"
    assert "write failed" in result["paper_replay"]["error"]


def test_backtest_progress_advances_only_after_processing_day():
    from backtest.backtest_engine import BacktestEngine
    engine = object.__new__(BacktestEngine)
    engine.config = SimpleNamespace(initial_capital=1000000)
    engine.total_capital = 1000000
    engine._get_trade_dates = lambda *a: ["20260901", "20260902"]
    updates = []
    processed = []
    def process(day, plans):
        assert updates[-1]["completed"] == len(processed)
        assert updates[-1]["trade_date"] == day
        processed.append(day)
    engine._process_date = process
    engine._generate_backtest_report = lambda: {"total_return": 0}
    engine.run_backtest("20260901", "20260902", "fixture", progress=updates.append)
    assert [(row["completed"], row["total"]) for row in updates] == [(0, 2), (1, 2), (1, 2), (2, 2), (2, 2)]
