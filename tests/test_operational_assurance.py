from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.operations.evidence import attach_evidence
from core.operations.equity import AccountEquityEvaluator
from core.operations.experiments import StrategyExperimentLedger
from core.operations.health import gate_realtime_payload, postclose_health
from core.operations.ledger import TradingLedger, stable_id
from core.factors.jobs.runner import FactorJobRunner
from core.portfolio.execution_quotes import execution_price
from core.portfolio.holding_repository import HoldingRepository
from core.realtime.sector_breadth import RealtimeSectorBreadthProvider
from core.screening.strategy_profiles import PRODUCTION_STRATEGY_IDS


def test_signal_evidence_and_event_ids_are_idempotent(tmp_path: Path) -> None:
    ledger = TradingLedger(tmp_path / "portfolio.sqlite")
    payload = {"candidate_date": "20260924", "market_date": "20260925", "profile": "decision_pool",
               "rows": [{"code": "000001", "confirm_status": "confirmed", "entry_mode": "weak_to_strong",
                         "confirm_time": "09:45:00", "entry_time": "09:46:00", "entry_price": 12.3,
                         "strategy_version": "v2", "sector_detail": {"observed_at": "2026-09-25T09:45:03",
                                                                      "data_completeness": 1.0}}]}
    attach_evidence(payload)
    row = payload["rows"][0]
    first_id = row["signal_id"]
    attach_evidence(payload)
    assert row["signal_id"] == first_id
    assert row["signal_evidence"]["entry_time"] == "09:46:00"
    assert row["signal_evidence"]["screening_score"] is None
    ledger.evidence(first_id, row["signal_evidence"])
    event_id = stable_id(first_id, "signal", "confirmed")
    assert ledger.event(event_id, "signal", row["signal_evidence"], signal_id=first_id)
    assert not ledger.event(event_id, "signal", row["signal_evidence"], signal_id=first_id)
    assert len(ledger.timeline(first_id)["events"]) == 1


def test_intraday_gate_demotes_stale_confirmation() -> None:
    now = datetime(2026, 9, 25, 9, 46, tzinfo=ZoneInfo("Asia/Shanghai"))
    collector = {"storage": "redis", "trade_date": "20260925",
                 "collector": {"ok": True, "lease_owned": True, "updated_at": "2026-09-25T09:45:59"}}
    payload = {"market_date": "20260925", "counts": {"confirmed": 1}, "rows": [
        {"confirm_status": "confirmed", "received_at": "2026-09-25T09:45:59",
         "sector_detail": {"observed_at": "2026-09-25T09:45:59", "data_completeness": 1.0,
                           "member_count": 10, "observed_members": 10}},
    ]}
    gate_realtime_payload(payload, collector=collector, now=now, require_shared=True)
    assert payload["rows"][0]["confirm_status"] == "confirmed"
    payload["rows"][0]["sector_detail"]["data_completeness"] = 0.5
    gate_realtime_payload(payload, collector=collector, now=now, require_shared=True)
    assert payload["rows"][0]["confirm_status"] == "observe"
    assert "板块" in payload["rows"][0]["reason"]
    assert payload["counts"]["confirmed"] == 0


def test_intraday_gate_requires_constituent_coverage() -> None:
    now = datetime(2026, 9, 25, 9, 46, tzinfo=ZoneInfo("Asia/Shanghai"))
    collector = {"storage": "redis", "trade_date": "20260925",
                 "collector": {"ok": True, "lease_owned": True, "updated_at": "2026-09-25T09:45:59"}}
    payload = {"market_date": "20260925", "rows": [
        {"confirm_status": "confirmed", "received_at": "2026-09-25T09:45:59",
         "sector_detail": {"observed_at": "2026-09-25T09:45:59", "data_completeness": 1.0,
                           "member_count": 80, "observed_members": 4}},
    ]}
    gate_realtime_payload(payload, collector=collector, now=now, require_shared=True)
    assert payload["rows"][0]["confirm_status"] == "observe"
    assert payload["rows"][0]["health_gate"]["sector_coverage"] == 0.05


