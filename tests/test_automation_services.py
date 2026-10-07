from __future__ import annotations

from datetime import timedelta

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


def test_auction_notification_lists_stocks_and_open_gaps():
    content = AuctionAlertService.notification_content({
        "candidate_date": "20260904",
        "rows": [
            {
                "code": "600001",
                "name": "竞价甲",
                "rank": 2,
                "category": "高开加速观察",
                "open_gap_pct": 7.26,
                "open_price": 12.34,
                "resonance_sectors": "机器人",
            },
            {
                "code": "000002",
                "name": "竞价乙",
                "rank": 1,
                "category": "高开加速观察",
                "open_gap_pct": 5.18,
                "open_price": 20.56,
            },
            {
                "code": "300003",
                "name": "竞价丙",
                "rank": 3,
                "category": "数据不足",
                "open_gap_pct": None,
            },
        ],
    })

    assert "【高开加速观察】2只" in content
    assert "竞价甲（600001）：高开+7.26%，开盘12.34，板块：机器人" in content
    assert "竞价乙（000002）：高开+5.18%，开盘20.56" in content
    assert "【数据不足】1只" in content
    assert "竞价丙（300003）：竞价数据不足" in content
    assert "不以竞价结果直接买入" in content


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
        "candidate_date": "20260706",
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
                "pool_rank": 1,
                "source_rank": 197,
                "leader_score": 59.1,
                "limit_pct": 10,
                "limit_progress": 1.0,
                "sector_status_score": 49.6,
                "market_status_score": 60.2,
                "continuity_score": 65,
                "capital_recognition_score": 52.8,
                "safety_score": 82.4,
                "evidence": {"板块地位": False, "持续性": True},
                "open_gap_pct": 2.6,
                "reason": "站上分时均价并突破前高",
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
    assert "候选日：20260706；行情日：20260707" in calls[0][1]
    assert "池排名#1，龙头评分59.1" in calls[0][1]
    assert "候选名次：#197（仅作观察顺序）" in calls[0][1]
    assert "候选日评分：板块地位50 / 市场辨识度60 / 身份持续性65 / 资金认可53 / 接力安全82" in calls[0][1]
    assert "候选日涨停进度：100%（10cm）" in calls[0][1]
    assert "身份依据：持续性" in calls[0][1]
    assert "今日开盘：+2.60%" in calls[0][1]
    assert "盘中确认依据：站上分时均价并突破前高" in calls[0][1]
    assert "板块地位49.6" not in calls[0][1]
    assert calls[0][2]["event_key"] == "intraday:20260707:600001:weak_to_strong"


def test_realtime_notification_omits_unavailable_leader_metrics(monkeypatch):
    service = NotificationService()
    messages = []
    monkeypatch.setattr(service, "send", lambda title, content, **kwargs: (
        messages.append(content) or {"ok": True, "sent": 1}
    ))

    service.notify_realtime_payload({
        "market_date": "20260707", "profile": "leader_pool",
        "rows": [{
            "code": "600001", "name": "测试龙头", "status": "confirmed",
            "pool_rank": 0, "leader_score": float("nan"),
            "limit_progress": float("nan"), "structure": {"protection": 0},
        }],
    })

    assert len(messages) == 1
    assert "龙头位置：" not in messages[0]
    assert "候选日涨停进度：" not in messages[0]
    assert "结构保护价：0" not in messages[0]


def test_notification_service_reports_channels_without_exposing_secrets(monkeypatch):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCT-secret-value")
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)

    status = NotificationService(backend=MemoryStateBackend("notify-status")).status()

    assert status["enabled"] is True
    assert status["configured_count"] == 1
    assert status["channels"]["Server酱个人微信"] is True
    assert status["serverchan_recipient_count"] == 1
    assert "secret" not in str(status).lower()


def test_notification_service_broadcasts_to_multiple_serverchan_recipients(monkeypatch):
    monkeypatch.setenv("SERVERCHAN_SENDKEY", "SCT-legacy")
    monkeypatch.setenv("SERVERCHAN_SENDKEYS", "SCT-first, SCT-second;SCT-first\nSCT-third")
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK_URL", raising=False)
    service = NotificationService(backend=MemoryStateBackend("notify-multiple-serverchan"))
    calls = []
    monkeypatch.setattr(
        service,
        "_post_form",
        lambda url, payload: calls.append((url, payload)) or {"ok": True, "status": 200},
    )

    result = service.send("买点", "测试多接收方")
    status = service.status()

    assert result["ok"] is True
    assert result["sent"] == 4
    assert len(calls) == 4
    assert status["configured_count"] == 1
    assert status["serverchan_recipient_count"] == 4
    assert status["configured_endpoint_count"] == 4
    assert all("SCT-" not in str(item) for item in result["results"])


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
    monkeypatch.setenv("AUTOMATION_DAILY_CATCH_UP", "false")
    scheduler = InternalScheduler()
    scheduler.start()
    try:
        status = scheduler.status()
        tags = {tag for job in status["jobs"] for tag in job["tags"]}
        assert status["running"] is True
        assert tags == {"auction", "daily"}
    finally:
        scheduler.stop()


