import json
import sqlite3
from datetime import datetime, timedelta
import zlib

from core.realtime.debug_journal import IntradayDebugJournal


def records(path):
    with sqlite3.connect(path) as con:
        return [json.loads(zlib.decompress(r[0])) for r in con.execute(
            "SELECT payload_zlib FROM debug_events ORDER BY id")]


def test_debug_retains_first_blocker_after_deadline_and_gate_changes(tmp_path):
    clock = [datetime(2026, 10, 26, 9, 32)]
    log = IntradayDebugJournal(tmp_path, enabled=True, clock=lambda: clock[0])
    payload = {"reason": "板块证据不足", "condition_checks": {"latest": {"checks": {"sector": None}}},
               "entry_parameters": {"acceleration_opening_minutes": 1}, "quote": {"last_price": 10}}
    log.record("evaluation", "20261026", "300369", payload=payload)
    payload["quote"]["last_price"] = 10.1
    log.record("evaluation", "20261026", "300369", payload=payload)
    payload["condition_checks"]["latest"]["checks"]["sector"] = False
    log.record("evaluation", "20261026", "300369", payload=payload)
    clock[0] += timedelta(minutes=1)
    log.record("evaluation", "20261026", "300369", payload=payload)
    clock[0] = datetime(2026, 10, 26, 14, 1)
    log.record("evaluation", "20261026", "300369", payload={"reason": "截止时间前未完成确认"})
    log.record("health_gate", "20261026", "300369", payload={"reason": "采集名单版本未对齐"})
    rows = records(tmp_path / "20261026.sqlite3")
    assert len(rows) == 5
    assert rows[0]["payload"]["reason"] == "板块证据不足"
    assert rows[0]["payload"]["quote"]["last_price"] == 10
    assert rows[1]["sample_kind"] == "state_change"
    assert rows[2]["sample_kind"] == "minute_sample"
    assert rows[-1]["stage"] == "health_gate"


def test_debug_io_failure_does_not_stop_trading_and_disabled_creates_nothing(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    IntradayDebugJournal(blocked, enabled=True).record("error", "20261026", payload={"reason": "test"})
    root = tmp_path / "disabled"
    IntradayDebugJournal(root, enabled=False).record("evaluation", "20261026")
    assert not root.exists()


def test_saved_configs_keep_original_opening_window():
    from risk.risk_config import RiskConfig
    from core.signals.production_rules import minute_evaluator, entry_parameters

    assert minute_evaluator(RiskConfig.from_dict({})).opening_window_minutes("acceleration") == 5
    risk = RiskConfig.from_dict({"acceleration_opening_minutes": 1})
    evaluator = minute_evaluator(risk)
    from backtest.minute_entry import ENTRY_ACCELERATION, ENTRY_WEAK, ENTRY_CONTINUATION
    assert evaluator.opening_window_minutes(ENTRY_ACCELERATION) == 1
    assert evaluator.opening_window_minutes(ENTRY_WEAK) == 5
    assert evaluator.opening_window_minutes(ENTRY_CONTINUATION) == 5
    assert entry_parameters(evaluator)["acceleration_opening_minutes"] == 1
    from backtest.backtest_engine import BacktestConfig
    assert BacktestConfig.from_risk_config(risk).acceleration_opening_minutes == 1


def test_debug_reader_filters_without_modifying_archive(tmp_path, monkeypatch, capsys):
    from scripts.read_intraday_debug import main

    log = IntradayDebugJournal(tmp_path, enabled=True, clock=lambda: datetime(2026, 10, 26, 9, 32, 3))
    log.record("evaluation", "20261026", "300369", payload={"reason": "缺少板块"})
    log.record("health_gate", "20261026", "000001", payload={"reason": "行情过期"})
    monkeypatch.setattr("sys.argv", ["read_intraday_debug", "--root", str(tmp_path), "--date", "20261026",
        "--code", "300369", "--stage", "evaluation", "--since", "09:32:03", "--until", "09:32:03"])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "300369"
    assert result["payload"]["reason"] == "缺少板块"
    assert len(records(tmp_path / "20261026.sqlite3")) == 2


def test_minute_fallback_shares_acceleration_window():
    import pandas as pd
    from backtest.minute_entry import ENTRY_ACCELERATION, MinuteEntryEvaluator

    frame = pd.DataFrame([
        dict(time="09:31:00", open=10.6, high=10.65, low=10.5, close=10.6, volume=10000, amount=106000),
        dict(time="09:32:00", open=10.65, high=10.75, low=10.65, close=10.72, volume=20000, amount=214000),
        dict(time="09:33:00", open=10.72, high=10.8, low=10.7, close=10.75, volume=20000, amount=214500),
    ])
    args = dict(mode=ENTRY_ACCELERATION, bars=frame, open_gap=.06, prev_close=10,
                is_leader=True, limit_price=11, sector_sync=lambda at: True)
    short = MinuteEntryEvaluator(acceleration_opening_minutes=1).evaluate(**args)
    old = MinuteEntryEvaluator(acceleration_opening_minutes=5).evaluate(**args)
    assert short.status == "filled" and short.confirm_time == "09:32:00"
    assert old.status != "filled"
