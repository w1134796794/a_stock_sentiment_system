from __future__ import annotations

import pandas as pd

from core.infrastructure.shared_state import MemoryStateBackend
from core.notifications.notifier import NotificationService
from core.portfolio.exit_decision_service import ExitDecisionService
from core.portfolio.holding_repository import HoldingRepository
from core.portfolio.holding_service import HoldingService
from core.portfolio.paper_trading_service import PaperTradingService
from core.portfolio.position_monitor import PositionMonitor


def _position(**overrides):
    data = {
        "id": 1,
        "code": "000001",
        "name": "测试股份",
        "entry_date": "20260801",
        "entry_price": 10.0,
        "last_price": 10.0,
        "high_watermark": 10.0,
        "shares": 1000,
        "cost_amount": 10_000.0,
        "strategy_id": "weak_to_strong",
        "structural_stop": 0.0,
        "emergency_loss_pct": 6.0,
    }
    data.update(overrides)
    return data


def _quote(**overrides):
    data = {
        "code": "000001",
        "last_price": 10.2,
        "open_price": 10.0,
        "high_price": 10.3,
        "change_pct": 2.0,
        "is_stale": False,
        "date": "20260803",
        "time": "09:36:00",
        "pre_close": 10.0,
    }
    data.update(overrides)
    return data


