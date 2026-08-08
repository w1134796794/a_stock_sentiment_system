from __future__ import annotations

from core.automation.internal_scheduler import InternalScheduler
from core.infrastructure.shared_state import MemoryStateBackend
from core.models.health_monitor import ModelHealthMonitor
from core.notifications.notifier import NotificationService
from core.realtime.auction_alert_service import AuctionAlertService
from risk.capital_presets import resolve_capital_preset


def test_small_capital_presets_concentrate_without_exceeding_limits():
    small = resolve_capital_preset(50_000)
    medium = resolve_capital_preset(200_000)
    standard = resolve_capital_preset(1_000_000)

    assert small.max_positions == 3
    assert medium.max_positions == 5
    assert standard.max_positions == 8
    assert small.max_position_per_stock > medium.max_position_per_stock > standard.max_position_per_stock


def test_auction_gap_classification_is_plain_language():
    assert AuctionAlertService._classify(-4.0)[0] == "大幅低开"
    assert AuctionAlertService._classify(0.5)[0] == "弱转强观察"
    assert AuctionAlertService._classify(3.0)[0] == "强势延续观察"
    assert AuctionAlertService._classify(7.0)[0] == "高开加速观察"
    assert AuctionAlertService._classify(None)[0] == "数据不足"


def test_realtime_notification_only_sends_confirmed(monkeypatch):
    service = NotificationService()
    calls = []

    def fake_send(title, content, **kwargs):
        calls.append((title, content, kwargs))
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(service, "send", fake_send)
    count = service.notify_realtime_payload({
        "market_date": "20260706",
        "rows": [
            {
                "code": "000001",
                "name": "测试A",
                "confirm_status": "confirmed",
                "entry_mode_text": "弱转强",
                "pct_chg": 2.3,
                "last_price": 12.34,
                "strategy_name": "弱转强修复",
                "resonance_sectors": "机器人",
                "confirm_time": "09:43:00",
                "suggested_position": "确认后参考10%",
            },
            {"code": "000002", "name": "测试B", "confirm_status": "observe", "entry_mode_text": "观察", "pct_chg": 1.0},
        ],
    })

    assert count == 1
    assert len(calls) == 1
    assert "弱转强确认" in calls[0][1]
    assert "测试A（000001）" in calls[0][1]
    assert "12.34" in calls[0][1]
    assert "机器人" in calls[0][1]
    assert "确认后参考10%" in calls[0][1]


def test_realtime_notification_sends_confirmed_leader_strength(monkeypatch):
    service = NotificationService()
    calls = []

    def fake_send(title, content, **kwargs):
        calls.append((title, content, kwargs))
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(service, "send", fake_send)
    count = service.notify_realtime_payload({
        "market_date": "20260707",
        "profile": "leader_pool",
        "observation_source": "leader_pool",
        "strategy": {"id": "leader_pool", "name": "近期龙头池"},
        "rows": [
            {
                "code": "600001",
                "name": "龙头测试",
                "status": "confirmed",
                "entry_mode": "weak_to_strong",
                "entry_mode_text": "弱转强",
                "change_pct": 3.25,
                "last_price": 18.66,
                "confirm_time": "09:46:00",
                "leader_roles": ["板块龙头", "情绪龙头"],
                "lifecycle_state": "分歧龙头",
                "leader_time_label": "上一交易日龙头",
                "resonance_sectors": "机器人",
            },
            {"code": "600002", "name": "观察龙头", "status": "observe"},
        ],
    })

    assert count == 1
    assert len(calls) == 1
    assert calls[0][0] == "龙头盘中转强确认：龙头测试"
    assert "龙头身份：板块龙头、情绪龙头" in calls[0][1]
    assert "龙头阶段：分歧龙头，上一交易日龙头" in calls[0][1]
    assert "行情：18.66，涨幅+3.25%" in calls[0][1]
    assert calls[0][2]["event_key"] == "intraday:20260707:600001:weak_to_strong"


def test_notification_service_reports_channels_without_exposing_secrets(monkeypatch):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCT-secret-value")
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)

    status = NotificationService(backend=MemoryStateBackend("notify-status")).status()

    assert status["enabled"] is True
    assert status["configured_count"] == 1
    assert status["channels"]["Server酱个人微信"] is True
    assert "secret" not in str(status).lower()


def test_notification_service_deduplicates_same_signal(monkeypatch):
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://example.invalid/wecom")
    monkeypatch.delenv("SERVERCHAN_SENDKEY", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    backend = MemoryStateBackend("notify-dedup")
    calls = []
    service = NotificationService(backend=backend)
    monkeypatch.setattr(
        service,
        "_post_json",
        lambda url, payload: calls.append((url, payload)) or {"ok": True, "status": 200},
    )

    first = service.send("买点", "测试", event_key="intraday:20260706:000001:weak")
    second = service.send("买点", "测试", event_key="intraday:20260706:000001:weak")

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["deduplicated"] is True
    assert len(calls) == 1


def test_internal_scheduler_registers_both_jobs(monkeypatch):
    monkeypatch.setenv("AUTOMATION_ENABLED", "true")
    scheduler = InternalScheduler()
    scheduler.start()
    try:
        status = scheduler.status()
        tags = {tag for job in status["jobs"] for tag in job["tags"]}
        assert status["running"] is True
        assert tags == {"auction", "daily"}
    finally:
        scheduler.stop()


def test_model_health_reports_active_fallback(tmp_path):
    monitor = ModelHealthMonitor(tmp_path)
    result = monitor.write({
        "trade_date": "20260703",
        "final": [{"confidence_grade": "C"}],
        "weight_metadata": {
            "candidate_model_runtime": "fallback_drift",
            "market_regime": "strong",
            "feature_drift": {"status": "degraded", "max_psi": 0.4, "max_ks": 0.2},
        },
    })

    assert result["status"] == "fallback"
    assert "IC/IR" in result["message"]
    assert (tmp_path / "latest.json").exists()


def test_model_health_triggers_training_diagnostic_after_three_no_ab_days(tmp_path):
    monitor = ModelHealthMonitor(tmp_path)
    for date in ("20260701", "20260702", "20260703"):
        result = monitor.write({
            "trade_date": date,
            "final": [{"confidence_grade": "C"}],
            "weight_metadata": {"candidate_model_runtime": "fallback_drift"},
        })

    assert result["no_ab_streak"] == 3
    assert result["training_diagnostic_triggered"] is True
    assert "连续3个交易日" in result["training_diagnostic_message"]