def test_sector_breadth_excludes_stale_member_quotes(tmp_path: Path) -> None:
    provider = RealtimeSectorBreadthProvider(duckdb_path=tmp_path / "missing.duckdb")
    provider._members = lambda *_args: {f"{index:06d}" for index in range(10)}
    provider._stock_quotes = lambda _codes: {
        f"{index:06d}": {"change_pct": 2.0, "is_stale": index >= 4}
        for index in range(10)
    }
    provider._sector_changes = lambda _names: [1.0]
    _, detail = provider.evaluate(["测试板块"], "20260925")
    assert detail["member_count"] == 10
    assert detail["observed_members"] == 4


def test_paper_fill_and_position_change_share_signal_id(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.sqlite"
    repository = HoldingRepository(db)
    repository.ensure_account("default", name="模拟", initial_capital=100_000)
    signal_id = stable_id("signal", "20260924", "20260925", "000001")
    position = repository.open_position({
        "code": "000001", "name": "测试", "entry_date": "20260925", "entry_time": "09:46:00",
        "entry_price": 10, "shares": 100, "source": "auto_realtime",
        "metadata": {"signal_id": signal_id},
    })
    timeline = TradingLedger(db).timeline(signal_id)
    assert {event["kind"] for event in timeline["events"]} == {"paper_fill", "position_changed"}
    assert all(event["payload"]["position_id"] == position["id"] for event in timeline["events"])


def test_postclose_requires_matching_dates_and_all_factor_jobs(tmp_path: Path, monkeypatch) -> None:
    import core.operations.health as health

    date = "20260925"
    monkeypatch.setattr(health, "fetch_status", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(health, "factor_status", lambda *_args, **_kwargs: {"ready": True})
    (tmp_path / "factor_status").mkdir()
    (tmp_path / "screening" / "decision_pool").mkdir(parents=True)
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "factor_status" / f"factors_{date}.json").write_text(json.dumps({
        "trade_date": date, "jobs": [{"name": job.name, "ok": True}
                                      for job in FactorJobRunner.JOBS.values()],
    }))
    (tmp_path / "screening" / "decision_pool" / f"decision_pool_{date}.json").write_text(
        json.dumps({"trade_date": date}))
    (tmp_path / "snapshots" / f"{date}.json").write_text(json.dumps({"meta": {"date": date}}))
    assert postclose_health(date, db_path=tmp_path / "db", web_data_dir=tmp_path,
                            snapshot_dir=tmp_path / "snapshots")["ok"]
    (tmp_path / "snapshots" / f"{date}.json").write_text(json.dumps({"meta": {"date": "20260924"}}))
    assert not postclose_health(date, db_path=tmp_path / "db", web_data_dir=tmp_path,
                                snapshot_dir=tmp_path / "snapshots")["ok"]


def test_postclose_rejects_artifacts_generated_after_market_open(tmp_path: Path, monkeypatch) -> None:
    import core.operations.health as health

    date = "20260924"
    monkeypatch.setattr(health, "fetch_status", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(health, "factor_status", lambda *_args, **_kwargs: {"ready": True})
    (tmp_path / "fetch_status").mkdir()
    (tmp_path / "factor_status").mkdir()
    (tmp_path / "screening" / "decision_pool").mkdir(parents=True)
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "fetch_status" / f"fetch_{date}.json").write_text(json.dumps({
        "fetched_at": "2026-09-24T20:00:00",
    }))
    (tmp_path / "factor_status" / f"factors_{date}.json").write_text(json.dumps({
        "trade_date": date, "generated_at": "2026-09-24T20:10:00",
        "jobs": [{"name": name, "ok": True}
                 for name in ("market", "lhb", "signals", "sector", "stock")],
    }))
    pool_path = tmp_path / "screening" / "decision_pool" / f"decision_pool_{date}.json"
    pool_path.write_text(json.dumps({"trade_date": date, "generated_at": "2026-09-25T09:30:00"}))
    (tmp_path / "snapshots" / f"{date}.json").write_text(json.dumps({
        "meta": {"date": date, "generated_at": "2026-09-24T20:20:00"},
    }))
    result = postclose_health(date, db_path=tmp_path / "db", web_data_dir=tmp_path,
                              snapshot_dir=tmp_path / "snapshots", as_of_market_date="20260925")
    assert not result["ok"]
    assert "决策池不具备开盘前时点证据" in result["reasons"]


def test_experiment_freeze_and_historical_fold(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.sqlite"
    HoldingRepository(db)
    ledger = TradingLedger(db)

    class Profiles:
        @staticmethod
        def get_profile(name):
            return {"id": name, "version": "frozen"}

    service = StrategyExperimentLedger(root=tmp_path, ledger=ledger, profiles=Profiles())
    service._factor_hash = lambda: "factor-v1"
    frozen = service.freeze()
    for date in ("20260101", "20260102", "20260103", "20260104"):
        (tmp_path / "screening" / "decision_pool").mkdir(parents=True, exist_ok=True)
        (tmp_path / "screening" / "decision_pool" / f"decision_pool_{date}.json").write_text(
            json.dumps({"trade_date": date, "regime": "neutral", "generated_at": f"{date[:4]}-{date[4:6]}-{date[6:]}T20:00:00",
                        "rows": [{"code": "000001", "strategy_id": name} for name in PRODUCTION_STRATEGY_IDS]}))
    report = service.evaluate(frozen["experiment_id"], start="20260101", end="20260104",
                              train_days=2, validation_days=2)
    assert len(report["folds"]) == 1
    assert report["folds"][0]["classification"] == "historical_descriptive"
    metrics = report["folds"][0]["metrics"]
    assert {row["strategy_id"] for row in metrics} == set(PRODUCTION_STRATEGY_IDS)
    assert all(row["candidates"] == 2 and row["mean_closed_trade_return_pct"] is None for row in metrics)
    assert report["equity"]["status"] == "no_funded_account"


def test_equity_includes_open_loss_and_fees(tmp_path: Path) -> None:
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    repository.ensure_account("default", name="test", initial_capital=100_000)
    opened = repository.open_position({
        "code": "000001", "entry_date": "20260102", "entry_price": 10,
        "shares": 1000, "fees": 3,
    })
    repository.sell_position(opened["id"], {
        "trade_date": "20260106", "price": 10, "shares": 1000, "fees": 13,
    })
    ledger = TradingLedger(repository.db_path)
    evaluator = AccountEquityEvaluator(ledger)
    prices = [
        {"trade_date": "20260102", "code": "000001", "close": 11},
        {"trade_date": "20260105", "code": "000001", "close": 9},
    ]
    result = evaluator.evaluate(
        account_key="default", start="20260102", end="20260106", experiment_id="e1",
        price_rows=prices, calendar=["20260102", "20260105", "20260106"],
    )
    assert result["status"] == "complete"
    assert [row["equity"] for row in result["rows"]] == [100_997, 98_997, 99_984]
    assert result["summary"]["fees"] == 16
    assert result["summary"]["max_drawdown_pct"] < -1.9
    with ledger._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM account_equity_daily").fetchone()[0] == 3
    prices.pop()
    missing = evaluator.evaluate(
        account_key="default", start="20260102", end="20260106", experiment_id="e2",
        price_rows=prices, calendar=["20260102", "20260105", "20260106"],
    )
    assert missing["rows"][1]["equity"] is None
    assert missing["summary"]["total_return_pct"] is None


def test_equity_window_does_not_count_earlier_trades_as_current_turnover(tmp_path: Path) -> None:
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    repository.ensure_account("default", name="test", initial_capital=100_000)
    repository.open_position({
        "code": "000001", "entry_date": "20260102", "entry_price": 10,
        "shares": 1000, "fees": 3,
    })
    result = AccountEquityEvaluator(TradingLedger(repository.db_path)).evaluate(
        account_key="default", start="20260105", end="20260105", experiment_id="window",
        price_rows=[{"trade_date": "20260105", "code": "000001", "close": 11}],
        calendar=["20260105"],
    )
    assert result["rows"][0]["equity"] == 100_997
    assert result["rows"][0]["trade_notional"] == 0
    assert result["summary"]["buys"] == 0
    assert result["summary"]["fees"] == 0


def test_execution_price_uses_market_tick_and_rejects_limit_price() -> None:
    quote = {"time": "09:46:01", "last_price": 10.03, "pre_close": 10,
             "code": "000001", "name": "测试"}
    assert execution_price(quote, side="buy", reference_time="09:46:00", slippage=0.001) == 10.04
    quote["last_price"] = 10.99
    try:
        execution_price(quote, side="buy", reference_time="09:46:00", slippage=0.001)
    except ValueError as exc:
        assert "涨跌停" in str(exc)
    else:
        raise AssertionError("应拒绝触及涨停的模拟成交")


def test_experiment_joins_consensus_signal_to_primary_trade_once(tmp_path: Path) -> None:
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    repository.ensure_account("default", name="test", initial_capital=100_000)
    ledger = TradingLedger(repository.db_path)

    class Profiles:
        @staticmethod
        def get_profile(name):
            return {"id": name, "version": "frozen"}

    service = StrategyExperimentLedger(root=tmp_path, ledger=ledger, profiles=Profiles())
    service._factor_hash = lambda: "factor-v1"
    frozen = service.freeze()
    pool_dir = tmp_path / "screening" / "decision_pool"
    pool_dir.mkdir(parents=True)
    dates = ["20260101", "20260102", "20260103", "20260104", "20260105"]
    for date in dates:
        candidates = ([{
            "code": "000001", "策略ID": "mainline_leader",
            "策略来源": "mainline_leader,weak_to_strong",
            "策略组合评分": 82, "rank": 1, "execution_eligible": True,
        }] if date == "20260103" else [])
        (pool_dir / f"decision_pool_{date}.json").write_text(json.dumps({
            "trade_date": date, "regime": "neutral", "generated_at": f"{date[:4]}-{date[4:6]}-{date[6:]}T20:00:00",
            "rows": candidates,
        }), encoding="utf-8")
    signal_id = stable_id("signal", "20260103", "20260104", "000001")
    ledger.event(stable_id(signal_id, "signal"), "signal", {
        "candidate_date": "20260103", "strategy_id": "mainline_leader",
        "strategy_sources": "mainline_leader,weak_to_strong",
        "code": "000001", "status": "confirmed",
    }, signal_id=signal_id)
    ledger.event(stable_id(signal_id, "first_quote_rejected"), "paper_order_rejected", {
        "code": "000001", "reason": "首次报价过期",
    }, signal_id=signal_id, account_key="default")
    position = repository.open_position({
        "code": "000001", "entry_date": "20260104", "entry_price": 10,
        "shares": 1000, "fees": 3, "strategy_id": "mainline_leader",
        "source": "auto_realtime", "metadata": {"signal_id": signal_id, "candidate_date": "20260103"},
    })
    repository.sell_position(position["id"], {
        "trade_date": "20260105", "price": 11, "shares": 1000, "fees": 14,
    })
    evaluator = AccountEquityEvaluator(ledger)

    class InjectedEquity:
        def evaluate(self, **kwargs):
            return evaluator.evaluate(**kwargs, calendar=dates, price_rows=[
                {"trade_date": "20260104", "code": "000001", "close": 10.5},
            ])

    report = service.evaluate(frozen["experiment_id"], start=dates[0], end=dates[-1],
                              train_days=2, validation_days=3, equity_evaluator=InjectedEquity())
    metrics = {row["strategy_id"]: row for row in report["folds"][0]["metrics"]}
    assert metrics["mainline_leader"]["candidates"] == 1
    assert metrics["weak_to_strong"]["candidates"] == 1
    assert metrics["mainline_leader"]["confirmed"] == 1
    assert metrics["weak_to_strong"]["confirmed"] == 1
    assert metrics["mainline_leader"]["paper_fills"] == 1
    assert metrics["weak_to_strong"]["paper_fills"] == 1
    assert metrics["mainline_leader"]["rejected_orders"] == 0
    assert metrics["weak_to_strong"]["rejected_orders"] == 0
    assert metrics["mainline_leader"]["closed_trades"] == 1
    assert metrics["weak_to_strong"]["closed_trades"] == 0
    assert report["equity"]["summary"]["total_return_pct"] == 0.983