def test_repository_records_partial_and_full_sell(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    opened = repository.open_position(
        {
            "code": "000001.SZ",
            "name": "测试股份",
            "entry_date": "20260801",
            "entry_time": "09:36:00",
            "entry_price": 10.0,
            "shares": 1000,
            "strategy_id": "weak_to_strong",
        }
    )

    partial = repository.sell_position(
        opened["id"],
        {
            "trade_date": "20260803",
            "trade_time": "10:15:00",
            "price": 11.0,
            "shares": 400,
            "reason": "减仓",
        },
    )
    closed = repository.sell_position(
        opened["id"],
        {
            "trade_date": "20260804",
            "trade_time": "14:35:00",
            "price": 9.5,
            "shares": 600,
            "reason": "退出",
        },
    )

    assert partial["shares"] == 600
    assert partial["status"] == "open"
    assert closed["shares"] == 0
    assert closed["status"] == "closed"
    trades = repository.list_trades()
    assert [row["action"] for row in trades] == ["sell", "sell", "buy"]
    assert [row["trade_time"] for row in trades] == ["14:35:00", "10:15:00", "09:36:00"]


def test_market_weakness_alone_does_not_force_sell():
    decision = ExitDecisionService().evaluate(
        _position(),
        _quote(),
        market_context={"market_score": 25, "regime": "退潮", "reason": "主要指数转弱"},
        sector_context={"state": True, "breadth": 0.7, "index_change_pct": 1.0},
        signal_date="20260803",
    )

    assert decision.action == "watch"
    assert decision.can_sell is True
    assert "主要指数转弱" in "；".join(decision.reasons)


def test_structural_break_sells_but_t1_new_position_is_blocked():
    service = ExitDecisionService()
    position = _position(structural_stop=9.8)
    quote = _quote(last_price=9.7, high_price=10.1, change_pct=-3.0)

    sell = service.evaluate(position, quote, signal_date="20260803")
    blocked = service.evaluate(position, quote, signal_date="20260801")

    assert sell.action == "sell"
    assert sell.can_sell is True
    assert blocked.action == "blocked"
    assert blocked.can_sell is False


def test_exit_signal_is_persisted_only_when_action_changes(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    position = repository.open_position(
        {"code": "000001", "entry_date": "20260801", "entry_price": 10, "shares": 100}
    )
    payload = {
        "action": "sell",
        "action_label": "建议卖出",
        "signal_date": "20260803",
        "current_price": 9.2,
        "reason": "风险共振",
    }

    first = repository.save_exit_signal(position["id"], payload)
    second = repository.save_exit_signal(position["id"], payload)

    assert first["changed"] is True
    assert second["changed"] is False
    assert len(repository.list_exit_signals()) == 1


class _FakeQuotes:
    def __init__(self, quote):
        self.quote = quote

    def get_quotes(self, codes):
        return {"ok": True, "quotes": [{**self.quote, "code": codes[0]}]}


class _FakeSector:
    def evaluate(self, sectors, market_date):
        return False, {
            "breadth": 0.2,
            "index_change_pct": -2.0,
            "data_completeness": 1.0,
            "reason": "所属板块同步转弱",
        }


class _FakeNotifier:
    def __init__(self):
        self.calls = []

    def notify_exit_signal(self, position, decision, *, signal_date=""):
        self.calls.append((position, decision, signal_date))
        return {"ok": True, "sent": 1}


def test_monitor_updates_holding_and_notifies_once_per_action(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    opened = repository.open_position(
        {
            "code": "000001",
            "name": "测试股份",
            "entry_date": "20260801",
            "entry_price": 10.0,
            "shares": 1000,
            "sector_names": "机器人",
            "structural_stop": 9.8,
        }
    )
    notifier = _FakeNotifier()
    monitor = PositionMonitor(
        repository=repository,
        quote_service=_FakeQuotes(_quote(last_price=9.7, high_price=10.0, change_pct=-3.0)),
        sector_breadth_provider=_FakeSector(),
        market_context_provider=lambda: {"market_score": 25, "regime": "退潮"},
        notifier=notifier,
        backend=MemoryStateBackend("portfolio-test"),
    )

    first = monitor.run_once(signal_date="20260803")
    second = monitor.run_once(signal_date="20260803")
    current = repository.get_position(opened["id"])

    assert first["ok"] is True
    assert first["notified"] == 1
    assert second["notified"] == 0
    assert current["latest_action"] == "sell"
    assert current["last_price"] == 9.7
    assert len(notifier.calls) == 1


def test_dashboard_keeps_last_price_when_quote_is_missing(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    repository.open_position(
        {"code": "000001", "entry_date": "20260801", "entry_price": 10, "shares": 100}
    )
    monitor = PositionMonitor(
        repository=repository,
        quote_service=_FakeQuotes({"last_price": 0, "is_stale": True}),
        market_context_provider=lambda: {},
        notifier=_FakeNotifier(),
        backend=MemoryStateBackend("portfolio-missing-quote"),
    )

    monitor.run_once(signal_date="20260803")
    data = HoldingService(repository).dashboard()

    assert data["positions"][0]["last_price"] == 10.0
    assert data["positions"][0]["latest_action"] == "data_insufficient"


def test_position_without_manual_structure_price_uses_visible_risk_floor(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    opened = repository.open_position(
        {
            "code": "000001", "entry_date": "20260801",
            "entry_price": 10, "shares": 100, "emergency_loss_pct": 6,
        }
    )
    dashboard = HoldingService(repository).dashboard()
    decision = ExitDecisionService().evaluate(opened, _quote(), signal_date="20260803")

    assert opened["structural_stop"] == 9.4
    assert dashboard["positions"][0]["structural_stop"] == 9.4
    assert dashboard["positions"][0]["protection_price_source"] == "账户风险底线"
    assert decision.protect_price == 9.4


def test_exit_notification_is_plain_chinese(monkeypatch):
    service = NotificationService(backend=MemoryStateBackend("portfolio-notify"))
    calls = []

    def fake_send(title, content, **kwargs):
        calls.append((title, content, kwargs))
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(service, "send", fake_send)
    result = service.notify_exit_signal(
        _position(),
        {
            "action": "sell",
            "action_label": "建议卖出",
            "current_price": 9.5,
            "protect_price": 9.8,
            "pnl_pct": -5,
            "market_state": "转弱",
            "sector_state": "转弱",
            "can_sell": True,
            "reasons": ["个股、板块和市场共振转弱"],
        },
        signal_date="20260803",
    )

    assert result["sent"] == 1
    assert calls[0][0] == "持仓卖出提醒：测试股份"
    assert "建议动作：建议卖出" in calls[0][1]
    assert "保护价格：9.80" in calls[0][1]
    assert calls[0][2]["event_key"] == "portfolio-exit:20260803:1:sell"


def test_confirmed_realtime_signal_opens_only_one_simulation_position(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(
        repository,
        initial_capital=100_000,
        max_positions=4,
        position_pct=10,
        quote_service=_FakeQuotes(_quote(last_price=10.0, pre_close=10.0)),
    )
    payload = {
        "market_date": "20260803",
        "candidate_date": "20260802",
        "profile": "weak_to_strong",
        "strategy": {"id": "weak_to_strong", "name": "弱转强修复"},
        "rows": [
            {
                "code": "000001",
                "name": "测试股份",
                "confirm_status": "confirmed",
                "entry_price": 10.0,
                "entry_time": "09:36:00",
                "entry_mode": "weak_to_strong",
                "entry_mode_text": "弱转强",
                "resonance_sectors": "机器人",
                "执行仓位上限%": 10,
            }
        ],
    }

    first = service.process_realtime_payload(payload)
    second = service.process_realtime_payload(payload)
    assert first["opened"] == 1
    assert second["opened"] == 0
    assert len(repository.list_positions("default")) == 1
    assert 90_000 < repository.account("default")["cash"] < 91_000
    assert repository.list_trades("default")[0]["fees"] > 0


def test_paper_buy_rejects_quote_before_entry_time(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(
        repository, initial_capital=100_000,
        quote_service=_FakeQuotes(_quote(last_price=10, pre_close=10, time="09:35:59")),
    )
    result = service.process_realtime_payload({
        "market_date": "20260803", "candidate_date": "20260802",
        "rows": [{"code": "000001", "confirm_status": "confirmed",
                  "entry_price": 9.9, "entry_time": "09:36:00"}],
    })
    assert result["opened"] == 0
    assert "成交报价早于预定成交时间" in result["skipped"]
    assert repository.list_trades("default") == []


def test_missing_holding_quote_is_audited_then_recovers(tmp_path):
    import json
    from types import SimpleNamespace
    from core.operations.ledger import TradingLedger

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(repository, initial_capital=100_000,
                                  quote_service=_FakeQuotes(_quote()))
    repository.open_position(_position(code="000002"))
    payload = {"market_date": "20260803", "rows": [{"code": "000001", "confirm_status": "confirmed",
               "entry_time": "09:36:00", "entry_price": 10.2, "执行仓位上限%": 8, "市场总仓位上限%": 15}]}
    first = service.process_realtime_payload(payload)
    assert first["opened"] == 0
    with TradingLedger(repository.db_path)._connect() as conn:
        record = conn.execute("SELECT payload_json FROM trading_events WHERE kind='paper_order_rejected'").fetchone()
    assert set(json.loads(record[0])["holding_quote_errors"]) == {"000002"}
    service.quote_service = SimpleNamespace(get_quotes=lambda codes: {"quotes": [
        _quote(code=code, last_price=9.0 if code == "000002" else 10.2) for code in codes]})
    assert service.process_realtime_payload(payload)["opened"] == 1
    positions = repository.list_positions()
    assert next(p for p in positions if p["code"] == "000001")["shares"] == 500
    assert service._position_pct({"执行仓位上限%": 0, "position_budget_pct": 12}) == 0
    assert service._position_pct({"screening_score": 85}) == 12


def test_dashboard_uses_dated_snapshot_without_writing_ledger(tmp_path, monkeypatch):
    from datetime import datetime
    from core.realtime import trading_session
    from core.realtime.quote_service import RealtimeQuoteService

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = HoldingService(repository)
    repository.open_position(_position(code="300006", entry_price=9.55))
    fixed = trading_session.shanghai_now(datetime(2026, 8, 3, 10, 0))
    monkeypatch.setattr(trading_session, "shanghai_now", lambda *_: fixed)
    monkeypatch.setattr(RealtimeQuoteService, "get_quotes", lambda *_: {"quotes": [
        _quote(code="300006", last_price=10.24, time="09:59:59")]})
    mark = service.dashboard()["positions"][0]
    assert (mark["last_price"], mark["valuation_basis"], mark["valuation_date"]) == (10.24, "snapshot", "20260803")
    assert service.dashboard()["summary"]["market_value"] == 10_240
    assert repository.list_positions()[0]["last_price"] == 9.55
    monkeypatch.setattr(RealtimeQuoteService, "get_quotes", lambda *_: {"quotes": [
        _quote(code="300006", date="20260731", last_price=10.24)]})
    assert service.dashboard()["positions"][0]["valuation_basis"] == "ledger"


def test_dashboard_uses_archived_close_only_after_close(tmp_path, monkeypatch):
    from datetime import datetime
    from config import settings
    from core.infrastructure.duckdb_access import connect
    from core.realtime import trading_session
    from core.realtime.quote_service import RealtimeQuoteService

    path = tmp_path / "factors.duckdb"
    with connect(path) as con:
        con.execute("CREATE TABLE stock_daily_silver AS SELECT '300006' AS code, '20260803' AS trade_date, 10.24 AS close")
    monkeypatch.setattr(settings, "FACTOR_DB_PATH", path)
    monkeypatch.setattr(RealtimeQuoteService, "get_quotes", lambda *_: {"quotes": []})
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = HoldingService(repository)
    repository.open_position(_position(code="300006", entry_price=9.55))
    localize = trading_session.shanghai_now
    for hour, expected in ((14, "ledger"), (15, "daily_close")):
        fixed = localize(datetime(2026, 8, 3, hour))
        monkeypatch.setattr(trading_session, "shanghai_now", lambda *_: fixed)
        assert service.dashboard()["positions"][0]["valuation_basis"] == expected
    assert service.dashboard()["positions"][0]["last_price"] == 10.24


def test_formal_confirmation_without_simulated_price_waits_for_later_quote_then_buys(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    quote = _quote(last_price=10.0, time="09:36:00")
    service = PaperTradingService(repository, quote_service=_FakeQuotes(quote))
    payload = {"market_date": "20260803", "rows": [{"code": "000001", "confirm_status": "confirmed",
        "formal_confirmed_at": "09:36:00", "confirm_time": "09:36:00", "entry_time": "", "entry_price": None}]}
    waiting = service.process_realtime_payload(payload)
    assert waiting["opened"] == 0 and "等待正式确认之后的新成交报价" in waiting["skipped"]
    service.quote_service = _FakeQuotes(_quote(last_price=10.0, time="09:36:03"))
    assert service.process_realtime_payload(payload)["opened"] == 1
    assert service.process_realtime_payload(payload)["opened"] == 0
    assert repository.list_trades("default")[0]["trade_time"] == "09:36:03"


def test_manual_and_automatic_buys_share_one_million_account(tmp_path):
    from types import SimpleNamespace

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    holding_service = HoldingService(repository)
    holding_service.add_buy(
        {
            "code": "000001",
            "name": "手工股票",
            "entry_date": "20260803",
            "entry_price": 10.0,
            "shares": 1000,
            "source": "manual",
        }
    )
    trading_service = PaperTradingService(
        repository,
        quote_service=SimpleNamespace(get_quotes=lambda codes: {"quotes": [
            _quote(code=code, last_price=20.0 if code == "000002" else 10.0,
                   pre_close=20.0 if code == "000002" else 10.0,
                   time="09:38:00") for code in codes
        ]}),
    )
    result = trading_service.process_realtime_payload(
        {
            "market_date": "20260803",
            "candidate_date": "20260802",
            "profile": "first_board_launch",
            "rows": [
                {
                    "code": "000002",
                    "name": "自动股票",
                    "confirm_status": "confirmed",
                    "entry_price": 20.0,
                    "entry_time": "09:38:00",
                    "entry_mode_text": "强势延续",
                }
            ],
        }
    )
    dashboard = holding_service.dashboard()

    assert result["opened"] == 1
    assert dashboard["account"]["initial_capital"] == 1_000_000
    assert 910_000 < dashboard["summary"]["cash"] < 915_000
    assert dashboard["summary"]["total_assets"] < 1_000_000
    assert len(dashboard["positions"]) == 2


def test_live_account_allows_more_than_three_manual_and_automatic_positions(tmp_path):
    from types import SimpleNamespace

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    quotes = SimpleNamespace(get_quotes=lambda codes: {"quotes": [_quote(code=code) for code in codes]})
    service = PaperTradingService(repository, initial_capital=100_000, quote_service=quotes)
    holdings = HoldingService(repository)
    for index in range(1, 5):
        holdings.add_buy(_position(code=f"{index:06d}", shares=100))

    result = service.process_realtime_payload({"market_date": "20260803", "rows": [{
        "code": "000006", "confirm_status": "confirmed", "entry_price": 10.2,
        "entry_time": "09:36:00", "执行仓位上限%": 8, "市场总仓位上限%": 25,
    }]})

    assert result["opened"] == 1, result["skipped"]
    assert result["rotated"] == 0
    dashboard = holdings.dashboard()
    assert dashboard["summary"]["position_count"] == 5
    assert dashboard["policy"]["max_positions"] is None


def test_stronger_confirmed_signal_rotates_out_weakest_t1_holding_when_budget_is_full(tmp_path):
    from types import SimpleNamespace

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(
        repository,
        initial_capital=100_000,
        position_pct=34,
        rotation_min_edge=6,
        quote_service=SimpleNamespace(get_quotes=lambda codes: {"quotes": [
            _quote(code=code, last_price=20 if code == "000009" else 10,
                   pre_close=20 if code == "000009" else 10) for code in codes
        ]}),
    )
    for index, strength in enumerate((45, 62, 74), start=1):
        repository.open_position(
            {
                "code": f"00000{index}",
                "name": f"持仓{index}",
                "entry_date": "20260801",
                "entry_price": 10.0,
                "shares": 1000,
                "metadata": {"entry_strength_score": strength},
            },
            "default",
        )

    result = service.process_realtime_payload(
        {
            "market_date": "20260803",
            "candidate_date": "20260802",
            "profile": "weak_to_strong",
            "rows": [
                {
                    "code": "000009",
                    "name": "更强股票",
                    "confirm_status": "confirmed",
                    "entry_price": 20.0,
                    "entry_time": "09:36:00",
                    "entry_mode": "weak_to_strong",
                    "screening_score": 82,
                    "市场总仓位上限%": 30,
                }
            ],
        }
    )

    assert result["opened"] == 1
    assert result["rotated"] == 1
    assert result["rotations"][0]["sold_code"] == "000001"
    assert repository.get_open_position("000001", "default") == {}
    replacement = repository.get_open_position("000009", "default")
    assert replacement["metadata"]["rotation"]["sold_code"] == "000001"
    assert len(repository.list_positions("default")) == 3
    actions = [row["action"] for row in repository.list_trades("default")]
    assert actions.count("sell") == 1
    assert actions.count("buy") == 4


def test_rotation_respects_t1_and_strength_edge(tmp_path):
    from types import SimpleNamespace

    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(
        repository,
        initial_capital=100_000,
        max_positions=1,
        position_pct=100,
        rotation_min_edge=6,
        quote_service=SimpleNamespace(get_quotes=lambda codes: {"quotes": [
            _quote(code=code, last_price=10, pre_close=10) for code in codes
        ]}),
    )
    repository.open_position(
        {
            "code": "000001",
            "entry_date": "20260803",
            "entry_price": 10.0,
            "shares": 10_000,
            "metadata": {"entry_strength_score": 40},
        },
        "default",
    )
    payload = {
        "market_date": "20260803",
        "profile": "weak_to_strong",
        "rows": [
            {
                "code": "000002",
                "confirm_status": "confirmed",
                "entry_price": 10.0,
                "entry_mode": "weak_to_strong",
                "screening_score": 90,
                "entry_time": "09:36:00",
            }
        ],
    }

    same_day = service.process_realtime_payload(payload)
    assert same_day["opened"] == 0
    assert same_day["rotated"] == 0
    assert same_day["skipped"] == {"没有可换出的更弱持仓": 1}

    payload["market_date"] = "20260804"
    service.quote_service = type("Quotes", (), {"get_quotes": lambda self, codes: {
        "quotes": [_quote(code=code, date="20260804", last_price=10) for code in codes]
    }})()
    payload["rows"][0]["screening_score"] = 40
    not_stronger = service.process_realtime_payload(payload)
    assert not_stronger["opened"] == 0
    assert not_stronger["rotated"] == 0
    assert repository.get_open_position("000001", "default")


def test_simulation_monitor_executes_sell_decision(tmp_path):
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    repository.ensure_account("default", name="模拟持仓账户", initial_capital=100_000)
    opened = repository.open_position(
        {
            "code": "000001",
            "name": "测试股份",
            "entry_date": "20260801",
            "entry_price": 10.0,
            "shares": 1000,
            "structural_stop": 9.8,
        },
        "default",
    )
    monitor = PositionMonitor(
        repository=repository,
        quote_service=_FakeQuotes(_quote(last_price=9.7, high_price=10.0, change_pct=-3.0)),
        sector_breadth_provider=_FakeSector(),
        market_context_provider=lambda: {"market_score": 25, "regime": "退潮"},
        notifier=_FakeNotifier(),
        backend=MemoryStateBackend("paper-auto-exit"),
    )

    result = monitor.run_once(
        account_key="default",
        signal_date="20260803",
        auto_execute=True,
    )

    assert result["executed"] == 1
    assert repository.get_position(opened["id"])["status"] == "closed"
    from core.portfolio.execution_quotes import execution_price
    from risk.risk_config import RiskConfig
    costs = RiskConfig.load()
    quote = _quote(last_price=9.7, high_price=10.0, change_pct=-3.0)
    sell_price = execution_price(quote, side='sell', reference_time=quote['time'], slippage=costs.slippage)
    expected_cash = 90_000 + sell_price * 1000 * (1 - costs.commission_rate - costs.stamp_duty_rate)
    assert abs(repository.account('default')['cash'] - expected_cash) < .00001
    assert repository.list_trades('default')[0]['fees'] > 0


def test_import_historical_backtest_trades_into_simulation_account(tmp_path, monkeypatch):
    import core.portfolio.paper_trading_service as paper_module

    result_dir = tmp_path / "backtest_results"
    result_dir.mkdir()
    run_id = "20260809_120000"
    (result_dir / f"backtest_trades_{run_id}.csv").write_text(
        "date,stock_code,stock_name,action,entry_date,entry_time,entry_price,exit_price,shares,exit_reason,strategy_id,strategy_name\n"
        "20260602,000001,测试股份,BUY,20260602,09:36:00,10,0,1000,,weak_to_strong,弱转强修复\n"
        "20260605,000001,测试股份,SELL,20260602,09:36:00,10,11,1000,动态退出,weak_to_strong,弱转强修复\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paper_module, "OUTPUT_DIR", tmp_path)
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(repository, initial_capital=100_000)

    result = service.import_backtest_run(run_id)

    assert result["buy"] == 1
    assert result["sell"] == 1
    assert repository.list_positions("default") == []
    assert repository.account("default")["cash"] == 101_000


def test_replay_prefetches_missing_minute_and_auction_data(tmp_path, monkeypatch):
    import config.settings as settings
    import core.data as data_module
    import scripts.prefetch_strategy_minutes as prefetch_module

    calls = {"minute": [], "auction": []}

    class FakeDataManager:
        def __init__(self, *args, **kwargs):
            assert kwargs["allow_remote_history"] is True

        def get_stock_tick(self, ts_code, trade_date, **kwargs):
            calls["minute"].append((trade_date, ts_code))
            times = [f"{minute // 60:02d}:{minute % 60:02d}:00"
                     for start in (570, 780) for minute in range(start + 1, start + 121)]
            return pd.DataFrame({"trade_date": trade_date, "time": times, "close": 10, "volume": 100})

        def get_auction_data(self, ts_code, trade_date):
            calls["auction"].append((trade_date, ts_code))
            return {"open": 10.0}

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("backtest.trade_calendar.TradeCalendar", lambda: type("Calendar", (), {
        "is_real": True, "prev": lambda self, date: "20260529"})())
    monkeypatch.setattr(data_module, "DataManager", FakeDataManager)
    monkeypatch.setattr(prefetch_module, "_minute_cached", lambda *args: False)
    monkeypatch.setattr(
        prefetch_module,
        "build_requirements",
        lambda *args, **kwargs: (
            {("20260602", "000001.SZ")},
            {("20260602", "000001.SZ")},
            {"candidate_rows": 1, "minute_requirements": 1},
        ),
    )
    repository = HoldingRepository(tmp_path / "portfolio.sqlite")
    service = PaperTradingService(repository)
    progress = []

    result = service.prefetch_replay_minutes(
        "20260601",
        "20260630",
        progress=progress.append,
    )

    assert result["minute_fetched"] == 1
    assert result["auction_fetched"] == 1
    assert calls == {
        "minute": [("20260602", "000001.SZ")],
        "auction": [("20260602", "000001.SZ")],
    }
    assert progress[-1]["stage"] == "分钟证据准备完成"
