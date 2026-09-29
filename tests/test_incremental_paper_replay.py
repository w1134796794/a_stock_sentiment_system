from __future__ import annotations

from pathlib import Path

import pytest
import pandas as pd

from backtest.backtest_engine import TradeRecord
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.incremental_replay import IncrementalPaperReplay, ReplayBlocked
from core.portfolio.paper_trading_service import PaperTradingService


class Calendar:
    is_real = True
    days = ["20260901", "20260902", "20260903"]

    def next(self, date):
        if date == "20260831":
            return self.days[0]
        return self.days[self.days.index(date) + 1] if date in self.days[:-1] else "20260904"

    def prev(self, date):
        return self.days[self.days.index(date) - 1] if date != self.days[0] else "20260831"

    def get_trade_dates(self, start, end):
        return [day for day in self.days if start <= day <= end]


class Engine:
    def __init__(self, cash=1_000_000):
        self.cash = cash
        self.trade_history = []
        self.entry_attempts = []
        self.current_positions = {}
        self.last_date = ""

    def _get_stock_daily_bar(self, code, date):
        return {"close": 10}

    def run_one_day(self, date, _plan):
        self.last_date = date
        if date != "20260901":
            return
        trade = TradeRecord(
            date=date, stock_code="000001", stock_name="测试", pattern_type="弱转强",
            action="BUY", entry_price=10, exit_price=0, shares=100,
            position_size=1000, pnl=0, pnl_pct=0, holding_days=0,
            hot_resonance=False, resonance_sectors="",
        )
        self.trade_history.append(trade)
        self.cash -= 1000
        self.current_positions["000001"] = {
            "entry_price": 10, "last_close": 10, "highest_price": 10,
            "strategy_execution": {},
        }

    def export_state(self):
        return {"last_date": self.last_date, "cash": self.cash,
                "current_positions": self.current_positions}


def service(tmp_path: Path, monkeypatch) -> IncrementalPaperReplay:
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    replay = IncrementalPaperReplay(repository, calendar=Calendar())
    monkeypatch.setattr(replay, "config_version", lambda: "test-v1")
    monkeypatch.setattr(replay, "_plan", lambda prev: (tmp_path, ["000001"]))
    monkeypatch.setattr(replay, "_seed_engine", lambda *_: Engine())
    return replay