def test_internal_scheduler_recovers_incomplete_daily_job(monkeypatch):
    monkeypatch.setenv("AUTOMATION_DAILY_CATCH_UP", "true")
    monkeypatch.setenv("AUTOMATION_DAILY_TIME", "00:00")
    scheduler = InternalScheduler()
    today = __import__("datetime").datetime.now().strftime("%Y%m%d")
    scheduler.calendar.is_trade_date = lambda trade_date: trade_date == today
    scheduler.calendar.get_trade_dates = lambda start, end: [today]
    scheduler.daily_state.save({
        "status": "error",
        "job": "daily",
        "trade_date": today,
        "attempt": 1,
        "pipeline_ok": False,
    })
    dispatched = []
    def fake_dispatch(name, target):
        dispatched.append((name, target))
        return True

    monkeypatch.setattr(scheduler, "_dispatch_job", fake_dispatch)

    assert scheduler._recover_due_daily_job() is True
    assert dispatched[0][0] == "daily"


def test_internal_scheduler_waits_until_daily_retry_is_due(monkeypatch):
    monkeypatch.setenv("AUTOMATION_DAILY_CATCH_UP", "true")
    monkeypatch.setenv("AUTOMATION_DAILY_TIME", "00:00")
    monkeypatch.setenv("AUTOMATION_DAILY_RETRY_MINUTES", "15")
    scheduler = InternalScheduler()
    now = scheduler._now()
    today = now.strftime("%Y%m%d")
    scheduler.calendar.is_trade_date = lambda trade_date: trade_date == today
    scheduler.daily_state.save({
        "status": "error",
        "job": "daily",
        "trade_date": today,
        "attempt": 1,
        "pipeline_ok": False,
        "next_retry_at": (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
    })
    dispatched = []
    monkeypatch.setattr(
        scheduler,
        "_dispatch_job",
        lambda name, target: dispatched.append((name, target)) or True,
    )

    assert scheduler._recover_due_daily_job() is False
    assert dispatched == []


def test_internal_scheduler_defaults_allow_delayed_post_close_source(monkeypatch):
    monkeypatch.delenv("AUTOMATION_DAILY_TIME", raising=False)
    monkeypatch.delenv("AUTOMATION_DAILY_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("AUTOMATION_DAILY_RETRY_MINUTES", raising=False)

    scheduler = InternalScheduler()

    assert scheduler.daily_time == "20:00"
    assert scheduler.max_daily_attempts == 6
    assert scheduler.daily_retry_minutes == 15


def test_internal_scheduler_uses_shanghai_timezone_and_separate_job_states(monkeypatch):
    monkeypatch.setenv("AUTOMATION_TIMEZONE", "Asia/Shanghai")
    scheduler = InternalScheduler()
    scheduler._save_daily_state({"status": "done", "job": "daily", "pipeline_ok": True})
    scheduler._save_auction_state({"status": "done", "job": "auction"})

    assert scheduler.timezone_name == "Asia/Shanghai"
    assert scheduler._now().utcoffset().total_seconds() == 8 * 3600
    assert scheduler.daily_state.load()["job"] == "daily"
    assert scheduler.auction_state.load()["job"] == "auction"


def test_internal_scheduler_explains_native_segfault():
    message = InternalScheduler._exit_code_message(-11)

    assert "SIGSEGV" in message
    assert "原生扩展" in message


def test_realtime_notification_limits_crowded_weak_market_cluster(monkeypatch):
    backend = MemoryStateBackend("notify-cluster-limit")
    service = NotificationService(backend=backend)
    calls = []

    def fake_send(title, content, **kwargs):
        calls.append((title, content, kwargs))
        return {"ok": True, "sent": 1}

    monkeypatch.setattr(service, "send", fake_send)
    rows = []
    for index, sector in enumerate(("创新药", "医疗研发外包", "CRO概念"), start=1):
        rows.append({
            "code": f"30000{index}",
            "name": f"医药{index}",
            "confirm_status": "confirmed",
            "entry_mode": "weak_to_strong",
            "entry_mode_text": "弱转强",
            "resonance_sectors": sector,
            "score": 90 - index,
        })

    count = service.notify_realtime_payload({
        "market_date": "20260814",
        "market_score": 39,
        "market_regime": "weak",
        "rows": rows,
    })

    assert count == 1
    assert len(calls) == 1
    assert "风险主题簇：医药医疗" in calls[0][1]
    assert sum(row.get("notification_status") == "同主题推送已达上限" for row in rows) == 2


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