def test_commits_each_day_and_stops_before_missing_evidence(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    monkeypatch.setattr(replay, "_required_minutes",
                        lambda date, codes: ["000001"] if date == "20260902" else [])

    with pytest.raises(ReplayBlocked) as exc:
        replay.run("20260901", "20260903")

    assert exc.value.trade_date == "20260902"
    checkpoint = replay.checkpoint()
    assert checkpoint["last_completed_date"] == "20260901"
    assert checkpoint["blocked_date"] == "20260902"
    assert replay.repository.account()["cash"] == pytest.approx(999000)
    assert len(replay.repository.list_trades()) == 1
    with replay.repository._connect() as conn:
        day = conn.execute("SELECT fill_keys_json FROM portfolio_replay_days").fetchone()
        assert "incremental_replay" not in day[0]  # stable hash, not a mutable row id


def test_sector_peer_minute_evidence_is_required(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    pd.DataFrame([{
        "代码": "000001", "共振板块": "测试板块", "所属板块": "备用板块",
    }]).to_csv(tmp_path / "交易计划_20260831.csv", index=False)
    monkeypatch.setattr(
        "core.portfolio.incremental_replay.BacktestEngine._load_sector_peer_codes",
        lambda date, sectors: {"000001": ["000002", "000003"]},
    )
    seen = []

    def required(date, codes):
        seen.extend(codes)
        return ["000002"]

    monkeypatch.setattr(replay, "_required_minutes", required)
    with pytest.raises(ReplayBlocked, match="000002"):
        replay.run("20260901", "20260901")

    assert set(seen) == {"000001", "000002", "000003"}
    assert replay.repository.list_trades() == []
    assert replay.checkpoint()["blocked_date"] == "20260901"


def test_engine_insufficient_evidence_does_not_commit_day(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    monkeypatch.setattr(replay, "_required_minutes", lambda date, codes: [])
    engine = Engine()

    def insufficient(date, plan):
        engine.entry_attempts.append({
            "stock_code": "000001", "status": "data_insufficient",
            "reason_code": "missing_sector_confirmation", "reason": "板块确认数据不足",
        })

    engine.run_one_day = insufficient
    monkeypatch.setattr(replay, "_seed_engine", lambda *_: engine)
    with pytest.raises(ReplayBlocked, match="板块确认数据不足"):
        replay.run("20260901", "20260901")

    assert replay.repository.list_trades() == []
    assert replay.checkpoint()["blocked_date"] == "20260901"
    assert not replay.checkpoint()["last_completed_date"]


def test_repeat_does_not_rebuy_and_manual_trade_is_preserved(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    monkeypatch.setattr(replay, "_required_minutes", lambda date, codes: [])
    replay.run("20260901", "20260901")
    replay.repository.open_position({
        "code": "000002", "name": "手工", "entry_date": "20260901",
        "entry_price": 20, "shares": 100, "source": "manual",
    })
    # Restarted process reads its own account checkpoint, not global rolling_state.json.
    replay2 = service(tmp_path, monkeypatch)
    monkeypatch.setattr(replay2, "_required_minutes", lambda date, codes: [])
    result = replay2.run("20260901", "20260901")
    assert result["completed_days"] == 0
    assert replay2.repository.get_open_position("000002")
    assert len(replay2.repository.list_trades()) == 2


def test_later_manual_fill_blocks_time_travel(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    monkeypatch.setattr(replay, "_required_minutes", lambda date, codes: [])
    replay.run("20260901", "20260901")
    replay.repository.open_position({
        "code": "000002", "name": "手工", "entry_date": "20260903",
        "entry_price": 20, "shares": 100, "source": "manual",
    })
    with pytest.raises(ReplayBlocked, match="穿越成交时间") as exc:
        replay.run("", "20260903")
    assert exc.value.trade_date == "20260902"


def test_failed_day_rolls_back_fill_and_checkpoint(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    engine = Engine()
    engine.run_one_day("20260901", str(tmp_path))
    engine.cash = 123  # Force post-fill reconciliation failure inside the transaction.

    with pytest.raises(ReplayBlocked, match="现金与回放引擎不一致"):
        replay._commit_day("20260901", "test-v1", engine, engine.trade_history, 0)

    assert replay.repository.list_trades() == []
    assert replay.checkpoint() == {}
    with replay.repository._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM portfolio_replay_days").fetchone()[0] == 0


def test_existing_manual_position_enters_engine_state(tmp_path, monkeypatch):
    replay = service(tmp_path, monkeypatch)
    replay.repository.open_position({
        "code": "000002", "name": "手工", "entry_date": "20260831",
        "entry_price": 20, "shares": 100, "source": "manual",
        "emergency_loss_pct": 8,
    })
    account, positions, _, _ = replay._account_snapshot()
    replay.dm = object()
    engine = IncrementalPaperReplay._seed_engine(replay, {}, account, positions)

    assert engine.current_positions["000002"]["shares"] == 100
    assert engine.current_positions["000002"]["stop_loss_price"] == pytest.approx(18.4)
    assert engine.cash == pytest.approx(replay.repository.account()["cash"])


def test_historical_rebuild_uses_separate_account(tmp_path, monkeypatch):
    import core.portfolio.paper_trading_service as paper_module

    monkeypatch.setattr(paper_module, "OUTPUT_DIR", tmp_path)
    result_dir = tmp_path / "backtest_results"
    result_dir.mkdir()
    pd.DataFrame([{
        "date": "20260901", "entry_date": "20260901", "action": "BUY",
        "stock_code": "000001", "stock_name": "重建", "entry_price": 10,
        "shares": 100, "position_size": 1000,
    }]).to_csv(result_dir / "backtest_trades_test.csv", index=False)
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    paper = PaperTradingService(repository)
    repository.open_position({
        "code": "000002", "name": "手工", "entry_date": "20260831",
        "entry_price": 20, "shares": 100, "source": "manual",
    })

    with pytest.raises(ValueError, match="独立账户"):
        paper.import_backtest_run("test", reset=True)
    imported = paper.import_backtest_run("test", reset=True, account_key="history_test")

    assert imported["account_key"] == "history_test"
    assert repository.get_open_position("000002", "default")
    assert repository.get_open_position("000001", "history_test")
    assert len(repository.list_trades("default")) == 1
